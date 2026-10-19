#!/usr/bin/env python3
"""Offline tests of npu-transcribe's dictation (--dictate, --dictate-file): the text while you
speak, when words become final, long dictations, and the engine's commands and events. No models,
no NPU, no microphone: a fake model hears each word as a burst of noise of its own loudness.

Run: python3 -I transcribe/tests/test_dictation.py -v      (needs numpy; the server test, ffmpeg)
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import select
import shutil
import subprocess
import sys
import time
import unittest
import wave
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
TOOL = HERE.parent / "npu-transcribe"
_loader = importlib.machinery.SourceFileLoader("npu_transcribe", str(TOOL))
_spec = importlib.util.spec_from_loader(_loader.name, _loader)
nt = importlib.util.module_from_spec(_spec)
sys.modules[_loader.name] = nt
_loader.exec_module(nt)
RATE = nt.SAMPLE_RATE
WORD, GAP = 0.3, 0.1          # a word: this long, this much quiet after it


class Speech:
    """Made-up speech: each word is a tone of its own pitch (VOCAB), so the fake model knows it;
    WORD + GAP per word, the pause after a sentence as given."""

    def __init__(self) -> None:
        self.x = quiet(0.5, 0)
        self.words: list[tuple[str, float, float]] = []

    def say(self, sentence: str, pause: float = 1.2) -> "Speech":
        t = np.arange(int(WORD * RATE)) / RATE
        for w in sentence.split():
            start = self.x.size / RATE
            tone = (0.05 * np.sin(2 * np.pi * VOCAB.pitch(w) * t)).astype(np.float32)
            self.x = np.concatenate([self.x, tone + quiet(WORD, len(self.words)), quiet(GAP, len(self.words))])
            self.words.append((w, start, start + WORD))
        self.x = np.concatenate([self.x, quiet(pause, len(self.words))])
        return self


def quiet(seconds: float, seed: int) -> np.ndarray:
    return np.random.default_rng(seed).normal(0, 0.0005, int(seconds * RATE)).astype(np.float32)


class Vocab:
    def __init__(self) -> None:
        self.words: list[str] = []

    def pitch(self, word: str) -> float:
        if word not in self.words:
            self.words.append(word)
        return 150.0 + 30.0 * self.words.index(word)

    def nearest(self, pitch: float) -> str:
        return self.words[min(len(self.words) - 1, max(0, round((pitch - 150.0) / 30.0)))]


VOCAB = Vocab()


class FakeGigaAM(nt.GigaAM):
    """GigaAM's words(): the tones in the block, each the word of its pitch."""

    def __init__(self) -> None:  # (no model)
        self.device, self.calls = "NPU", 0

    def words(self, block: np.ndarray) -> list:
        self.calls += 1
        hop = RATE // 50
        loud = nt.frame_rms(block, hop) > 0.008
        out, i = [], 0
        while i < loud.size:
            if not loud[i]:
                i += 1
                continue
            j = i
            while j < loud.size and loud[j]:
                j += 1
            if (j - i) * hop >= 0.15 * RATE:     # (a word cut in half by the block's end is still heard)
                part = block[i * hop:j * hop]
                pitch = float(np.argmax(np.abs(np.fft.rfft(part)))) * RATE / part.size
                out.append(nt.Word(i * hop / RATE, j * hop / RATE, " " + VOCAB.nearest(pitch)))
            i = j
        return out


class FakeWhisper:
    """Whisper's generate() (without timestamps, as dictation asks for it), from the same tones."""

    def __init__(self) -> None:
        self.giga, self.device, self.model_dir, self.word_timestamps = FakeGigaAM(), "NPU", Path("fake-whisper"), False

    def generate(self, block, **kwargs):
        from types import SimpleNamespace as NS
        ws = self.giga.words(block)
        return NS(texts=["".join(w.text for w in ws)], language="<|en|>")


