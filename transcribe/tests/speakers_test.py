#!/usr/bin/env python3
"""Who speaks when, on real recordings: how much speech npu-transcribe's diarization gives to
the wrong person, and its diarization error rate (DER).

Russian: five podcasts with 2-3 people (4.1 h) of leshinsky/ru-youtube-diarization on Hugging
Face, with its hand-corrected labels (Apache-2.0; the audio is from YouTube, so it is only
fetched into WORKDIR for this local test, ~2.3 GB once). English with --ami: the 16 AMI test
meetings recorded by one microphone in the room (9 h, ~1 GB, CC-BY-4.0, from the AMI corpus
mirror), with the forced-alignment labels of nttcslab-sp/diar-forced-alignment.

"Wrong person": each labelled stretch of speech is given to a voice as npu-transcribe gives
words to speakers (the voice that talks most during it, else the nearest one within 30 s);
after the best one-to-one matching of voices to people, the share of speech under someone
else. DER: missed speech + false alarms + confusion, 10 ms frames, no collar, overlaps
counted (the Russian labels count pauses inside a phrase as speech, so their DER is high).

Measured 2026-10-13 (docs/npu-transcribe.md), wrong person / DER:
  Russian  Nemotron 2.1 % / 21.3 %    pyannote way (before 0.6.0) 8.1 % / 20.3 %
  AMI      Nemotron 8.4 % / 11.0 %    pyannote way 22.8 % / 42.3 %

Usage: speakers_test.py WORKDIR [--ami]      (run with the installer's venv Python)
Env:   NPU_DIARIZATION=pyannote  measure the way of before 0.6.0
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import itertools
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
RATE = 16_000
RU = "https://huggingface.co/datasets/leshinsky/ru-youtube-diarization/resolve/main/"
RU_PODCASTS = {"astrology": ("astrology.json", "clips/astrology.wav"),
               "movie_podcast": ("movie_podcast.json", "clips/movie_podcast.wav"),
               "yCX4MRy14zw": ("youtube-1.json", "clips/yCX4MRy14zw.opus"),
               "p0_iIC13bs8": ("youtube-1.json", "clips/p0_iIC13bs8.opus"),
               "nr3hmL9bcPU": ("youtube-2.json", "clips/nr3hmL9bcPU.opus")}
AMI = "https://groups.inf.ed.ac.uk/ami/AMICorpusMirror/amicorpus/{0}/audio/{0}.Array1-01.wav"
AMI_LABELS = "https://raw.githubusercontent.com/nttcslab-sp/diar-forced-alignment/master/AMI/test/{}.rttm"
AMI_TEST = [f"{m}{p}" for m in ("EN2002", "ES2004", "IS1009", "TS3003") for p in "abcd"]


def load_tool():
    loader = importlib.machinery.SourceFileLoader("npu_transcribe", str(HERE.parent / "npu-transcribe"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = mod
    loader.exec_module(mod)
    return mod


def fetch(url: str, path: Path) -> Path:
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        urllib.request.urlretrieve(url, path.with_suffix(".part"))
        path.with_suffix(".part").rename(path)
    return path


def decode(path: Path) -> np.ndarray:
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "f32le", "-ac", "1", "-ar", str(RATE), "-"],
                         check=True, capture_output=True).stdout
    return np.frombuffer(raw, np.float32)


def recordings(work: Path, ami: bool):
    """(name, audio file, [(start, end, person), ...]) of the chosen set."""
    if not ami:
        for name, (labels, clip) in RU_PODCASTS.items():
            task = next(t for t in json.loads(fetch(RU + labels, work / "ru" / labels).read_text())
                        if t["data"]["audio"] == clip)
            ref = [(r["value"]["start"], r["value"]["end"], r["value"]["labels"][0])
                   for r in task["annotations"][0]["result"] if r["type"] == "labels"]
            yield name, fetch(RU + clip, work / "ru" / Path(clip).name), ref
        return
    for name in AMI_TEST:
        rttm = fetch(AMI_LABELS.format(name), work / "ami" / f"{name}.rttm").read_text().split("\n")
        ref = [(float(p[3]), float(p[3]) + float(p[4]), p[7]) for p in (line.split() for line in rttm) if p]
        yield name, fetch(AMI.format(name), work / "ami" / f"{name}.wav"), ref


def best_match(weights: np.ndarray) -> list[tuple[int, int]]:
    """The one-to-one matching of rows (people) to columns (voices) with the most weight."""
    rows, cols = weights.shape
    if rows > cols:
        return [(r, c) for c, r in best_match(weights.T)]
    best, pairs = -1.0, []
    for perm in itertools.permutations(range(cols), rows):
        total = weights[np.arange(rows), perm].sum()
        if total > best:
            best, pairs = total, list(zip(range(rows), perm))
    return pairs


def wrong_person(ref, turns, speaker_at) -> tuple[float, float]:
    """(seconds under someone else, seconds labelled)"""
    people = sorted({p for _, _, p in ref})
    voices = sorted({t.speaker for t in turns})
    w = np.zeros((len(people), len(voices) + 1))
    for a, b, person in ref:
        voice = speaker_at(turns, a, b)
        w[people.index(person), voices.index(voice) if voice is not None else len(voices)] += b - a
    right = sum(w[r, c] for r, c in best_match(w[:, :len(voices)])) if voices else 0.0
    return w.sum() - right, w.sum()


def der(ref, turns, seconds: float) -> tuple[int, int, int, int]:
    """(missed, false alarm, confusion, speech) in 10 ms frames"""
    n = int(np.ceil(seconds * 100)) + 1
    people, voices = sorted({p for _, _, p in ref}), sorted({t.speaker for t in turns})
    r, h = np.zeros((n, len(people)), bool), np.zeros((n, len(voices)), bool)
    for a, b, p in ref:
        r[int(round(a * 100)):int(round(b * 100)), people.index(p)] = True
    for t in turns:
        h[int(round(t.start * 100)):int(round(t.end * 100)), voices.index(t.speaker)] = True
    pairs = best_match(r.T.astype(np.float32) @ h.astype(np.float32)) if voices else []
    correct = sum((r[:, i] & h[:, j]).astype(int) for i, j in pairs) if pairs else np.zeros(n, int)
    nr, nh = r.sum(1), h.sum(1)
    return (int(np.maximum(nr - nh, 0).sum()), int(np.maximum(nh - nr, 0).sum()),
            int((np.minimum(nr, nh) - correct).sum()), int(nr.sum()))


def main() -> int:
    args = [a for a in sys.argv[1:] if a != "--ami"]
    if len(args) != 1:
        print(__doc__.split("Usage:")[1].strip(), file=sys.stderr)
        return 2
    work = Path(args[0])
    nt = load_tool()
    models = nt.SpeakerModels(["NPU", "CPU"])
    how = "Nemotron" if models.nemotron else "pyannote"
    totals, wrong, labelled, seconds, wall = np.zeros(4, int), 0.0, 0.0, 0.0, 0.0
    for name, audio, ref in recordings(work, "--ami" in sys.argv):
        x = decode(audio)
        t0 = time.monotonic()
        turns, _, _ = nt.diarize(models, x)
        wall += time.monotonic() - t0
        seconds += x.size / RATE
        bad, total = wrong_person(ref, turns, nt.speaker_at)
        d = np.array(der(ref, turns, x.size / RATE))
        wrong, labelled, totals = wrong + bad, labelled + total, totals + d
        print(f"{name:16s} {x.size / RATE / 60:5.1f} min  wrong person {bad / total:6.1%}  DER {d[:3].sum() / d[3]:6.1%}"
              f"  people {len({p for *_, p in ref})} -> voices {len({t.speaker for t in turns})}", flush=True)
    print(f"{how} on {models.device}: wrong person {wrong / labelled:.1%}, DER {totals[:3].sum() / totals[3]:.1%} "
          f"(missed {totals[0] / totals[3]:.1%}, false alarm {totals[1] / totals[3]:.1%}, "
          f"confusion {totals[2] / totals[3]:.1%}); {seconds / 60:.0f} min in {wall:.0f} s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
