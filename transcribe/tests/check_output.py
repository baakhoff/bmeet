#!/usr/bin/env python3
"""Validate an npu-transcribe .srt file and score it against a reference text.

Usage: check_output.py FILE.srt [REFERENCE.txt]
Checks: cue numbers 1..N, "HH:MM:SS,mmm --> HH:MM:SS,mmm" timing lines,
start <= end, cues in chronological order, no empty cue text.
With a reference it also prints the word error rate (WER, lower is better)
after lower-casing, dropping punctuation and spelling digits as written.
Exit code 1 if the SRT is invalid.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

TIME = re.compile(r"^(\d{2}):([0-5]\d):([0-5]\d),(\d{3}) --> (\d{2}):([0-5]\d):([0-5]\d),(\d{3})$")


def seconds(h: str, m: str, s: str, ms: str) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000


def check_srt(text: str) -> tuple[list[str], list[str]]:
    """Return (errors, cue texts)."""
    errors: list[str] = []
    cues: list[str] = []
    last_start = -1.0
    blocks = [b for b in text.replace("\r\n", "\n").strip().split("\n\n") if b.strip()]
    for n, block in enumerate(blocks, 1):
        lines = block.split("\n")
        if len(lines) < 3:
            errors.append(f"cue {n}: expected number, timing and text, got {lines!r}")
            continue
        if lines[0] != str(n):
            errors.append(f"cue {n}: number is {lines[0]!r}")
        m = TIME.match(lines[1])
        if not m:
            errors.append(f"cue {n}: bad timing line {lines[1]!r}")
            continue
        start, end = seconds(*m.groups()[:4]), seconds(*m.groups()[4:])
        if end < start:
            errors.append(f"cue {n}: ends before it starts")
        if start < last_start:
            errors.append(f"cue {n}: starts before the previous cue")
        last_start = start
        body = " ".join(lines[2:]).strip()
        if not body:
            errors.append(f"cue {n}: empty text")
        cues.append(body)
    if not blocks:
        errors.append("no cues")
    return errors, cues


def words(text: str) -> list[str]:
    # Whisper writes "200GB" / "30 days"; the reference spells numbers out.
    for digits, spelled in (("200", "two hundred "), ("30", "thirty ")):
        text = text.replace(digits, spelled)
    return re.findall(r"[\w']+", text.lower().replace("-", " ").replace("ё", "е"))


def wer(ref: list[str], hyp: list[str]) -> float:
    """Word-level Levenshtein distance divided by the reference length."""
    prev = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        cur = [i] + [0] * len(hyp)
        for j, h in enumerate(hyp, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (r != h))
        prev = cur
    return prev[-1] / max(1, len(ref))


def main() -> int:
    if len(sys.argv) not in (2, 3):
        print(__doc__, file=sys.stderr)
        return 2
    errors, cues = check_srt(Path(sys.argv[1]).read_text(encoding="utf-8"))
    status = "valid" if not errors else f"INVALID ({len(errors)} problems)"
    line = f"{sys.argv[1]}: {len(cues)} cues, SRT {status}"
    if len(sys.argv) == 3:
        ref = words(Path(sys.argv[2]).read_text(encoding="utf-8"))
        line += f", WER {100 * wer(ref, words(' '.join(cues))):.1f}%"
    print(line)
    for e in errors[:20]:
        print("  " + e)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