def dictate(speech: Speech, engine, partial: float | None = None, stop_at: float | None = None):
    """speech said into a Dictation as the server does it (DICTATE_TICK, simulated clock).
    Returns the text events (time, final, draft) and the Dictation."""
    clock = nt.Clock(simulated=True)
    d = nt.Dictation(engine, {} if isinstance(engine, nt.GigaAM) else {"language": "<|en|>"}, partial)
    x = speech.x if stop_at is None else speech.x[:int(stop_at * RATE)]
    events, pos = [], 0
    while True:
        upto = min(x.size, int(clock.now() * RATE))
        d.append(x[pos:upto])
        pos = max(pos, upto)
        over = pos >= x.size
        if d.step(over):
            ev = nt.text_event(d, clock)
            events.append((ev["at"], ev["final"], ev["draft"]))
        if over:
            return events, d
        clock.sleep(nt.DICTATE_TICK - clock.now() % nt.DICTATE_TICK)


def first_shown(events, word: str) -> float:
    return next(t for t, final, draft in events if word in [x.strip(".,") for x in f"{final} {draft}".split()])


class Drafts(unittest.TestCase):
    def test_words_show_while_you_speak(self):
        s = Speech().say("Alpha bravo charlie delta echo foxtrot golf hotel.")
        events, d = dictate(s, FakeGigaAM())
        self.assertEqual(d.text(), "Alpha bravo charlie delta echo foxtrot golf hotel.")
        for w, _, end in s.words:   # each word within a redraft (+ a tick) after it was said
            self.assertLess(first_shown(events, w.rstrip(".")) - end, nt.DICTATE_PARTIAL + 2 * nt.DICTATE_TICK + 0.2, w)
        self.assertTrue(any(draft and not final for _, final, draft in events))   # a draft before anything is final

    def test_sentence_final_after_a_pause_and_not_redone(self):
        s = Speech().say("Alpha bravo charlie.", pause=1.2).say("Delta echo foxtrot golf.")
        engine = FakeGigaAM()
        events, d = dictate(s, engine)
        first_end = s.words[2][2]
        final_at = next(t for t, final, _ in events if final == "Alpha bravo charlie.")
        self.assertLess(final_at - first_end, nt.DICTATE_PAUSE + 0.6)
        self.assertLess(final_at, s.words[3][1] + 0.5)    # final before the next sentence is under way
        self.assertEqual(d.final, ["Alpha bravo charlie.", "Delta echo foxtrot golf."])
        # after it is final, only the new sentence is transcribed again: its drafts lack the first one
        later = [draft for t, final, draft in events if t > final_at and draft]
        self.assertTrue(later and all("Alpha" not in draft for draft in later))

    def test_clean_speech_is_sound_without_a_noise_floor(self):
        """3 s of clean speech (-13..-19 dBFS) with one short breath: the quietest tenth is speech, so
        it cannot be the noise. Speech above LIVE_SPEECH_DBFS is sound anyway; it was taken for a pause
        in the middle of a sentence, which made the words in it final too early and lost them."""
        tone = lambda amp: (amp * np.sin(2 * np.pi * 200 * np.arange(nt.LIVE_FRAME) / RATE)).astype(np.float32)
        frames = [tone(0.3 if i % 2 else 0.15) for i in range(30)]
        x = np.concatenate(frames[:12] + [quiet(0.2, 1)] + frames[12:])
        d = nt.Dictation(FakeGigaAM(), {})
        d.append(x)
        d.listen()
        self.assertGreaterEqual(d.sound, x.size - nt.LIVE_FRAME)

    def test_no_sentence_end_no_freeze(self):
        s = Speech().say("Alpha bravo charlie", pause=1.2).say("delta echo.")
        events, d = dictate(s, FakeGigaAM())
        self.assertFalse(any(final for t, final, _ in events if t < s.words[-1][2]))
        self.assertEqual(d.text(), "Alpha bravo charlie delta echo.")

    def test_long_dictation_is_cut_and_nothing_is_lost(self):
        words = [f"w{i}" for i in range(80)]    # 32 s without a sentence end or a pause
        s = Speech().say(" ".join(words) + ".", pause=0.5)
        engine = FakeGigaAM()
        events, d = dictate(s, engine)
        self.assertGreaterEqual(len(d.final), 2)                     # cut at least once (DICTATE_LONGEST)
        self.assertEqual(d.text().rstrip(".").split(), words)          # every word once, in order
        cut_at = next(t for t, final, _ in events if final)
        self.assertLess(cut_at, nt.DICTATE_LONGEST + 2)   # (so GigaAM's 30 s input always holds the rest)
        self.assertNotIn("W", d.text()[1:])                 # the part after a cut lost its capital

    def test_stop_mid_sentence_gives_everything_said(self):
        s = Speech().say("Alpha bravo charlie delta echo foxtrot.")
        stop = s.words[3][2] + 0.05     # right after "delta"
        events, d = dictate(s, FakeGigaAM(), stop_at=stop)
        self.assertEqual(d.text(), "Alpha bravo charlie delta")

    def test_silence_costs_nothing(self):
        s = Speech()
        s.x = np.concatenate([s.x, quiet(10, 3)])
        engine = FakeGigaAM()
        events, d = dictate(s, engine)
        self.assertEqual((events, d.text(), engine.calls), ([], "", 0))

    def test_passes_per_minute(self):
        s = Speech()
        for i in range(6):
            s.say(" ".join(f"s{i}w{j}" for j in range(12)) + ".", pause=1.0)
        engine = FakeGigaAM()
        events, d = dictate(s, engine)
        seconds = s.x.size / RATE
        # one per DICTATE_PARTIAL of speech, plus one as each sentence ends and one at the end
        self.assertLessEqual(engine.calls, seconds / nt.DICTATE_PARTIAL + 2 * 6 + 2)
        self.assertEqual(len(d.final), 6)

    def test_whisper_redrafts_less_often(self):
        s = Speech().say(" ".join(f"x{j}" for j in range(25)) + ".")
        giga, whisper = FakeGigaAM(), FakeWhisper()
        _, d1 = dictate(s, giga)
        _, d2 = dictate(s, whisper)
        self.assertEqual(d1.text(), d2.text())
        self.assertLess(whisper.giga.calls, giga.calls * 0.75)


