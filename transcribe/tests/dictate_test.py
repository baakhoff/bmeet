#!/usr/bin/env python3
"""Live dictation (npu-transcribe --dictate-file, as bmeet-dictate uses it) on real speech, against
dictating the way voxtype does it today and against the same model on the whole recording.

The dictations: in Russian, the Podlodka podcast clips (bond005/podlodka_speech on Hugging Face,
test and validation splits: 20 clips of 8-59 s; its card states no license, so they are only
fetched for this local test, never kept in the repository) and two of ~90 s made of five and four
of them in a row; in English, LibriSpeech test-clean (CC-BY-4.0) through the datasets-server: the
utterances of 4 s and more of six readers, and per reader one of ~45-60 s of their utterances in
a row. Fetched into WORKDIR once.

Each dictation is said into npu-transcribe --dictate-file --simulate: its clock skips the waiting
but not the work, so the text comes as late as it would while you speak. Measured:
  word errors   of the final text (lower case, no punctuation, ё = е), also for the dictations
                longer than 30 s; next to them the same model on the whole recording, and
                voxtype today (whisper-small on the whole recording, as its OpenVINO engine does)
  word -> text  how long after a word is said it is first shown (its end, from the model's word
                times on the whole recording, to the first text that has it)
  sentence      how long after its last word a sentence becomes final (what pasting finished
                sentences would wait for)
  stop -> text  from the end of the audio to the final text
  CPU           of the engine during the dictations, per minute of speech (model loading not counted)

Usage: dictate_test.py WORKDIR [--language ru|en] [--model turbo|small] [--partial SECONDS]
                       [--no-baseline] [--only REGEX]
Must run with the Python that has openvino-genai: NPU_PYTHON (default: the installer's venv)
dictate_test.py ... Exit status 0 = within the limits below. Writes only below WORKDIR (plus the
OpenVINO compile cache).
"""
from __future__ import annotations

import argparse
import collections
import difflib
import importlib.machinery
import importlib.util
import json
import os
import re
import resource
import statistics
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOOL = HERE.parent / "npu-transcribe"
RATE = 16_000
PODLODKA = "https://datasets-server.huggingface.co/rows?dataset=bond005/podlodka_speech&config=default"
LIBRISPEECH = ("https://datasets-server.huggingface.co/rows?dataset=openslr/librispeech_asr"
               "&config=clean&split=test&length=100&offset=")
GAP = 0.6           # seconds of quiet between the clips of a long dictation
# Limits (exit status 1 beyond them), per engine: GigaAM (Russian) / Whisper (other languages)
LIMITS = {"gigaam": {"wer_extra": 1.0, "long_extra": 2.0, "delay": 1.0, "stop": 0.5, "cpu": 1.0},
          "whisper": {"wer_extra": 1.0, "long_extra": 2.0, "delay": 2.0, "stop": 1.5, "cpu": 4.0}}


def load_tool():
    loader = importlib.machinery.SourceFileLoader("npu_transcribe", str(TOOL))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = mod
    loader.exec_module(mod)
    return mod


def fetch_json(url: str, tries: int = 5):
    for attempt in range(tries):   # (the datasets-server sometimes answers 500)
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                return json.load(r)
        except OSError:
            if attempt == tries - 1:
                raise
            time.sleep(5)


def fetch(work: Path, language: str) -> list[dict]:
    folder = work / language
    index = folder / "clips.json"
    if index.exists():
        return json.loads(index.read_text())
    folder.mkdir(parents=True, exist_ok=True)
    clips: list[dict] = []
    if language == "ru":
        seen = set()
        for split in ("test", "validation"):
            for row in fetch_json(f"{PODLODKA}&split={split}&offset=0&length=100")["rows"]:
                text = row["row"]["transcription"]
                if text in seen:   # (some clips are in both splits)
                    continue
                seen.add(text)
                path = folder / f"{split}-{row['row_idx']:03d}.wav"
                urllib.request.urlretrieve(row["row"]["audio"][0]["src"], path)
                clips.append({"file": str(path), "text": text, "speaker": "podlodka"})
    else:
        rows = []
        for offset in (0, 400, 800, 1200, 1600, 2000):
            rows += fetch_json(LIBRISPEECH + str(offset))["rows"]
        readers: dict[int, list] = collections.defaultdict(list)
        for row in rows:
            readers[row["row"]["speaker_id"]].append(row)
        for reader in sorted(readers, key=lambda r: -len(readers[r]))[:6]:
            for row in readers[reader][:12]:
                path = folder / f"{row['row_idx']}.flac"
                urllib.request.urlretrieve(row["row"]["audio"][0]["src"], path)
                clips.append({"file": str(path), "text": row["row"]["text"], "speaker": reader})
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


