#!/usr/bin/env python3
"""Generate synthetic speech test clips for npu-transcribe.

Ubuntu desktops have the espeak-ng library (libespeak-ng.so.1 + espeak-ng-data,
pulled in by speech-dispatcher) but usually not the espeak-ng CLI.
We drive it through ctypes in synchronous mode, collect the PCM samples in
a callback and hand them to ffmpeg for encoding. Nothing is played on the
speakers and nothing is installed.

Usage: make_speech.py OUTDIR
Writes en_short.ogg, ru_short.ogg, de_short.ogg, fr_short.ogg (~5-8 s each),
en_long.mp4 (black video + speech, ~2.7 min: the briefing is read twice so it
crosses npu-transcribe's 120 s block boundary) and matching *.ref.txt texts.
"""
from __future__ import annotations

import ctypes
import subprocess
import sys
from pathlib import Path

# Constants from espeak-ng's speak_lib.h (stable public API).
AUDIO_OUTPUT_SYNCHRONOUS = 2
POS_CHARACTER = 1
ESPEAK_CHARS_UTF8 = 1
ESPEAK_ENDPAUSE = 0x1000
ESPEAK_RATE = 1
ESPEAK_INITIALIZE_DONT_EXIT = 0x8000

# Our own text (no copyrighted material). Paragraphs are separated by
# 1.5 s of silence in the long clip.
EN_SHORT = (
    "The quick brown fox jumps over the lazy dog. "
    "Please transcribe this short test recording on the neural processing unit."
)
RU_SHORT = (
    "Привет! Это короткая проверка распознавания русской речи. "
    "Сегодня хорошая погода, и мы тестируем нейронный процессор."
)
DE_SHORT = "Guten Morgen. Heute testen wir die Spracherkennung auf dem neuen Laptop."
FR_SHORT = "Bonjour à tous. Aujourd'hui nous testons la reconnaissance vocale sur le nouvel ordinateur."
EN_LONG = [
    "Welcome to the weekly maintenance briefing. Today we will cover backups, "
    "disk usage, and the new speech recognition tool running on the laptop.",
    "First, backups. The nightly job finished successfully on every machine "
    "except the old file server, which ran out of space on Tuesday. "
    "We moved the archive to the new storage pool and the job is green again.",
    "Second, disk usage. The build cache grew to almost two hundred gigabytes. "
    "We added a cleanup task that removes anything older than thirty days.",
    "Third, the speech recognition tool. It converts any audio or video file "
    "into subtitles and a readable transcript. It prefers the neural processing "
    "unit and falls back to the processor when the accelerator is not available.",
    "To try it, right click a recording in the file manager and choose "
    "transcribe. A terminal window shows the progress, and a notification "
    "appears when the transcript is ready.",
    "Finally, a reminder about passwords. Never paste secrets into chat tools, "
    "tickets, or shared documents. Use the password manager instead.",
    "That is all for this week. Thank you for listening, and see you next Monday.",
]


class Espeak:
    """Minimal ctypes wrapper around libespeak-ng in synchronous mode."""

    CALLBACK = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.POINTER(ctypes.c_short),
                                ctypes.c_int, ctypes.c_void_p)

    def __init__(self) -> None:
        self.lib = ctypes.CDLL("libespeak-ng.so.1")
        self.lib.espeak_Initialize.restype = ctypes.c_int
        self.rate = self.lib.espeak_Initialize(
            AUDIO_OUTPUT_SYNCHRONOUS, 0, None, ESPEAK_INITIALIZE_DONT_EXIT)
        if self.rate <= 0:
            raise RuntimeError("espeak_Initialize failed")
        self._pcm = bytearray()
        # Keep a reference to the callback so it is not garbage-collected.
        self._cb = self.CALLBACK(self._on_audio)
        self.lib.espeak_SetSynthCallback(self._cb)

    def _on_audio(self, wav, numsamples, _events) -> int:
        if numsamples > 0 and wav:
            self._pcm += ctypes.string_at(wav, numsamples * 2)
        return 0  # 0 = continue synthesis

    def say(self, text: str, voice: str, wpm: int = 160) -> bytes:
        """Return s16le mono PCM at self.rate for `text`."""
        if self.lib.espeak_SetVoiceByName(voice.encode()) != 0:
            raise RuntimeError(f"espeak voice {voice!r} not available")
        self.lib.espeak_SetParameter(ESPEAK_RATE, wpm, 0)
        self._pcm = bytearray()
        data = text.encode("utf-8") + b"\0"
        err = self.lib.espeak_Synth(data, len(data), 0, POS_CHARACTER, 0,
                                    ESPEAK_CHARS_UTF8 | ESPEAK_ENDPAUSE, None, None)
        if err != 0:
            raise RuntimeError(f"espeak_Synth failed: {err}")
        self.lib.espeak_Synchronize()
        return bytes(self._pcm)

    def silence(self, seconds: float) -> bytes:
        return b"\0\0" * int(self.rate * seconds)


def encode(pcm: bytes, rate: int, out: Path, video: bool = False) -> None:
    """Encode raw s16le mono PCM with ffmpeg (optionally with a black video track)."""
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y"]
    if video:
        cmd += ["-f", "lavfi", "-i", "color=c=black:s=320x240:r=5"]
    cmd += ["-f", "s16le", "-ar", str(rate), "-ac", "1", "-i", "pipe:0"]
    if video:
        cmd += ["-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac"]
    else:
        cmd += ["-c:a", "libvorbis"]
    subprocess.run(cmd + [str(out)], input=pcm, check=True)


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    outdir = Path(sys.argv[1])
    outdir.mkdir(parents=True, exist_ok=True)
    tts = Espeak()

    clips = {
        "en_short.ogg": (tts.silence(0.5) + tts.say(EN_SHORT, "en"), EN_SHORT),
        "ru_short.ogg": (tts.silence(0.5) + tts.say(RU_SHORT, "ru"), RU_SHORT),
        "de_short.ogg": (tts.silence(0.5) + tts.say(DE_SHORT, "de"), DE_SHORT),
        "fr_short.ogg": (tts.silence(0.5) + tts.say(FR_SHORT, "fr"), FR_SHORT),
    }
    long_pcm = tts.silence(1.0)
    for para in EN_LONG * 2:
        long_pcm += tts.say(para, "en") + tts.silence(1.5)
    clips["en_long.mp4"] = (long_pcm, " ".join(EN_LONG * 2))

    for name, (pcm, ref) in clips.items():
        out = outdir / name
        encode(pcm, tts.rate, out, video=name.endswith(".mp4"))
        (outdir / (out.stem + ".ref.txt")).write_text(ref + "\n", encoding="utf-8")
        print(f"{out}  {len(pcm) / 2 / tts.rate:6.1f} s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
