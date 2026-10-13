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

--in-person: like --remember (you also say a few more sentences in the first meeting, so
that your own voice is learned from your microphone), but the second meeting is in the
room: everyone on the microphone, nothing in the system audio. It must still come out as
You, Anna, Boris and Viktor.

--muted: after the pause you also say a sentence while the call cannot hear you
(call-mic.jsonl, as the Chrome extension logs it, says so); it must not be in the
transcript, the rest must.

Usage: meeting_test.py WORKDIR [--language ru|en] [--headphones] [--remember | --in-person] [--muted]
Env:   NPU_PYTHON  Python with openvino-genai (default: the installer's venv)
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
# --in-person: on one microphone the voices are told apart by sound alone, and espeak's
# variants above are too alike for that (TitaNet: You/Boris 0.84, as one real person). These
# four are at most 0.41 alike in both languages, like different real people (<= ~0.4).
VOICES_APART = {"Anna": ("+f2", 0.85), "Boris": ("+m3", 1.3), "Viktor": ("+m7", 0.85), "You": ("+f4", 1.3)}

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


# Said by you while muted (--muted): the call cannot hear it, so it must not be transcribed.
MUTED = {
    "ru": ("You", "Пока микрофон выключен, обсудим зарплату Бориса шёпотом."),
    "en": ("You", "While the microphone is muted, let us whisper about salaries."),
}

# More of your speech (--in-person), so that your own voice is learned in the first meeting.
YOU_MORE = {
    "ru": [("You", "Давайте я коротко расскажу, что мы успели сделать за прошлую неделю."),
           ("You", "Мы закончили миграцию базы данных и обновили все тестовые окружения."),
           ("You", "Осталось проверить резервное копирование и написать инструкцию для дежурных.")],
    "en": [("You", "Let me briefly tell you what we managed to do during the last week."),
           ("You", "We finished the database migration and updated all the test environments."),
           ("You", "What is left is to check the backups and to write a guide for the people on call.")],
}
T0 = 1_760_000_000.0  # when the test meeting "started" (part-1.start)


def make(folder: Path, lines: list[tuple[str, str]], language: str, headphones: bool,
         in_person: bool = False, muted: tuple[str, str] | None = None, voices: dict | None = None) -> None:
    """in_person: everyone on the microphone, no system audio. muted: this line goes into
    part 2 (second; after the pause, and out of the first 30 s, where the language is
    detected), and call-mic.jsonl says the call could not hear you meanwhile."""
    tts = load_tts()
    rng = np.random.default_rng(7)
    shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir(parents=True)
    half = len(lines) // 2
    chunks = [lines[:half], lines[half:]]
    if muted:
        chunks[1] = chunks[1][:1] + [muted] + chunks[1][1:]
    log, wall = [], T0
    for part, chunk in enumerate(chunks, 1):
        remote, mic, at = [], [], 0
        (folder / f"part-{part}.start").write_text(f"{wall}\n")
        for line in chunk:
            who, text = line
            variant, slower = (voices or VOICES)[who]
            x = speak(tts, language + variant, text, slower)
            gap = np.zeros(int(rng.uniform(0.4, 1.0) * RATE), np.float32)
            mine = who == "You" or in_person
            remote += [gap, np.zeros_like(x) if mine else x]
            mic += [gap, x if mine else np.zeros_like(x)]
            if line == muted:  # you mute, say it, unmute (the extension logs it within 0.5 s)
                start = wall + (at + gap.size) / RATE
                log += [(start - 0.8, False), (start + x.size / RATE + 0.8, True)]
            at += gap.size + x.size
        r, m = np.concatenate(remote), np.concatenate(mic)
        if not headphones and not in_person:  # the speakers reach the microphone 30 ms later, -20 dB
            m = m + np.concatenate([np.zeros(480, np.float32), r[:-480]]) * 0.1
        m = m + rng.normal(0, 0.002, m.size).astype(np.float32)  # room noise
        if not in_person:
            write_wav(folder / f"part-{part}-remote.wav", r)
        write_wav(folder / f"part-{part}-mic.wav", m)
        wall += at / RATE + 10  # a 10 s pause between the parts
    info = {"title": f"Test meeting ({language})", "in_person": in_person}
    (folder / "meeting.json").write_text(json.dumps(info))
    if muted:  # as bmeet-chrome-host writes it: live, every 5 s and at each change
        changes = [(T0, True)] + log
        rows = []
        for (t, live), nxt in zip(changes, changes[1:] + [(wall, None)]):
            rows += [(x, live) for x in np.arange(t, nxt[0], 5.0)]
        (folder / "call-mic.jsonl").write_text("".join(
            json.dumps({"t": round(float(t), 3), "call": True, "live": live}) + "\n" for t, live in rows))


def tokens(text: str) -> list[str]:
    return re.findall(r"\w+", text.lower())


def score(md: str, lines: list[tuple[str, str]], named: bool = False,
          one_mic: bool = False) -> tuple[bool, list[str], dict]:
    """Which label each word ended up under, against who said it. Only words of 4+ letters
    that one person says are counted, matched loosely (Whisper misspells synthetic speech).
    named: the people must be labelled with their own names (remembered voices).
    one_mic: everyone on one microphone (in person). Told apart by sound alone, a quick reply
    between two lines of someone else can go to them (on these ~22 s meetings with 0.4-1 s
    gaps: 1-3 words): 85 % of the words and at most one of theirs under You."""
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
    ok = (total > 0 and right / total >= (0.85 if one_mic else 0.9) and len(people) == 3
          and main.get("You") == "You" and len({main[p] for p in people}) == 3
          and others_as_you <= (1 if one_mic else 0)
          and (not named or all(main[p] == p for p in people)))
    report.append(f"  {right}/{total} words under the right label ({right / max(total, 1):.0%}), "
                  f"{len(people)} people found (3 expected), {others_as_you} of theirs under You"
                  + (", named " + ", ".join(people) if named else ""))
    return ok, report, main


def speaker_pass(python: str, folder: Path, out: Path, env: dict, voices: Path | None) -> bool:
    out.write_text("# placeholder (voxtype's transcript)\n")
    # Whisper: GigaAM (the default for Russian) was trained on real speech and writes almost
    # nothing for espeak's robotic Russian; its accuracy is measured on real speech instead.
    cmd = [python, "-I", str(TOOL), "--meeting", str(folder), "--output", str(out), "--engine", "whisper"]
    run = subprocess.run(cmd + (["--voices", str(voices)] if voices else []), env=env,
                         capture_output=True, text=True)
    for line in run.stderr.splitlines():
        if re.search(r"voice|done:|ERROR|WARNING|language", line):
            print("  " + line.removeprefix("npu-transcribe: "))
    if run.returncode != 0:
        print(f"FAIL: npu-transcribe exited with {run.returncode}")
    return run.returncode == 0


def muted_words(md: str, muted: tuple[str, str]) -> list[str]:
    """Words of the muted sentence (5+ letters, matched loosely) found in the transcript."""
    said = [w for w in tokens(muted[1]) if len(w) >= 5]
    body = md.split("## Transcript", 1)[-1]
    return [w for w in tokens(body) if len(w) >= 5 and difflib.get_close_matches(w, said, n=1, cutoff=0.8)]


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
    p.add_argument("--in-person", action="store_true")
    p.add_argument("--muted", action="store_true")
    args = p.parse_args()
    python = os.environ.get("NPU_PYTHON") or str(Path.home() / ".local/share/bmeet/venv/bin/python")
    name = (f"meeting-{args.language}{'-headphones' if args.headphones else ''}"
            f"{'-in-person' if args.in_person else ''}{'-muted' if args.muted else ''}")
    env = dict(os.environ, NPU_TRANSCRIBE_HISTORY=str(args.work / "history.jsonl"))
    voices = args.work / f"{name}-voices" if args.remember or args.in_person else None
    if voices:
        shutil.rmtree(voices, ignore_errors=True)

    lines = SCRIPT[args.language] + (YOU_MORE[args.language] if args.in_person else [])
    muted = MUTED[args.language] if args.muted else None
    voice_set = VOICES_APART if args.in_person else VOICES
    folder, out = args.work / name, args.work / f"{name}.md"
    make(folder, lines, args.language, args.headphones, muted=muted, voices=voice_set)
    if not speaker_pass(python, folder, out, env, voices):
        return 1
    ok, report, main_speaker = score(out.read_text(), lines)
    if muted:
        heard = muted_words(out.read_text(), muted)
        ok &= len(heard) <= 1
        report.append(f"  {len(heard)} word(s) of the muted sentence in the transcript"
                      + (f": {', '.join(heard)}" if heard else "") + " (at most 1 expected)")
    if args.in_person:
        learned = (voices / "you.json").exists()
        ok &= learned
        report.append("  your voice " + ("learned" if learned else "NOT learned") + " from your microphone")
    print("\n".join(report))
    print(("PASS" if ok else "FAIL") + f": {out}")
    if not ok or not voices:
        return 0 if ok else 1

    print("  naming the people as the app would, then a second meeting"
          + (" in the room (microphone only)" if args.in_person else ""))
    name_people(voices, out, main_speaker)
    folder, out = args.work / f"{name}-2", args.work / f"{name}-2.md"
    make(folder, SCRIPT_2[args.language], args.language, args.headphones, in_person=args.in_person,
         voices=voice_set)
    if not speaker_pass(python, folder, out, env, voices):
        return 1
    ok, report, _ = score(out.read_text(), SCRIPT_2[args.language], named=True, one_mic=args.in_person)
    print("\n".join(report))
    print(("PASS" if ok else "FAIL") + f": {out}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
