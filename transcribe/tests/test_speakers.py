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

        def embed(spans):  # Viktor speaks 11.5-14.5 s inside a turn the segmentation gave Boris
            return [viktor if 11.5 <= (a + b) / 2 < 14.5 else boris for a, b in spans]
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
            [Turn(0, 10, 0)], lambda spans: [viktor if 5 <= (a + b) / 2 < 5.6 else boris for a, b in spans],
            {"Boris": boris, "Viktor": viktor})   # Viktor for < VOICE_MIN_RUN: not a change
        self.assertEqual(blip, [Turn(0, 10, 0)])
        unknown = nt.split_by_known_voices(
            [Turn(0, 10, 0)], lambda spans: [nt.unit(self.vec(0, 0, 1)) if a > 4 else boris for a, b in spans],
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


class NpuDiarizer(unittest.TestCase):
    """The parts of the NPU diarizer that are plain arithmetic."""

    def test_cluster(self):
        a, b = np.array([1.0, 0, 0]), np.array([0, 1.0, 0])
        near = lambda v, e: (v + e) / np.linalg.norm(v + e)
        embs = np.array([near(a, [0, .1, 0]), near(b, [.1, 0, 0]), near(a, [0, 0, .1]), near(b, [0, 0, .1])])
        self.assertEqual(nt.cluster(embs, threshold=0.4).tolist(), [0, 1, 0, 1])
        self.assertEqual(nt.cluster(embs, threshold=1.5).tolist(), [0, 0, 0, 0])   # merges all
        self.assertEqual(nt.cluster(embs, num_speakers=1).tolist(), [0, 0, 0, 0])
        self.assertEqual(nt.cluster(np.zeros((0, 3))).tolist(), [])

    def test_reconstruct_two_voices_over_overlapping_windows(self):
        step = nt.SpeakerModels.SEG_STEP
        def window(start_frame_of_change):
            act = np.zeros((589, 3), bool)
            act[:start_frame_of_change, 0] = True        # local voice 0 first ...
            act[start_frame_of_change:, 1] = True        # ... then local voice 1
            return act
        # the change is at ~5 s: frame 296 in the first window, frame 0 in the second one
        windows = [(0, window(296)), (step, window(0))]
        voice = {(0, 0): 0, (0, 1): 1, (1, 1): 1}
        turns = nt.reconstruct(windows, voice, 15 * RATE)
        self.assertEqual([t.speaker for t in turns], [0, 1])
        self.assertAlmostEqual(turns[0].start, 0.0, delta=0.05)
        self.assertAlmostEqual(turns[0].end, 5.0, delta=0.1)
        self.assertAlmostEqual(turns[1].start, 5.0, delta=0.1)
        self.assertAlmostEqual(turns[1].end, 15.0, delta=0.2)

    def test_reconstruct_drops_blips_and_closes_gaps(self):
        act = np.zeros((589, 3), bool)
        act[0:200, 0] = act[210:400, 0] = True           # 0.17 s gap: closed
        act[500:510, 1] = True                           # 0.17 s blip: dropped
        turns = nt.reconstruct([(0, act)], {(0, 0): 0, (0, 1): 1}, 10 * RATE)
        self.assertEqual(len(turns), 1)
        self.assertAlmostEqual(turns[0].end - turns[0].start, 400 * 270 / RATE, delta=0.05)


class NemotronParts(unittest.TestCase):
    """Nemotron 3 Diarization's numpy side: features, speaker cache, turns."""

    def test_features_in_blocks_are_those_of_one_pass(self):
        n = nt.Nemotron.__new__(nt.Nemotron)
        n.setup_features()
        x = noise(3.0, 0.1, 1) * np.sin(np.linspace(0, 40, 3 * RATE)).astype(np.float32)
        y = np.pad(np.concatenate([x[:1], x[1:] - 0.97 * x[:-1]]), 256)    # pre-emphasis, centred
        valid = x.size // 160
        idx = np.arange(512)[None, :] + 160 * np.arange(valid + 1)[:, None]
        whole = np.log((np.abs(np.fft.rfft(y[idx] * n.window)) ** 2) @ n.mel + 2 ** -24)
        whole[valid:] = 0.0                                                # the last frame is padding
        for first, count in ((0, 40), (123, 50), (valid - 20, 40)):
            got = n.features(x, first, count)
            want = np.zeros((count, 128))
            have = whole[first:first + count]
            want[:len(have)] = have
            np.testing.assert_allclose(got, want, atol=2e-4)
        self.assertTrue(np.allclose(n.window[56:456], np.hanning(400)) and not n.window[:56].any())

    def test_speaker_cache_fifo_and_compression(self):
        class Small(nt.SpeakerCache):
            LENGTH, FIFO, UPDATE = 24, 4, 6   # budget 2 frames a voice, one of them silence
        cache, rng = Small(), np.random.default_rng(0)
        silence = np.full(nt.Nemotron.HIDDEN, 7.0, np.float32)
        voices = [0, 1, 0, 1, 2]
        for step_no, voice in enumerate(voices):
            kept = cache.frames()
            chunk = rng.normal(size=(10, nt.Nemotron.HIDDEN)).astype(np.float32)
            chunk[:, 0] = step_no * 10 + np.arange(10)          # frame number, to follow them
            step = np.concatenate([kept, chunk, chunk[:2]])     # + 2 look-ahead frames
            probs = np.zeros((len(step), nt.Nemotron.VOICES), np.float32)
            probs[len(kept):, voice] = 0.9
            if kept.size:
                probs[:len(kept)] = cache.probs.max() if cache.compressed else 0.0
            cache.update(step, probs, silence, 10)
            self.assertLessEqual(len(cache.embeds), Small.LENGTH)
            self.assertLessEqual(len(cache.fifo), Small.FIFO)
            np.testing.assert_array_equal(cache.fifo[:, 0], chunk[-len(cache.fifo):, 0])
        self.assertTrue(cache.compressed)
        self.assertEqual(len(cache.embeds), Small.LENGTH)
        # one silence slot per voice, and the best frames of voices 0, 1, 2 grouped by voice
        self.assertEqual(int((cache.embeds[:, 1] == 7.0).sum()), nt.Nemotron.VOICES)
        kept = [int(f) for f, s in zip(cache.embeds[:, 0], cache.embeds[:, 1]) if s != 7.0]
        owner = [voices[f // 10] for f in kept]
        self.assertEqual(owner, sorted(owner))
        self.assertEqual(set(owner), {0, 1, 2})

    def test_voice_turns_numbered_by_first_words(self):
        p = np.zeros((500, 8), np.float32)
        p[100:200, 3] = 0.9       # voice 3 first: 1.0-2.0 s
        p[250:300, 3] = 0.6
        p[150:400, 5] = 0.7       # overlapping speech: voice 5 is the second
        p[450:460, 5] = 0.4       # under the threshold
        turns = nt.voice_turns(p)
        self.assertEqual([(t.speaker, round(t.start, 2), round(t.end, 2)) for t in turns],
                         [(0, 1.0, 2.0), (1, 1.5, 4.0), (0, 2.5, 3.0)])
        self.assertEqual(nt.voice_turns(np.zeros((10, 8))), [])

    def test_fewer_voices_and_fingerprint_turns(self):
        a, b = np.array([1.0, 0, 0]), np.array([0, 1.0, 0])
        turns = [Turn(0, 10, 0), Turn(10, 20, 1), Turn(20, 22, 2), Turn(22, 30, 0), Turn(30, 31, 3)]
        vecs = {0: (a, 4.0), 1: (b, 4.0), 2: (nt.unit(a + 0.2 * b), 2.0), 3: (a, 4.0)}
        two = nt.fewer_voices(turns, vecs, 2)
        self.assertEqual([t.speaker for t in two], [0, 1, 0, 0, 0])  # 3 (no vector) -> biggest, 2 -> like 0
        self.assertEqual(nt.fewer_voices(turns, vecs, 9), turns)
        # one person as two voices (0.98 alike) becomes one; 0.71 alike stay apart
        same = nt.same_people(turns, {0: (a, 4.0), 1: (b, 4.0), 2: (nt.unit(a + 0.2 * b), 2.0),
                                      3: (a, 4.0), 4: (nt.unit(a + b), 1.0)})
        self.assertEqual([t.speaker for t in same], [0, 1, 0, 0, 3])
        many = [Turn(i * 10, i * 10 + 2 + i % 3, i % 2) for i in range(60)]
        picked = nt.fingerprint_turns(many, list(range(60)), 4.0)
        for voice in (0, 1):
            seconds = [min(4.0, many[i].end - many[i].start) for i in picked if many[i].speaker == voice]
            self.assertGreaterEqual(sum(seconds), nt.FINGERPRINT_SECONDS)
            self.assertLess(sum(seconds) - seconds[-1], nt.FINGERPRINT_SECONDS + 4)
            length = lambda i: many[i].end - many[i].start
            mine = [i for i in range(60) if many[i].speaker == voice]
            self.assertGreaterEqual(min(length(i) for i in mine if i in picked),     # the longest ones
                                    max(length(i) for i in mine if i not in picked))

    def test_models_missing(self, tmp=Path(__file__).resolve().parent / ".work-speakers"):
        saved = nt.DIARIZATION_MODEL, nt.SEGMENTATION_MODEL, nt.EMBEDDING_MODEL
        try:
            shutil.rmtree(tmp, ignore_errors=True)
            tmp.mkdir()
            nt.DIARIZATION_MODEL, nt.SEGMENTATION_MODEL = tmp / "nemotron", tmp / "seg.onnx"
            nt.EMBEDDING_MODEL = tmp / "titanet.onnx"
            self.assertEqual(nt.speaker_models_missing(), [tmp / "titanet.onnx", tmp / "nemotron/encoder.xml"])
            nt.EMBEDDING_MODEL.write_bytes(b"x")
            nt.SEGMENTATION_MODEL.write_bytes(b"x")        # an install from before 0.6.0
            self.assertEqual(nt.speaker_models_missing(), [])
            nt.SEGMENTATION_MODEL.unlink()
            (tmp / "nemotron").mkdir()
            (tmp / "nemotron/encoder.xml").write_bytes(b"x")
            self.assertEqual(nt.speaker_models_missing(), [])
        finally:
            nt.DIARIZATION_MODEL, nt.SEGMENTATION_MODEL, nt.EMBEDDING_MODEL = saved
            shutil.rmtree(tmp, ignore_errors=True)


class Sentences(unittest.TestCase):
    def test_sentences_from_ctc_words(self):
        segs = nt.sentences(words("Привет.@0.0-0.4 Как@0.6-0.8 дела?@0.8-1.1 Это@1.3-1.5 я@1.5-1.6 "
                                  "после@3.5-3.8 паузы.@3.8-4.2 «Цитата.»@4.4-5.0 хвост@5.2-5.5"))
        self.assertEqual([s.text for s in segs],
                         ["Привет.", "Как дела?", "Это я", "после паузы.", "«Цитата.»", "хвост"])
        self.assertEqual((segs[2].start, segs[2].end), (1.3, 1.6))
        self.assertEqual(len(segs[3].words), 2)


class Hallucinations(unittest.TestCase):
    def test_stock_phrases_only(self):
        for text in ("Продолжение следует...", "Субтитры сделал DimaTorzok", "Thanks for watching!",
                     "Продолжение следует. Продолжение следует.", "СПАСИБО ЗА ПРОСМОТР!"):
            self.assertTrue(nt.hallucination(text), text)
        for text in ("Продолжение следует в четверг, после релиза.", "Спасибо!", "", "Thank you."):
            self.assertFalse(nt.hallucination(text), text)


class Loops(unittest.TestCase):
    def test_phrase_loop_in_a_segment(self):
        text = "Ну, непонятно. " + "Я в рейтинге. " * 40 + "Поговорим по параметрам."
        out = nt.collapse_repeats(Seg(0, 60, " ".join(text.split())))
        self.assertEqual(out.text, "Ну, непонятно. Я в рейтинге. Поговорим по параметрам.")

    def test_loop_with_word_timestamps(self):
        ws = words("Hi@0-0.5 there@0.5-1") + [Word(1 + i * 0.4, 1.3 + i * 0.4, w)
                                               for i, w in enumerate([" the", " end."] * 6)]
        out = nt.collapse_repeats(Seg(0, 6, "x", ws, "You"))
        self.assertEqual(out.text, "Hi there the end.")
        self.assertAlmostEqual(out.end, 1.7)
        self.assertEqual((len(out.words), out.speaker), (4, "You"))

    def test_real_repetition_stays(self):
        for text in ("Нет, нет, нет, так нельзя.", "Да. Да. Хорошо, хорошо.", "мы мы говорим"):
            self.assertEqual(nt.collapse_repeats(Seg(0, 5, text)).text, text)
        self.assertEqual(nt.collapse_repeats(Seg(0, 5, "the the the the the end")).text, "the end")

    def test_repeated_segments(self):
        segs = [Seg(0, 1, "Hello."), Seg(1, 2, "Thanks."), Seg(2, 3, "Thanks!"), Seg(3, 4, "Hello."),
                Seg(4, 5, "Я в рейтинге."), Seg(5, 6, "Я в рейтинге."), Seg(6, 7, "я в рейтинге"),
                Seg(7, 8, "Я в рейтинге."), Seg(8, 9, "Bye.")]
        self.assertEqual([s.text for s in nt.drop_repeated_segments(segs)],
                         ["Hello.", "Thanks.", "Thanks!", "Hello.", "Я в рейтинге.", "Bye."])


class Tidy(unittest.TestCase):
    def check(self, before, after):
        self.assertEqual(nt.tidy(before), after)

    def test_fillers(self):
        self.check("Э-э, я созванивалась с бренд-донором, а-а, на прошлой неделе, он сказал, что, "
                   "хмм, показал мне схему, по которой,м, вообще.",
                   "Я созванивалась с бренд-донором на прошлой неделе, он сказал, что показал мне "
                   "схему, по которой вообще.")
        self.check("Вот. А-а. И мы, э-э-э, начали. А..а, какую-то, хмм, условно.",
                   "Вот. И мы начали. Какую-то условно.")
        self.check("So, um, we start, uh, now.", "So we start now.")
        self.check("Э-э.", "")

    def test_repeats_and_stutters(self):
        self.check("То есть мы мы говорим, или или нет. Вот по функцио функционал.",
                   "То есть мы говорим, или нет. Вот по функционал.")
        self.check("И-и-и да, ответ: да.", "И да, ответ: да.")
        # (meant: a word said several times on purpose, "при приходе", millimetres)
        for same in ("Нет, нет, нет, так нельзя.", "Да да, очень очень хорошо.", "При приходе: 5 мм.",
                     "Вот вот.", "А здесь, а там."):
            self.check(same, same)

    def test_mixed_scripts_and_spacing(self):
        self.check("Так,oкей, ретеншн и реetention, Viкl LifeTime, одно FД.",
                   "Так, окей, ретеншн и reetention, Vikl LifeTime, одно FD.")
        self.check("Подожди,подожди. 5,3 темнее.. Он ушёл....", "Подожди, подожди. 5,3 темнее. Он ушёл...")
        self.check("—— Да, включил. — Включил?", "— Да, включил. — Включил?")

    def test_verbatim_and_outputs(self):
        segs = [Seg(0, 1, "Э-э, привет.", speaker="You"), Seg(1, 2, "А-а.", speaker="Person 1"),
                Seg(2, 3, "Мы мы начали.", speaker="You")]
        md = nt.meeting_md("T", None, 3, segs, {})
        self.assertIn("*[00:00]* Привет. Мы начали.", md)
        self.assertNotIn("Person 1", md)  # only a filler: nothing left of it
        try:
            nt.VERBATIM = True
            self.assertEqual(nt.tidy("Э-э, привет."), "Э-э, привет.")
        finally:
            nt.VERBATIM = False


class EnglishTerms(unittest.TestCase):
    """GigaAM's words with Whisper's English words put in (swap_english, english_terms)."""

    @staticmethod
    def timed(spec):  # "word@start-end ..." with Whisper's/GigaAM's leading spaces
        return words(spec)

    def test_garbled_terms_are_swapped(self):
        mine = self.timed("в@0-0.2 компании@0.2-0.8 Daita@0.9-1.2 Doh,@1.2-1.5 то@1.6-1.7 есть@1.7-2")
        theirs = self.timed("в@0-0.2 компании@0.2-0.8 Datadog,@0.9-1.5 то@1.6-1.7 есть@1.7-2")
        out, n = nt.swap_english(mine, theirs)
        self.assertEqual(("".join(w.text for w in out).strip(), n), ("в компании Datadog, то есть", 1))

    def test_cyrillic_spelling_and_russian_endings(self):
        mine = self.timed("на@0-0.2 ворнинги,@0.3-0.9 там,@1-1.3 не@1.4-1.5 знаю,@1.5-1.8 GPU-шки@1.9-2.6")
        theirs = self.timed("на@0-0.2 warning,@0.3-0.9 там,@1-1.3 не@1.4-1.5 знаю,@1.5-1.8 GPU@1.9-2.4")
        out, n = nt.swap_english(mine, theirs)
        self.assertEqual("".join(w.text for w in out).strip(), "на warning-и, там, не знаю, GPU-шки")
        self.assertEqual(n, 2)

    def test_pieces_join_and_unlike_words_stay(self):
        mine = self.timed("упомянул@0-0.6 OneOn-On.@0.7-1.4 Допустим@1.5-2")
        theirs = [Word(0.0, 0.6, " упомянул"), Word(0.7, 0.9, " one"), Word(0.9, 1.1, "-on"),
                  Word(1.1, 1.3, "-one"), Word(1.3, 1.4, "."), Word(1.5, 2.0, " Допустим")]
        out, _ = nt.swap_english(mine, theirs)
        self.assertEqual("".join(w.text for w in out).strip(), "упомянул one-on-one. Допустим")
        # (too unlike to swap: "ван-нлан" for "one-on-one" stays as GigaAM wrote it)
        out, n = nt.swap_english(self.timed("ван-нлан@0.7-1.4"), theirs[1:4])
        self.assertEqual(("".join(w.text for w in out).strip(), n), ("ван-нлан", 0))
        # nothing English-sounding where Whisper heard English: GigaAM stays
        out, n = nt.swap_english(self.timed("потом@0-0.5 мы@0.6-0.8"), self.timed("Wave@0-0.5 to@0.6-0.8"))
        self.assertEqual(("".join(w.text for w in out).strip(), n), ("потом мы", 0))

    def test_same_words_keep_gigaam_spelling(self):
        out, n = nt.swap_english(self.timed("в@0-0.2 Google@0.3-0.6 Play@0.6-0.9"),
                                 self.timed("в@0-0.2 google@0.3-0.6 play@0.6-0.9"))
        self.assertEqual(("".join(w.text for w in out).strip(), n), ("в Google Play", 0))

    def test_only_sentences_with_latin_go_to_whisper(self):
        class FakeWhisper:
            def __init__(self):
                self.calls = []

            def generate(self, samples, **kw):
                self.calls.append((samples.size / RATE, kw["language"]))
                w = type("W", (), {"start_ts": 0.3, "end_ts": 0.9, "word": " Datadog"})
                return type("R", (), {"words": [w]})
        segs = [Seg(0, 2, "Привет всем.", words("Привет@0-0.5 всем.@0.5-2")),
                Seg(10, 12, "Это Data Dog.", words("Это@10-10.2 Data@10.3-10.6 Dog.@10.6-10.9"))]
        fake = FakeWhisper()
        out, n, seconds = nt.english_terms(fake, np.zeros(20 * RATE, np.float32), segs, "ru")
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(fake.calls[0][1], "<|ru|>")
        self.assertAlmostEqual(seconds, 2 + 2 * nt.TERM_PAD)
        self.assertEqual([s.text for s in out], ["Привет всем.", "Это Datadog."])
        self.assertEqual(n, 1)


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

    def test_their_last_word_as_you_start_talking(self):
        # the volume test misses it (you are already loud there); the same word at the same moment is theirs
        theirs = [Seg(7, 10, "the budget is fine", words("the@7.0-7.3 budget@7.3-8.0 is@8.0-9.0 fine@9.0-9.95"),
                      "Person 1")]
        seg = Seg(9.9, 12, "fine yes I agree fine", words("fine@9.9-10.2 yes@10.3-10.6 I@10.6-10.8 agree@10.8-11.4 "
                                                         "fine@11.5-11.9"), "You")
        mic = self.mic.copy()
        mic[int(9.9 * RATE):10 * RATE] += noise(0.1, 0.1, 7)
        kept, cut = nt.drop_echo([seg], theirs, mic, self.remote)
        self.assertEqual(cut, 1)          # your own "fine" later on stays
        self.assertEqual([k.text for k in kept], ["yes I agree fine"])

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
            (tmp / "part-2.start").write_text("1760000100.25\n")
            mic, remote, parts = nt.read_meeting(tmp)
            self.assertEqual((mic.size, remote.size), (3 * RATE, 3 * RATE))
            self.assertEqual(float(remote[int(1.7 * RATE)]), 0.0)
            self.assertNotEqual(float(remote[int(2.1 * RATE)]), 0.0)
            self.assertEqual(parts, [nt.Part(0.0, 2.0, None), nt.Part(2.0, 1.0, 1760000100.25)])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_in_person_has_no_remote_tracks(self, tmp=Path(__file__).resolve().parent / ".work-speakers"):
        import wave
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir()
        try:
            for n in (1, 2):
                with wave.open(str(tmp / f"part-{n}-mic.wav"), "wb") as w:
                    w.setnchannels(1)
                    w.setsampwidth(2)
                    w.setframerate(RATE)
                    w.writeframes(b"\x00\x10" * RATE)
            mic, remote, parts = nt.read_meeting(tmp)
            self.assertEqual((mic.size, remote.size, len(parts)), (2 * RATE, 2 * RATE, 2))
            self.assertFalse(remote.any())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class MicOff(unittest.TestCase):
    """The call log of BMeet's Chrome extension -> when your microphone is left out."""
    T = 1_760_000_000.0

    def log(self, *spec):  # (seconds after T, call, live)
        return [(self.T + t, call, live) for t, call, live in spec]

    def test_muted_stretch(self):
        log = self.log((0, True, True), (5, True, True), (10, True, False), (15, True, False),
                       (20, True, False), (24, True, True), (29, True, True))
        parts = [nt.Part(0.0, 60.0, self.T - 2.0)]  # the recording started 2 s before the log
        # muted 10..24 s, minus LIVE_LEAD before the sound at 24 s; +2 s in the recording
        self.assertEqual(nt.muted_spans(log, parts), [(12.0, 26.0 - nt.LIVE_LEAD)])

    def test_across_a_pause(self):
        log = self.log((0, True, True), (10, True, False), (20, True, False), (30, True, True))
        # part 1: T-5..T+15, paused, part 2: T+25..T+45 (muted 10 s .. 30 s - LIVE_LEAD)
        parts = [nt.Part(0.0, 20.0, self.T - 5.0), nt.Part(20.0, 20.0, self.T + 25.0)]
        self.assertEqual(nt.muted_spans(log, parts), [(15.0, 20.0), (20.0, 25.0 - nt.LIVE_LEAD)])

    def test_lines_go_stale(self):  # the extension stopped talking: no longer muted after CALL_STALE
        log = self.log((0, True, True), (10, True, False))
        self.assertEqual(nt.muted_spans(log, [nt.Part(0.0, 100.0, self.T)]),
                         [(10.0, 10.0 + nt.CALL_STALE)])

    def test_no_call_or_never_live(self):
        parts = [nt.Part(0.0, 60.0, self.T)]
        self.assertEqual(nt.muted_spans(self.log((0, False, False), (30, False, False)), parts), [])
        # a call in which no sound was ever seen: the extension cannot see that microphone
        self.assertEqual(nt.muted_spans(self.log((0, True, False), (5, True, False)), parts), [])
        self.assertEqual(nt.muted_spans([], parts), [])

    def test_short_gaps_and_unplaced_parts(self):
        short = 10 + nt.LIVE_LEAD + nt.MIN_MUTED_SECONDS - 0.3  # muted for 0.3 s less than the minimum
        log = self.log((0, True, True), (10, True, False), (short, True, True))
        self.assertEqual(nt.muted_spans(log, [nt.Part(0.0, 60.0, self.T)]), [])
        log = self.log((0, True, True), (10, True, False), (30, True, True))
        self.assertEqual(nt.muted_spans(log, [nt.Part(0.0, 60.0, None)]), [])

    def test_read_call_log_and_silence(self, tmp=Path(__file__).resolve().parent / ".work-speakers"):
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir()
        try:
            (tmp / "call-mic.jsonl").write_text(
                '{"t": 20.5, "call": true, "live": false}\nnot json\n{"t": 10, "call": true, "live": true}\n')
            self.assertEqual(nt.read_call_log(tmp), [(10.0, True, True), (20.5, True, False)])
            self.assertEqual(nt.read_call_log(tmp / "missing"), [])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        x = np.ones(5 * RATE, np.float32)
        nt.silence(x, [(1.0, 2.5)])
        self.assertEqual(float(x[:RATE].min()), 1.0)
        self.assertEqual(float(x[RATE:int(2.5 * RATE)].max()), 0.0)
        self.assertEqual(float(x[int(2.5 * RATE):].min()), 1.0)


class YourVoice(unittest.TestCase):
    class Models:
        EMB_SECONDS = 4.0

        def __init__(self, vec):
            self.vec, self.pieces = vec, []

        def embed(self, pieces):
            self.pieces += pieces
            return np.array([self.vec for _ in pieces])

    def test_self_print_from_your_longest_lines(self):
        models = self.Models(np.array([3.0, 4.0]))
        mine = [Seg(i * 10.0, i * 10.0 + 6.0, "x") for i in range(6)] + [Seg(70.0, 71.0, "short")]
        fp = nt.self_print(models, np.zeros(80 * RATE, np.float32), mine)
        self.assertEqual(fp, [0.6, 0.8])
        self.assertEqual(len(models.pieces), 6)
        self.assertTrue(all(p.size == 4 * RATE for p in models.pieces))  # the middle 4 s
        # too little of your speech: nothing learned
        self.assertIsNone(nt.self_print(models, np.zeros(80 * RATE, np.float32), mine[:2]))
        # one long line (sentences Whisper joined): several 4 s pieces from its middle
        models.pieces = []
        self.assertEqual(nt.self_print(models, np.zeros(80 * RATE, np.float32), [Seg(10.0, 30.0, "long")]), [0.6, 0.8])
        self.assertEqual([p.size for p in models.pieces], [4 * RATE] * 4)

    def test_learn_self_keeps_the_latest_and_skips_strangers(self, tmp=Path(__file__).resolve().parent / ".work-speakers"):
        shutil.rmtree(tmp, ignore_errors=True)
        try:
            self.assertEqual(nt.load_self(tmp), [])
            self.assertTrue(nt.learn_self(tmp, [1.0, 0.0], "m1"))
            self.assertTrue(nt.learn_self(tmp, [0.9, 0.1], "m2"))
            self.assertFalse(nt.learn_self(tmp, [0.0, 1.0], "m3"))   # someone else
            self.assertTrue(nt.learn_self(tmp, [0.95, 0.05], "m2"))  # same meeting again: replaced
            self.assertEqual(nt.load_self(tmp), [[1.0, 0.0], [0.95, 0.05]])
            self.assertEqual((tmp / "you.json").stat().st_mode & 0o777, 0o600)
            for i in range(nt.MAX_SELF_PRINTS + 3):
                nt.learn_self(tmp, [1.0, 0.0], f"n{i}")
            self.assertEqual(len(nt.load_self(tmp)), nt.MAX_SELF_PRINTS)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