class Joining(unittest.TestCase):
    def test_capital_after_a_cut_mid_sentence(self):
        self.assertEqual(nt.join_text(["Мы сравниваем сигналы так, чтобы оно", "Соответствовало уху."]),
                         "Мы сравниваем сигналы так, чтобы оно соответствовало уху.")
        self.assertEqual(nt.join_text(["Первая задача.", "Вторая задача."]), "Первая задача. Вторая задача.")
        self.assertEqual(nt.join_text(["Мы запустили", "GPU-шки вчера."]), "Мы запустили GPU-шки вчера.")
        self.assertEqual(nt.join_text(["Вот что", "Я думаю."]), "Вот что Я думаю.")   # (one letter: left alone)
        self.assertEqual(nt.join_text(["", "Hello", ""]), "Hello")

    def test_text_event_draft_is_joined_too(self):
        d = nt.Dictation(FakeGigaAM(), {})
        d.final, d.draft = ["Мы сравниваем сигналы так, чтобы оно"], "Соответствовало"
        ev = nt.text_event(d, nt.Clock(simulated=True))
        self.assertEqual((ev["final"], ev["draft"]), ("Мы сравниваем сигналы так, чтобы оно", "соответствовало"))


class Microphone(unittest.TestCase):
    """MicSource reads pw-record's pipe without blocking and loses nothing when it is stopped (a
    stand-in for pw-record writes numbered samples; no microphone is opened)."""
    STAND_IN = r"""
import os, signal, struct, sys, time
written = 0
def stop(*_):
    open(sys.argv[1], "w").write(str(written)); sys.exit(0)
signal.signal(signal.SIGTERM, stop)
while True:
    os.write(1, struct.pack("<160h", *[(written // 2 + i) % 30000 for i in range(160)]))
    written += 320
    time.sleep(0.002)
"""

    def test_reads_as_it_comes_and_keeps_the_rest_at_the_stop(self):
        tmp = HERE / ".work-mic"
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir()
        old = nt.MicSource.COMMAND
        try:
            nt.MicSource.COMMAND = [sys.executable, "-I", "-c", self.STAND_IN, str(tmp / "written")]
            mic = nt.MicSource()
            got, t0 = [], time.monotonic()
            while time.monotonic() - t0 < 0.6:
                got.append(mic.read())               # (never blocks: often nothing new)
                time.sleep(0.05)
            got.append(mic.close())
            x = np.concatenate(got)
            written = int((tmp / "written").read_text()) // 2
            self.assertGreater(x.size, 0)
            self.assertEqual(x.size, written)        # nothing lost at the stop
            expect = (np.arange(x.size) % 30000).astype(np.float32) / 32768.0
            self.assertTrue(np.array_equal(x, expect))   # in order, none twice
            self.assertFalse(mic.ended())
        finally:
            nt.MicSource.COMMAND = old
            shutil.rmtree(tmp, ignore_errors=True)


