#!/usr/bin/env python3
"""Offline tests of npu-transcribe's speaker helpers: labelling and splitting by speaker,
echo removal and the meeting Markdown. No models, no NPU: only numpy is needed.

Run: python3 -I transcribe/tests/test_speakers.py -v
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import shutil
import sys
import unittest
from pathlib import Path

import numpy as np

_loader = importlib.machinery.SourceFileLoader("npu_transcribe", str(Path(__file__).resolve().parent.parent
                                                                      / "npu-transcribe"))
_spec = importlib.util.spec_from_loader(_loader.name, _loader)
nt = importlib.util.module_from_spec(_spec)
sys.modules[_loader.name] = nt  # dataclasses look the module up
_loader.exec_module(nt)
Seg, Turn, Word = nt.Segment, nt.Turn, nt.Word
RATE = nt.SAMPLE_RATE


def words(spec: str) -> list:
    """"text@start-end text@start-end ..." -> [Word, ...]"""
    out = []
    for item in spec.split():
        text, span = item.split("@")
        start, end = map(float, span.split("-"))
        out.append(Word(start, end, " " + text))
    return out


def noise(seconds: float, level: float, seed: int) -> np.ndarray:
    return np.random.default_rng(seed).normal(0, level, int(seconds * RATE)).astype(np.float32)


class Labelling(unittest.TestCase):
    TURNS = [Turn(0, 4.0, 0), Turn(4.2, 9, 1), Turn(9.5, 12, 0)]

    def test_sentence_split_at_speaker_change(self):
        seg = Seg(0.5, 6.0, "Okay that is all. Thank you Viktor, go on.",
                  words("Okay@0.5-0.8 that@0.8-1.0 is@1.0-1.2 all.@1.2-1.6 Thank@4.3-4.6 you@4.6-4.8 "
                        "Viktor,@4.8-5.3 go@5.4-5.6 on.@5.6-6.0"))
        parts, labels = nt.number_speakers([seg], self.TURNS, "Person", "Remote")
        self.assertEqual(labels, {0: "Person 1", 1: "Person 2"})
        self.assertEqual([(p.speaker, p.text) for p in parts],
                         [("Person 1", "Okay that is all."), ("Person 2", "Thank you Viktor, go on.")])
        self.assertEqual((parts[1].start, parts[1].end), (4.3, 6.0))

    def test_short_blip_is_smoothed(self):
        turns = [Turn(0, 2.0, 0), Turn(2.0, 2.3, 1), Turn(2.3, 6, 0)]
        seg = Seg(0, 5, "one two three four five",
                  words("one@0-0.9 two@1-1.9 three@2.0-2.3 four@2.4-3.5 five@3.6-4.5"))
        parts, labels = nt.number_speakers([seg], turns, "Person", "Remote")
        self.assertEqual([(p.speaker, p.text) for p in parts], [("Person 1", "one two three four five")])
        self.assertEqual(len(labels), 1)

    def test_no_voices_found(self):
        seg = Seg(0, 2, "hello", words("hello@0.2-0.8"))
        parts, labels = nt.number_speakers([seg], [], "Person", "Remote")
        self.assertEqual((parts[0].speaker, labels), ("Remote", {}))

    def test_numbered_by_first_words(self):
        # diarization's voice 1 says the first words here: it becomes Speaker 1
        parts, _ = nt.number_speakers([Seg(4.5, 8, "hello there")], self.TURNS, "Speaker", "?")
        self.assertEqual(parts[0].speaker, "Speaker 1")

    def test_recognised_voices_keep_their_name(self):
        segs = [Seg(0.5, 2, "hi"), Seg(4.5, 6, "hello"), Seg(9.6, 11, "bye")]
        turns = self.TURNS + [Turn(12, 14, 2)]
        segs.append(Seg(12.2, 13.5, "late"))
        parts, labels = nt.number_speakers(segs, turns, "Person", "Remote", {1: "Viktor"})
        self.assertEqual([p.speaker for p in parts], ["Person 1", "Viktor", "Person 1", "Person 2"])
        self.assertEqual(labels, {0: "Person 1", 1: "Viktor", 2: "Person 2"})

    def test_short_replies_without_a_turn_go_to_the_nearest_speaker(self):
        # "Угу" at 20 s: the segmentation gave it no turn; voice 1 spoke last, 11 s before
        parts, _ = nt.number_speakers([Seg(4.5, 8, "hello"), Seg(20, 20.4, "Угу.")],
                                      [Turn(0, 3, 0), Turn(4.2, 9, 1)], "Person", "Remote")
        self.assertEqual([p.speaker for p in parts], ["Person 1", "Person 1"])
        far, _ = nt.number_speakers([Seg(60, 61, "late")], [Turn(0, 3, 0)], "Person", "Remote")
        self.assertEqual(far[0].speaker, "Remote")  # more than NEAREST_SECONDS away

    def test_words_between_turns_take_a_neighbour(self):
        seg = Seg(3, 6, "a b c", words("a@3.0-3.5 b@4.05-4.15 c@4.4-5.0"))
        parts, _ = nt.number_speakers([seg], [Turn(0, 3.6, 0), Turn(5.5, 9, 1)], "P", "?")
        self.assertEqual(len(parts), 1)  # b and c are near voice 0's end (within 1 s)


class Voices(unittest.TestCase):
    @staticmethod
    def vec(*xs):
        v = np.zeros(192)
        v[:len(xs)] = xs
        return list(v)

    def turns_and_vecs(self, *spec):
        """spec: (start, end, voice, embedding or None) per turn"""
        turns = [Turn(a, b, v) for a, b, v, _ in spec]
        vecs = {i: (nt.unit(e), b - a) for i, (a, b, _, e) in enumerate(spec) if e is not None}
        return turns, vecs

    def test_identify_names_voices_and_splits_merged_people(self):
        anna, boris, other = self.vec(1, 0, 0), self.vec(0, 1, 0), self.vec(0, 0, 1)
        # the clustering put Anna and Boris into voice 0; voice 1 is someone not remembered
        turns, vecs = self.turns_and_vecs((0, 3, 0, anna), (3.5, 6, 0, boris), (6.2, 6.6, 0, None),
                                          (7, 9, 1, other), (9.5, 12, 0, anna))
        moved, names = nt.identify(turns, vecs, {"Anna": [anna], "Boris": [self.vec(0.1, 1, 0)]})
        self.assertEqual([t.speaker for t in moved], [0, 3, 0, 1, 0])   # Boris's turn split off
        self.assertEqual(names, {0: "Anna", 3: "Boris"})

    def test_identify_needs_similarity_and_margin(self):
        both = self.vec(1, 0.9, 0)       # 0.74 like Anna, 0.67 like Boris: too close to call
        weak = self.vec(0.5, 0, 1)       # 0.45 like Anna: not enough
        turns, vecs = self.turns_and_vecs((0, 3, 0, both), (4, 6, 1, weak))
        moved, names = nt.identify(turns, vecs, {"Anna": [self.vec(1, 0, 0)], "Boris": [self.vec(0, 1, 0)]})
        self.assertEqual((moved, names), (turns, {}))

    def test_identify_several_fingerprints(self):
        # Boris remembered from two meetings: their mean is compared (0.60 and 0.51 here)
        voices = {"Boris": [self.vec(0, 1, 0.3), self.vec(0, 1, -0.3)]}
        turns, vecs = self.turns_and_vecs((0, 2, 0, self.vec(0.8, 0.6, 0)))
        self.assertEqual(nt.identify(turns, vecs, voices)[1], {0: "Boris"})
        turns, vecs = self.turns_and_vecs((0, 2, 0, self.vec(0.85, 0.5, 0)))
        self.assertEqual(nt.identify(turns, vecs, voices)[1], {})

    def test_split_turn_where_remembered_people_change(self):
        boris, viktor = nt.unit(self.vec(0, 1, 0)), nt.unit(self.vec(1, 0, 0))

        def embed(a, b):  # Viktor speaks 11.5-14.5 s inside a turn the segmentation gave Boris
            mid = (a + b) / 2
            return viktor if 11.5 <= mid < 14.5 else boris
        out = nt.split_by_known_voices([Turn(0, 3, 0), Turn(8, 18, 0)], embed,
                                       {"Boris": boris, "Viktor": viktor})
        self.assertEqual([(t.start, t.speaker) for t in out[:2]], [(0, 0), (8, 0)])
        self.assertEqual(len(out), 4)
        self.assertAlmostEqual(out[2].start, 11.5, delta=0.26)
        self.assertAlmostEqual(out[3].start, 14.5, delta=0.26)
        self.assertEqual(out[3].end, 18)

    def test_short_or_unclear_changes_do_not_split(self):
        boris, viktor = nt.unit(self.vec(0, 1, 0)), nt.unit(self.vec(1, 0, 0))
        blip = nt.split_by_known_voices(
            [Turn(0, 10, 0)], lambda a, b: viktor if 5 <= (a + b) / 2 < 5.6 else boris,
            {"Boris": boris, "Viktor": viktor})   # Viktor for < VOICE_MIN_RUN: not a change
        self.assertEqual(blip, [Turn(0, 10, 0)])
        unknown = nt.split_by_known_voices(
            [Turn(0, 10, 0)], lambda a, b: nt.unit(self.vec(0, 0, 1)) if a > 4 else boris,
            {"Boris": boris})                      # someone not remembered: left to the clustering
        self.assertEqual(unknown, [Turn(0, 10, 0)])

    def test_merge_fragments_and_split_people(self):
        a, b = self.vec(1, 0, 0), self.vec(0, 1, 0)
        turns, vecs = self.turns_and_vecs(
            (0, 30, 0, a), (31, 60, 1, b), (61, 90, 0, a),
            (91, 95, 2, self.vec(0, 0, 1)),     # 4 s of something else: a fragment
            (96, 96.5, 3, None),                # too short to compare: nearest in time
            (100, 130, 4, self.vec(0.9, 0.2, 0)))  # 30 s, 0.98 like voice 0: the same person
        merged, _ = nt.merge_voices(turns, vecs)
        self.assertEqual([t.speaker for t in merged], [0, 1, 0, 0, 0, 0])
        # two people who just sound different stay apart, however much each says
        turns, vecs = self.turns_and_vecs((0, 20, 0, a), (21, 40, 1, self.vec(0.4, 0.9, 0)))
        self.assertEqual([t.speaker for t in nt.merge_voices(turns, vecs)[0]], [0, 1])  # 0.41

    def test_voice_prints_weight_the_longest_turns(self):
        turns, vecs = self.turns_and_vecs((0, 4, 0, self.vec(1, 0)), (5, 6, 0, self.vec(0, 1)),
                                          (7, 9, 1, self.vec(0, 0, 1)), (9, 9.5, 1, None))
        prints = nt.voice_prints(turns, vecs)
        self.assertAlmostEqual(prints[0][0], 4 / 17 ** 0.5, places=4)   # 4 s of (1,0) + 1 s of (0,1)
        self.assertAlmostEqual(prints[0][1], 1 / 17 ** 0.5, places=4)
        self.assertEqual(prints[1][:3], [0.0, 0.0, 1.0])

    def test_voices_file(self, tmp=Path(__file__).resolve().parent / ".work-speakers"):
        import json
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir()
        try:
            self.assertEqual(nt.load_voices(tmp), {})  # none yet
            fp = self.vec(1, 2)
            (tmp / "voices.json").write_text(json.dumps({"version": 1, "model": nt.EMBEDDING_MODEL.name,
                                                         "voices": {"Anna": {"fingerprints": [fp]}}}))
            self.assertEqual(nt.load_voices(tmp), {"Anna": [fp]})
            (tmp / "voices.json").write_text(json.dumps({"version": 1, "model": "other.onnx",
                                                         "voices": {"Anna": {"fingerprints": [fp]}}}))
            self.assertEqual(nt.load_voices(tmp), {})  # another model's fingerprints don't compare
            nt.save_meeting_voices(tmp / "v", Path("/x/2026-10-12_1041-meeting.md"), {"Person 1": fp})
            saved = tmp / "v/meetings/2026-10-12_1041-meeting.json"
            self.assertEqual(json.loads(saved.read_text())["people"], {"Person 1": fp})
            self.assertEqual(saved.stat().st_mode & 0o777, 0o600)
            self.assertEqual((tmp / "v").stat().st_mode & 0o777, 0o700)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class Hallucinations(unittest.TestCase):
    def test_stock_phrases_only(self):
        for text in ("Продолжение следует...", "Субтитры сделал DimaTorzok", "Thanks for watching!",
                     "Продолжение следует. Продолжение следует.", "СПАСИБО ЗА ПРОСМОТР!"):
            self.assertTrue(nt.hallucination(text), text)
        for text in ("Продолжение следует в четверг, после релиза.", "Спасибо!", "", "Thank you."):
            self.assertFalse(nt.hallucination(text), text)


class Echo(unittest.TestCase):
    """System audio 0-10 s, leaking into the microphone at -20 dB; you speak 10-12 s."""

    def setUp(self):
        self.remote = np.zeros(12 * RATE, np.float32)
        self.remote[:10 * RATE] = noise(10, 0.1, 1)
        self.mic = self.remote * 0.1 + noise(12, 0.001, 2)
        self.mic[10 * RATE:] += noise(2, 0.1, 3)

    def test_echo_words_are_cut_from_your_sentence(self):
        seg = Seg(8, 12, "budget is fine yes I agree",
                  words("budget@8.5-9.0 is@9.0-9.3 fine@9.3-9.9 yes@10.1-10.5 I@10.6-10.8 agree@10.8-11.5"),
                  "You")
        kept, cut = nt.drop_echo([seg], [], self.mic, self.remote)
        self.assertEqual(cut, 3)
        self.assertEqual([(k.text, k.start) for k in kept], [("yes I agree", 10.1)])

    def test_headphones_nothing_cut(self):
        mic = noise(12, 0.001, 4)
        mic[10 * RATE:] += noise(2, 0.1, 5)
        seg = Seg(10, 12, "yes I agree", words("yes@10.1-10.5 I@10.6-10.8 agree@10.8-11.5"), "You")
        self.assertEqual(nt.drop_echo([seg], [], mic, self.remote), ([seg], 0))

    def test_talking_over_the_other_side_stays(self):
        mic = self.mic.copy()
        mic[5 * RATE:7 * RATE] += noise(2, 0.1, 6)
        seg = Seg(5, 7, "wait a second", words("wait@5.1-5.5 a@5.6-5.8 second@5.8-6.6"), "You")
        self.assertEqual(nt.drop_echo([seg], [], mic, self.remote), ([seg], 0))

    def test_without_word_timestamps_by_text(self):
        none = np.zeros(0, np.float32)
        theirs = [Seg(10, 13, "Let's look at the slide with the budget.", [], "Person 1"),
                  Seg(0.8, 3, "Давайте начнём встречу.", [], "Person 2")]
        mine = [Seg(10.2, 13.1, "let's look at the slide with the budget", [], "You"),
                Seg(1, 3, "Давайте начнём встречу", [], "You"),
                Seg(14, 15, "Yes, I agree with that.", [], "You")]
        kept, cut = nt.drop_echo(mine, theirs, none, none)
        self.assertEqual([k.text for k in kept], ["Yes, I agree with that."])
        self.assertEqual(cut, 12)


class Output(unittest.TestCase):
    def test_meeting_markdown(self):
        md = nt.meeting_md("Meeting 2026-10-12 10:41", None, 125.0,
                           [Seg(0, 2, "Hi all.", [], "You"), Seg(2.5, 5, "Привет.", [], "Person 1"),
                            Seg(70, 75, "Bye.", [], "Person 2"), Seg(3700, 3701, "Late.", [], "You")],
                           {"Language": "en"})
        self.assertIn("- **Speakers:** You, Person 1, Person 2\n", md)
        self.assertIn("- **Duration:** 2m 5s\n", md)
        self.assertIn("### Person 1\n\n*[00:02]* Привет.\n", md)
        self.assertIn("*[01:10]* Bye.", md)
        self.assertIn("*[1:01:40]* Late.", md)
        self.assertNotIn("Date", md)

    def test_file_outputs_with_speakers(self):
        t = nt.Transcript([Seg(0, 2, "Hello.", [], "Speaker 1"), Seg(2, 4, "Hi.", [], "Speaker 2")],
                          4.0, 1.0, "en", 0)
        self.assertIn("Speaker 2: Hi.", nt.to_srt(t))
        self.assertIn("[00:00:02] **Speaker 2:** Hi.", nt.to_md(t, Path("x.ogg"), {}))
        self.assertEqual(nt.to_txt(t), "Speaker 1: Hello.\n\nSpeaker 2: Hi.\n")

    def test_file_outputs_without_speakers_unchanged(self):
        t = nt.Transcript([Seg(0, 2, "Hello."), Seg(2, 4, "Hi.")], 4.0, 1.0, "en", 0)
        self.assertEqual(nt.to_txt(t), "Hello. Hi.\n")
        self.assertIn("[00:00:00] Hello. Hi.", nt.to_md(t, Path("x.ogg"), {}))
        self.assertNotIn(":", nt.to_srt(t).split("\n")[2])


@unittest.skipUnless(shutil.which("ffmpeg"), "needs ffmpeg (npu-transcribe decodes with it)")
class MeetingFolder(unittest.TestCase):
    def test_parts_stay_in_step(self, tmp=Path(__file__).resolve().parent / ".work-speakers"):
        import wave
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir()
        try:
            def wav(name, seconds):
                with wave.open(str(tmp / name), "wb") as w:
                    w.setnchannels(1)
                    w.setsampwidth(2)
                    w.setframerate(RATE)
                    w.writeframes(b"\x00\x10" * int(seconds * RATE))
            wav("part-1-mic.wav", 2.0)
            wav("part-1-remote.wav", 1.5)   # shorter: padded, so part 2 starts at 2.0 s on both
            wav("part-2-mic.wav", 1.0)
            wav("part-2-remote.wav", 1.0)
            (tmp / "meeting.json").write_text("{}")
            mic, remote = nt.read_meeting(tmp)
            self.assertEqual((mic.size, remote.size), (3 * RATE, 3 * RATE))
            self.assertEqual(float(remote[int(1.7 * RATE)]), 0.0)
            self.assertNotEqual(float(remote[int(2.1 * RATE)]), 0.0)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
