#!/usr/bin/env python3
"""End-to-end test of `npu-transcribe --meeting` (BMeet's speaker pass) on a synthetic call.

Builds a meeting recording like bmeet-meeting-audio writes it (two parts: there is a
pause in the middle) from espeak-ng voices, without recording anything:
  system audio  three people taking turns: espeak voices f4 and m3, and m7 played back
                1.3x slower (deeper). espeak's own male variants are closer to each other
                than real people are (TitaNet similarity m3/m7 ~0.57; real voices ~0.1)
  microphone    "you" (voice m1) in the gaps, plus the system audio leaking in at
                -20 dB as from laptop speakers (--headphones: no leak)
then runs the speaker pass and checks the Markdown: three people found, every sentence
under the label of whoever said it (each voice has its own sentences), and none of the
other side's words under "You".

--remember: remembered voices. The first meeting runs with --voices; its people are then
named as the BMeet app would (voices.json from the meeting's fingerprints), and a second
meeting with other sentences, in which the people speak in another order, must come out
as Anna, Boris and Viktor.

Usage: meeting_test.py WORKDIR [--language ru|en] [--headphones] [--remember]
Env:   NPU_PYTHON  Python with openvino-genai + sherpa-onnx (default: the installer's venv)
       NPU_SPEAKER_THRESHOLD  default 0.7 here (synthetic voices, see main())
Exit status 0 = passed. Writes only below WORKDIR (plus the OpenVINO compile cache).
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
import shutil
import subprocess
import sys
import wave
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
TOOL = HERE.parent / "npu-transcribe"
TTS = HERE.parent.parent / "voxtype/tools/tts_clip.py"
RATE = 16_000
VOICES = {"Anna": ("+f4", 1.0), "Boris": ("+m3", 1.0), "Viktor": ("+m7", 1.3), "You": ("+m1", 1.0)}

# Our own text. Each voice says only its own sentences, so a sentence identifies its speaker.
SCRIPT = {
    "ru": [
        ("Anna", "Всем привет. Давайте начнём с обзора бюджета на следующий квартал."),
        ("Boris", "Спасибо, Анна. Расходы на серверы выросли почти на двадцать процентов."),
        ("You", "Я предлагаю перенести часть нагрузки на новый кластер."),
        ("Viktor", "Новый кластер будет готов только в конце ноября."),
        ("Anna", "Тогда нам нужно временное решение до конца месяца."),
        ("You", "Я подготовлю сравнение вариантов к пятнице."),
        ("Boris", "Хорошо. Ещё я хотел обсудить дежурства на праздники."),
        ("Viktor", "Я могу взять первую неделю января."),
        ("Anna", "Отлично, запишу это в протокол."),
        ("You", "Спасибо всем, на этом можно заканчивать."),
    ],
    "en": [
        ("Anna", "Hello everyone. Let's start with the budget for the next quarter."),
        ("Boris", "Thanks, Anna. Server costs went up by almost twenty percent."),
        ("You", "I suggest we move part of the load to the new cluster."),
        ("Viktor", "The new cluster will only be ready at the end of November."),
        ("Anna", "Then we need a temporary solution until the end of the month."),
        ("You", "I will prepare a comparison of the options by Friday."),
        ("Boris", "Good. I also wanted to talk about the holiday on-call schedule."),
        ("Viktor", "I can take the first week of January."),
        ("Anna", "Great, I will write that down in the minutes."),
        ("You", "Thank you all, we can finish here."),
    ],
}


# The second meeting of --remember: new sentences, Viktor speaks first, Anna last.
SCRIPT_2 = {
    "ru": [
        ("Viktor", "Доброе утро. Сервер в Амстердаме снова перегружен."),
        ("You", "Сколько времени займёт перенос данных?"),
        ("Boris", "Примерно три дня, если начать в субботу."),
        ("Viktor", "Тогда я предупрежу клиентов заранее."),
        ("Boris", "И не забудьте обновить документацию."),
        ("Anna", "Я возьму документацию на себя."),
        ("You", "Отлично, договорились."),
        ("Anna", "До встречи в четверг."),
    ],
    "en": [
        ("Viktor", "Good morning. The server in Amsterdam is overloaded again."),
        ("You", "How long will it take to move the data?"),
        ("Boris", "About three days if we start on Saturday."),
        ("Viktor", "Then I will warn the customers in advance."),
        ("Boris", "And please do not forget to update the documentation."),
        ("Anna", "I will take care of the documentation."),
        ("You", "Great, we have a deal."),
        ("Anna", "See you all on Thursday."),
    ],
}


def load_tts():
    loader = importlib.machinery.SourceFileLoader("tts_clip", str(TTS))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def speak(tts, voice: str, text: str, slower: float = 1.0) -> np.ndarray:
    """16 kHz speech; slower > 1 plays it back slower, which lowers the voice."""
    pcm, rate = tts.synth(voice, text)
    x = np.frombuffer(pcm, np.int16).astype(np.float32) / 32768
    step = rate / RATE / slower  # espeak-ng speaks at 22050 Hz
    return np.interp(np.arange(0, x.size, step), np.arange(x.size), x).astype(np.float32)


def write_wav(path: Path, x: np.ndarray) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes((np.clip(x, -1, 1) * 32767).astype(np.int16).tobytes())


def make(folder: Path, lines: list[tuple[str, str]], language: str, headphones: bool) -> None:
    tts = load_tts()
    rng = np.random.default_rng(7)
    folder.mkdir(parents=True, exist_ok=True)
    for part, chunk in ((1, lines[:5]), (2, lines[5:])):
        remote, mic = [], []
        for who, text in chunk:
            variant, slower = VOICES[who]
            x = speak(tts, language + variant, text, slower)
            gap = np.zeros(int(rng.uniform(0.4, 1.0) * RATE), np.float32)
            remote += [gap, np.zeros_like(x) if who == "You" else x]
            mic += [gap, x if who == "You" else np.zeros_like(x)]
        r, m = np.concatenate(remote), np.concatenate(mic)
        if not headphones:  # the speakers' sound reaches the microphone 30 ms later, -20 dB
            m = m + np.concatenate([np.zeros(480, np.float32), r[:-480]]) * 0.1
        m = m + rng.normal(0, 0.002, m.size).astype(np.float32)  # room noise
        write_wav(folder / f"part-{part}-remote.wav", r)
        write_wav(folder / f"part-{part}-mic.wav", m)
    (folder / "meeting.json").write_text(json.dumps({"title": f"Test meeting ({language})"}))


def tokens(text: str) -> list[str]:
    return re.findall(r"\w+", text.lower())


def score(md: str, lines: list[tuple[str, str]], named: bool = False) -> tuple[bool, list[str], dict]:
    """Which label each word ended up under, against who said it. Only words of 4+ letters
    that one person says are counted, matched loosely (Whisper misspells synthetic speech).
    named: the people must be labelled with their own names (remembered voices)."""
    owner: dict[str, set[str]] = collections.defaultdict(set)
    for who, text in lines:
        for word in tokens(text):
            if len(word) >= 4:
                owner[word].add(who)
    vocabulary = list(owner)
    votes: collections.Counter = collections.Counter()
    label = None
    for line in md.splitlines():
        if line.startswith("### "):
            label = line[4:].strip()
        elif line.startswith("*[") and label:
            for word in tokens(line.split("*", 2)[2]):
                close = owner.get(word) or (owner[m[0]] if len(word) >= 4 and (
                    m := difflib.get_close_matches(word, vocabulary, n=1, cutoff=0.8)) else set())
                if len(close) == 1:
                    votes[(label, next(iter(close)))] += 1
    labels = sorted({lab for lab, _ in votes})
    main = {lab: max(VOICES, key=lambda w: votes[(lab, w)]) for lab in labels}
    total = sum(votes.values())
    right = sum(votes[(lab, main[lab])] for lab in labels)
    people = [lab for lab in labels if lab != "You"]
    report = [f"  {lab:9s} -> " + ", ".join(f"{w} {votes[(lab, w)]}" for w in VOICES if votes[(lab, w)])
              for lab in labels]
    others_as_you = sum(n for (lab, w), n in votes.items() if lab == "You" and w != "You")
    ok = (total > 0 and right / total >= 0.9 and len(people) == 3 and main.get("You") == "You"
          and len({main[p] for p in people}) == 3 and others_as_you == 0
          and (not named or all(main[p] == p for p in people)))
    report.append(f"  {right}/{total} words under the right label ({right / max(total, 1):.0%}), "
                  f"{len(people)} people found (3 expected), {others_as_you} of theirs under You"
                  + (", named " + ", ".join(people) if named else ""))
    return ok, report, main


def speaker_pass(python: str, folder: Path, out: Path, env: dict, voices: Path | None) -> bool:
    out.write_text("# placeholder (voxtype's transcript)\n")
    cmd = [python, "-I", str(TOOL), "--meeting", str(folder), "--output", str(out)]
    run = subprocess.run(cmd + (["--voices", str(voices)] if voices else []), env=env,
                         capture_output=True, text=True)
    for line in run.stderr.splitlines():
        if re.search(r"voice|done:|ERROR|WARNING|language", line):
            print("  " + line.removeprefix("npu-transcribe: "))
    if run.returncode != 0:
        print(f"FAIL: npu-transcribe exited with {run.returncode}")
    return run.returncode == 0


def name_people(voices: Path, transcript: Path, main: dict) -> None:
    """What the BMeet app does when you name the people of a meeting: their fingerprints
    from voices/meetings/<transcript>.json go into voices/voices.json under the name."""
    saved = json.loads((voices / "meetings" / f"{transcript.stem}.json").read_text())
    remembered = {main[label]: {"fingerprints": [fp]} for label, fp in saved["people"].items()
                  if label in main and main[label] != "You"}
    (voices / "voices.json").write_text(json.dumps({"version": 1, "model": saved["model"],
                                                    "voices": remembered}))


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("work", type=Path)
    p.add_argument("--language", choices=sorted(SCRIPT), default="ru")
    p.add_argument("--headphones", action="store_true")
    p.add_argument("--remember", action="store_true")
    args = p.parse_args()
    python = os.environ.get("NPU_PYTHON") or str(Path.home() / ".local/share/bmeet/venv/bin/python")
    name = f"meeting-{args.language}{'-headphones' if args.headphones else ''}"
    env = dict(os.environ, NPU_TRANSCRIBE_HISTORY=str(args.work / "history.jsonl"))
    # One synthesizer's voices are more alike than real people (TitaNet similarity ~0.35
    # vs ~0.1), so they need a stricter "same voice" threshold than real meetings (0.85;
    # tuned on LibriSpeech readers). This test checks the pipeline, not that tuning.
    env.setdefault("NPU_SPEAKER_THRESHOLD", "0.7")
    voices = args.work / f"{name}-voices" if args.remember else None
    if voices:
        shutil.rmtree(voices, ignore_errors=True)

    folder, out = args.work / name, args.work / f"{name}.md"
    make(folder, SCRIPT[args.language], args.language, args.headphones)
    if not speaker_pass(python, folder, out, env, voices):
        return 1
    ok, report, main_speaker = score(out.read_text(), SCRIPT[args.language])
    print("\n".join(report))
    print(("PASS" if ok else "FAIL") + f": {out}")
    if not ok or not voices:
        return 0 if ok else 1

    print("  naming the people as the app would, then a second meeting")
    name_people(voices, out, main_speaker)
    folder, out = args.work / f"{name}-2", args.work / f"{name}-2.md"
    make(folder, SCRIPT_2[args.language], args.language, args.headphones)
    if not speaker_pass(python, folder, out, env, voices):
        return 1
    ok, report, _ = score(out.read_text(), SCRIPT_2[args.language], named=True)
    print("\n".join(report))
    print(("PASS" if ok else "FAIL") + f": {out}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
