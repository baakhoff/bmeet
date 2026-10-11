#!/usr/bin/env python3
"""Make a synthetic speech test clip (16 kHz mono WAV) without the espeak-ng CLI.

Kubuntu ships libespeak-ng1 + espeak-ng-data (pulled in by speech-dispatcher) but
not the `espeak-ng` command, so this talks to the library through ctypes.
Used only to produce throw-away test audio; never point it at real recordings.

Usage:  python3 -I tts_clip.py <voice> "<text>" <out.wav>
        voice: "en", "ru", ... (espeak-ng voice names)
"""
from __future__ import annotations

import ctypes
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

LIB = "libespeak-ng.so.1"
AUDIO_OUTPUT_SYNCHRONOUS = 2  # espeak_AUDIO_OUTPUT enum
ESPEAK_CHARS_UTF8 = 1
SYNTH_CB = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.POINTER(ctypes.c_short), ctypes.c_int, ctypes.c_void_p)


def synth(voice: str, text: str) -> tuple[bytes, int]:
    """Return (16-bit PCM bytes, sample rate) for `text` spoken with `voice`."""
    lib = ctypes.CDLL(LIB)
    rate = lib.espeak_Initialize(AUDIO_OUTPUT_SYNCHRONOUS, 0, None, 0)
    if rate <= 0:
        raise RuntimeError("espeak_Initialize failed (is espeak-ng-data installed?)")
    chunks: list[bytes] = []

    def on_audio(wav: ctypes.POINTER(ctypes.c_short), n: int, _events: ctypes.c_void_p) -> int:
        if wav and n > 0:
            chunks.append(ctypes.string_at(wav, n * 2))
        return 0  # 0 = continue

    cb = SYNTH_CB(on_audio)  # keep a reference so it is not garbage-collected
    lib.espeak_SetSynthCallback(cb)
    if lib.espeak_SetVoiceByName(voice.encode()) != 0:
        raise RuntimeError(f"unknown espeak-ng voice: {voice}")
    data = text.encode("utf-8")
    buf = ctypes.create_string_buffer(data)
    lib.espeak_Synth(buf, len(data) + 1, 0, 0, 0, ESPEAK_CHARS_UTF8, None, None)
    lib.espeak_Synchronize()
    lib.espeak_Terminate()
    return b"".join(chunks), rate


def main() -> int:
    if len(sys.argv) != 4:
        print(__doc__, file=sys.stderr)
        return 2
    voice, text, out = sys.argv[1], sys.argv[2], Path(sys.argv[3])
    pcm, rate = synth(voice, text)
    # Temp file next to the output (scripts use $XDG_RUNTIME_DIR), not in /tmp.
    with tempfile.TemporaryDirectory(dir=out.resolve().parent) as tmp:
        raw = Path(tmp) / "raw.wav"
        with wave.open(str(raw), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(pcm)
        # Whisper wants 16 kHz mono; let ffmpeg resample.
        subprocess.run(
            ["ffmpeg", "-loglevel", "error", "-y", "-i", str(raw), "-ar", "16000", "-ac", "1", str(out)],
            check=True,
        )
    print(f"wrote {out} ({len(pcm) / 2 / rate:.1f} s, voice={voice})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