class Server(unittest.TestCase):
    """--dictate: commands on stdin, events on stdout (the fake model, a file instead of the microphone)."""
    HARNESS = r"""
import importlib.machinery, importlib.util, sys
sys.path.insert(0, sys.argv[1])
import json
import test_dictation as t
nt = t.nt
t.VOCAB.words = json.loads(sys.argv[2])
class Engines:
    _whisper = _gigaam = None
    def for_language(self, language, words=False):
        return (t.FakeGigaAM() if language == "ru" else t.FakeWhisper()), language
args = nt.parse_args(["--dictate", "--language", "ru"])
sys.exit(nt.dictate_serve(args, Engines()))
"""

    def setUp(self):
        self.tmp = HERE / ".work-dictation"
        shutil.rmtree(self.tmp, ignore_errors=True)
        self.tmp.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def wav(self, speech: Speech) -> Path:
        path = self.tmp / "said.wav"
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(RATE)
            w.writeframes((np.clip(speech.x, -1, 1) * 32767).astype("<i2").tobytes())
        return path

    def read_until(self, proc, key: str, timeout: float = 15.0) -> list[dict]:
        out, end = [], time.monotonic() + timeout
        while time.monotonic() < end:
            if not select.select([proc.stdout], [], [], 0.2)[0]:
                continue
            line = proc.stdout.readline()
            if not line:
                break
            out.append(json.loads(line))
            if key in out[-1]:
                return out
        self.fail(f"no {key!r} event: {out}")

    @unittest.skipUnless(shutil.which("ffmpeg"), "needs ffmpeg")
    def test_start_stop_cancel(self):
        s = Speech().say("Alpha bravo charlie.", pause=0.8).say("Delta echo.", pause=0.5)
        wav = self.wav(s)
        proc = subprocess.Popen([sys.executable, "-I", "-c", self.HARNESS, str(HERE),
                                 json.dumps(VOCAB.words)], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        try:
            ready = self.read_until(proc, "ready")[-1]
            self.assertEqual((ready["language"], ready["model"]), ("ru", "gigaam-v3-ctc"))
            send = lambda cmd: (proc.stdin.write(json.dumps(cmd) + "\n"), proc.stdin.flush())
            send({"start": {"language": "ru", "file": str(wav), "speed": 4}})
            events = self.read_until(proc, "started")
            time.sleep(s.x.size / RATE / 4 + 0.5)
            send({"stop": {}})
            events = self.read_until(proc, "done")
            self.assertEqual(events[-1]["text"], "Alpha bravo charlie. Delta echo.")
            self.assertTrue(any(e.get("draft") for e in events), events)
            self.assertGreater(events[-1]["passes"], 0)
            # cancel: nothing comes of it; then English (Whisper) in the same engine
            send({"start": {"language": "en", "file": str(wav), "speed": 4}})
            self.assertEqual(self.read_until(proc, "started")[-1]["model"], "fake-whisper")
            send({"cancel": {}})
            self.read_until(proc, "cancelled")
            send({"stop": {}})       # (no dictation: ignored)
            send({"start": {"language": "ru", "file": str(self.tmp / "missing.wav")}})
            self.assertIn("error", self.read_until(proc, "error")[-1])
            proc.stdin.close()
            self.assertEqual(proc.wait(10), 0)
        finally:
            proc.kill()
            for f in (proc.stdin, proc.stdout):
                if not f.closed:
                    f.close()


if __name__ == "__main__":
    unittest.main()
