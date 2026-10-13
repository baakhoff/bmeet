#!/usr/bin/env python3
"""English terms in Russian speech: GigaAM alone against GigaAM with Whisper's English words
(npu-transcribe's default for Russian), on real Russian IT talk.

Downloads the Podlodka podcast clips (bond005/podlodka_speech on Hugging Face: 87 unique clips,
33 min; its card states no license, so they are only fetched for this local test, never kept
in the repository) through the datasets-server into WORKDIR once, transcribes them with
npu-transcribe twice (--no-english-terms, then the default), and prints the word error rate
and how many of the 98 English terms (written in Latin letters in the references) came out
right. Measured 2026-10-13 (docs/npu-transcribe.md): 7.05 % / 22 % without, 6.52 % / 54 % with.

Usage: english_terms_test.py WORKDIR
Env:   NPU_PYTHON  Python with openvino-genai (default: the installer's venv)
"""
from __future__ import annotations

import collections
import json
import os
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOOL = HERE.parent / "npu-transcribe"
API = "https://datasets-server.huggingface.co/rows?dataset=bond005/podlodka_speech&config=default"


def fetch(work: Path) -> list[dict]:
    index = work / "clips.json"
    if index.exists():
        return json.loads(index.read_text())
    (work / "audio").mkdir(parents=True, exist_ok=True)
    clips, seen = [], set()
    for split in ("train", "validation", "test"):
        with urllib.request.urlopen(f"{API}&split={split}&offset=0&length=100", timeout=60) as r:
            rows = json.load(r)["rows"]
        for row in rows:
            text = row["row"]["transcription"]
            if text in seen:  # (some clips are in two splits)
                continue
            seen.add(text)
            path = work / "audio" / f"{split}-{row['row_idx']:03d}.wav"
            urllib.request.urlretrieve(row["row"]["audio"][0]["src"], path)
            clips.append({"file": str(path), "text": text})
    index.write_text(json.dumps(clips, ensure_ascii=False))
    return clips


def norm(text: str) -> list[str]:
    t = re.sub(r"[«»\"“”„()\[\]:;,.!?…—–]", " ", text.lower().replace("ё", "е"))
    return re.sub(r"(?<!\w)-|-(?!\w)", " ", t).split()


def edits(a: list[str], b: list[str]) -> int:
    d = list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        prev, d[0] = d[0], i
        for j in range(1, len(b) + 1):
            prev, d[j] = d[j], min(d[j] + 1, d[j - 1] + 1, prev + (a[i - 1] != b[j - 1]))
    return d[len(b)]


def score(pairs: list[tuple[str, str]]) -> tuple[float, int, int]:
    """(WER %, English terms right, English terms): a term counts when the hypothesis has a
    Latin word starting like it ("one-on-one-ы" ~ "one-on-one")."""
    errors = words = right = terms = 0
    for ref, hyp in pairs:
        r, h = norm(ref), norm(hyp)
        errors, words = errors + edits(r, h), words + len(r)
        want = collections.Counter(w for w in r if re.search(r"[a-z]", w))
        have = collections.Counter(w for w in h if re.search(r"[a-z]", w))
        terms += sum(want.values())
        for w, k in want.items():
            stem = w.split("-")[0] if len(w.split("-")[0]) >= 3 else w
            right += min(k, sum(v for x, v in have.items() if x == w or x.startswith(stem[:max(4, len(stem) - 2)])))
    return 100 * errors / max(words, 1), right, terms


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    work = Path(sys.argv[1]).resolve()
    python = os.environ.get("NPU_PYTHON") or str(Path.home() / ".local/share/bmeet/venv/bin/python")
    clips = fetch(work)
    env = dict(os.environ, NPU_TRANSCRIBE_HISTORY=str(work / "history.jsonl"))
    for name, flags in (("GigaAM alone", ["--no-english-terms"]), ("with Whisper's English terms", [])):
        out = work / ("out-" + ("off" if flags else "on"))
        run = subprocess.run([python, "-I", str(TOOL), "--language", "ru", "--format", "txt", "--overwrite",
                              "--out", str(out), *flags, *(c["file"] for c in clips)],
                             env=env, capture_output=True, text=True)
        if run.returncode:
            print(run.stderr[-2000:])
            return 1
        pairs = [(c["text"], (out / (Path(c["file"]).stem + ".txt")).read_text(encoding="utf-8")) for c in clips]
        wer, right, terms = score(pairs)
        print(f"{name:30s} WER {wer:.2f} %, English terms {right}/{terms} ({100 * right / max(terms, 1):.0f} %)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