def cpu() -> float:
    r = resource.getrusage(resource.RUSAGE_SELF)
    return r.ru_utime + r.ru_stime


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("work", type=Path)
    p.add_argument("--language", default="ru", choices=("ru", "en"))
    p.add_argument("--model", default="turbo", choices=("turbo", "small"), help="Whisper (not Russian)")
    p.add_argument("--partial", type=float, default=None, help="redraft after this many seconds (default: the tool's)")
    p.add_argument("--no-baseline", action="store_true", help="skip the whole-recording comparisons")
    p.add_argument("--only", help="dictations whose name matches")
    args = p.parse_args()
    work = args.work.resolve()
    nt = load_tool()
    import numpy as np

    clips = fetch(work, args.language)
    dictations: list[tuple[str, list[dict]]] = []
    if args.language == "ru":
        dictations = [(Path(c["file"]).stem, [c]) for c in clips] + [("long-a", clips[0:5]), ("long-b", clips[10:14])]
    else:
        by: dict = collections.defaultdict(list)
        for c in clips:
            by[c["speaker"]].append(c)
        for reader, cs in by.items():
            dictations += [(Path(c["file"]).stem, [c]) for c in cs if nt.probe_duration(Path(c["file"])) >= 4]
            group, seconds = [], 0.0
            for c in cs:
                group.append(c)
                seconds += nt.probe_duration(Path(c["file"]))
                if seconds >= 45:
                    break
            dictations.append((f"long-{reader}", group))
    if args.only:
        dictations = [d for d in dictations if re.search(args.only, d[0])]

    audio_dir = work / f"dictations-{args.language}"
    audio_dir.mkdir(exist_ok=True)
    for name, group in dictations:   # 16 kHz WAVs: a quiet start, the clips with GAP between
        path = audio_dir / f"{name}.wav"
        if not path.exists():
            parts = [np.zeros(int(0.3 * RATE), np.float32)]
            for c in group:
                parts += [nt.decode_all(Path(c["file"])), np.zeros(int(GAP * RATE), np.float32)]
            x = np.concatenate(parts[:-1])
            import wave
            with wave.open(str(path), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(RATE)
                w.writeframes((np.clip(x, -1, 1) * 32767).astype(np.int16).tobytes())

    # Word times (for word -> text) and the whole-recording texts, in this process.
    nt.VERBATIM = False
    engine_kind = "gigaam" if args.language == "ru" else "whisper"
    cache = work / f"reference-{args.language}-{args.model}.json"
    ref = json.loads(cache.read_text()) if cache.exists() else {}
    missing = [n for n, _ in dictations if n not in ref]
    if missing:
        devices = ["NPU"]
        if engine_kind == "gigaam":
            model = nt.GigaAM(devices)
        else:
            model = nt.Whisper(nt.MODELS[args.model], devices, word_timestamps=True)
        for name in missing:
            x = nt.decode_all(audio_dir / f"{name}.wav")
            words, texts = [], []
            for start, block in nt.array_blocks(x):
                if engine_kind == "gigaam":
                    found = [nt.Word(start + w.start, start + w.end, w.text) for w in model.words(block)]
                    texts += [s.text for s in nt.sentences(found)]
                else:
                    r = model.generate(block, language=f"<|{args.language}|>", task="transcribe",
                                       return_timestamps=True, word_timestamps=True)
                    found = [nt.Word(start + float(w.start_ts), start + float(w.end_ts), w.word) for w in (r.words or [])]
                    texts.append(r.texts[0] if r else "")
                words += [[w.start, w.end, w.text] for w in found]
            ref[name] = {"words": words, "whole": nt.tidy(" ".join(texts))}
        del model
        cache.write_text(json.dumps(ref, ensure_ascii=False))
    voxtype_cache = work / f"voxtype-{args.language}.json"
    today = json.loads(voxtype_cache.read_text()) if voxtype_cache.exists() else {}
    if not args.no_baseline and any(n not in today for n, _ in dictations):
        small = nt.Whisper(nt.MODELS["small"], ["NPU"])
        small.generate(np.zeros(RATE, np.float32), language=f"<|{args.language}|>", task="transcribe")
        for name, _ in dictations:
            if name in today:
                continue
            x = nt.decode_all(audio_dir / f"{name}.wav")
            c0, t0 = cpu(), time.monotonic()
            r = small.generate(x, language=f"<|{args.language}|>", task="transcribe")
            today[name] = {"text": r.texts[0] if r else "", "stop": time.monotonic() - t0, "cpu": cpu() - c0}
        del small
        voxtype_cache.write_text(json.dumps(today, ensure_ascii=False))

    python = os.environ.get("NPU_PYTHON") or sys.executable
    totals = collections.Counter()
    delays: list[float] = []
    sentence_waits: list[float] = []
    stops: list[float] = []
    today_stops: list[float] = []
    print(f"{'dictation':>12} {'s':>5} {'live':>6} {'whole':>6} {'today':>6} {'cpu ms':>7} {'stop s':>6}")
    for name, group in dictations:
        wav = audio_dir / f"{name}.wav"
        cmd = [python, "-I", str(TOOL), "--dictate-file", str(wav), "--simulate", "--language", args.language]
        if engine_kind == "whisper":
            cmd += ["--model", args.model]
        if args.partial:
            cmd += ["--partial", str(args.partial)]
        env = dict(os.environ, NPU_TRANSCRIBE_HISTORY=str(work / "history.jsonl"))
        run = subprocess.run(cmd, capture_output=True, text=True, env=env)
        if run.returncode != 0:
            print(f"FAIL: {name}: exit {run.returncode}: {run.stderr.strip().splitlines()[-1:]}")
            return 1
        events = [json.loads(line) for line in run.stdout.splitlines() if line.strip()]
        done = events[-1]
        seconds = done["audio"]
        reference = norm(" ".join(c["text"] for c in group))
        final = norm(done["text"])
        e_live = edits(reference, final)
        e_whole = edits(reference, norm(ref[name]["whole"]))
        e_today = edits(reference, norm(today[name]["text"])) if name in today else 0
        totals.update(audio=seconds, words=len(reference), live=e_live, whole=e_whole, today=e_today,
                      cpu=done["cpu"], passes=done["passes"], updates=len(events) - 1)
        if seconds > 30:
            totals.update(long_words=len(reference), long_live=e_live, long_whole=e_whole, long_today=e_today)
        stop = done["at"] - seconds
        stops.append(stop)
        if name in today:
            today_stops.append(today[name]["stop"])
        # word -> text: final word i, first shown in text event k at events[k]["at"]
        seen: list[float | None] = [None] * len(final)
        for ev in events[:-1] + [dict(done, final=done["text"], draft="")]:
            shown = norm(f"{ev.get('final', '')} {ev.get('draft', '')}")
            for a, _, n in difflib.SequenceMatcher(None, final, shown, autojunk=False).get_matching_blocks():
                for i in range(a, a + n):
                    if seen[i] is None:
                        seen[i] = ev["at"]
        timed = ref[name]["words"]
        said = [norm(w[2])[0] if norm(w[2]) else "" for w in timed]
        end_of = {}
        for a, b, n in difflib.SequenceMatcher(None, final, said, autojunk=False).get_matching_blocks():
            for k in range(n):
                end_of[a + k] = timed[b + k][1]
                if seen[a + k] is not None:
                    delays.append(seen[a + k] - timed[b + k][1])
        # sentence: when the final text grows while you go on talking (not at the stop), its new last
        # word was said at end_of[...]
        before = 0
        for ev in events[:-1]:
            if ev["audio"] >= seconds - 0.01:
                break
            now = len(norm(ev.get("final", "")))
            if now > before and (now - 1) in end_of:
                sentence_waits.append(ev["at"] - end_of[now - 1])
            before = max(before, now)
        print(f"{name:>12} {seconds:5.1f} {100 * e_live / len(reference):5.1f}% {100 * e_whole / len(reference):5.1f}% "
              f"{100 * e_today / len(reference):5.1f}% {1000 * done['cpu']:7.0f} {stop:6.2f}")
    q = lambda xs, f: sorted(xs)[min(len(xs) - 1, int(f * len(xs)))] if xs else float("nan")
    wer = lambda k, n="words": 100 * totals[k] / max(1, totals[n])
    minutes = totals["audio"] / 60
    label = "GigaAM v3" if engine_kind == "gigaam" else f"Whisper {args.model}"
    print(f"== {args.language}, {label}, redraft every {args.partial or 'default'} s: {len(dictations)} dictations, "
          f"{minutes:.1f} min")
    print(f"   word errors: live {wer('live'):.2f} %, the same model on the whole recording {wer('whole'):.2f} %, "
          f"voxtype today (whisper-small) {wer('today'):.2f} %")
    if totals["long_words"]:
        print(f"   dictations over 30 s: live {wer('long_live', 'long_words'):.2f} %, whole "
              f"{wer('long_whole', 'long_words'):.2f} %, today {wer('long_today', 'long_words'):.2f} %")
    print(f"   word -> text: median {q(delays, .5):.2f} s, 90 % within {q(delays, .9):.2f} s; sentence final "
          f"{q(sentence_waits, .5):.2f} s after its last word (90 %: {q(sentence_waits, .9):.2f} s), "
          f"{60 * len(sentence_waits) / max(1, totals['audio']):.1f} per minute")
    print(f"   stop -> text: median {statistics.median(stops):.2f} s, max {max(stops):.2f} s"
          + (f" (today: median {statistics.median(today_stops):.2f} s, max {max(today_stops):.2f} s)" if today_stops else ""))
    print(f"   CPU {totals['cpu'] / minutes:.2f} s per minute of speech, {totals['passes'] / minutes:.0f} passes and "
          f"{totals['updates'] / minutes:.0f} text updates per minute")
    lim = LIMITS[engine_kind]
    problems = []
    if wer("live") > wer("whole") + lim["wer_extra"]:
        problems.append("live word errors")
    if totals["long_words"] and wer("long_live", "long_words") > wer("long_whole", "long_words") + lim["long_extra"]:
        problems.append("word errors over 30 s")
    if q(delays, .5) > lim["delay"]:
        problems.append("word -> text")
    if statistics.median(stops) > lim["stop"]:
        problems.append("stop -> text")
    if totals["cpu"] / minutes > lim["cpu"]:
        problems.append("CPU")
    print("PASS" if not problems else f"FAIL: {', '.join(problems)} beyond the limits ({lim})")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
