#!/usr/bin/env python3
"""End-to-end test of `npu-transcribe --meeting` (BMeet's speaker pass) on a made-up call.

Builds a meeting recording like bmeet-meeting-audio writes it (two parts: there is a
pause in the middle) from real people's speech, without recording anything:
  system audio  three people taking turns (Anna, Boris, Viktor)
  microphone    "you" in the gaps, plus the system audio leaking in at -20 dB as from
                laptop speakers (--headphones: no leak)
then runs the speaker pass and checks the Markdown: three people found, every phrase
under the label of whoever said it (each person has their own phrases), and none of the
other side's words under "You".

The people (fetched into WORKDIR once; needs the network then): in Russian, four speakers
of three podcasts in leshinsky/ru-youtube-diarization on Hugging Face (Apache-2.0 labels
with the text; the audio is from YouTube, so it is only fetched for this local test, never
kept in the repository), each saying their own labelled phrases, transcribed by GigaAM as
in a real meeting; in English, four LibriSpeech test-clean readers (CC-BY-4.0) through the
datasets-server.

--remember: remembered voices. The first meeting runs with --voices; its people are then
named as the BMeet app would (voices.json from the meeting's fingerprints), and a second
meeting with other phrases, in which the people speak in another order, must come out
as Anna, Boris and Viktor.

--in-person: like --remember (you also say a few more phrases in the first meeting, so
that your own voice is learned from your microphone), but the second meeting is in the
room: everyone on the microphone, nothing in the system audio. It must still come out as
You, Anna, Boris and Viktor.

--muted: after the pause you also say a phrase while the call cannot hear you
(call-mic.jsonl, as the Chrome extension logs it, says so); it must not be in the
transcript, the rest must.

--espeak: espeak-ng voices and our own sentences instead (no network). Nemotron 3
Diarization, which tells the voices apart since 0.6.0, was trained on real conversations
and often hears espeak's voices, all from one synthesizer, as one person; so this only
checks the rest (your label, the echo, muting), not that the people are told apart.

--live: the live transcript instead (npu-transcribe --live, as the BMeet window shows it while
the meeting runs): the meeting is recorded again in real time (--speed N: N times faster)
while it follows; your words must be under You and none of theirs, nothing said while
muted, and the lines must show up. Reports how soon after its end each line was seen.

Usage: meeting_test.py WORKDIR [--language ru|en] [--headphones] [--remember | --in-person] [--muted] [--espeak]
                       [--live [--speed N]]
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
import urllib.request
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


RU_CLIPS = "https://huggingface.co/datasets/leshinsky/ru-youtube-diarization/resolve/main/"
# Four different people, each one voice in all their phrases (TitaNet: the people at most
# 0.41 alike). The two podcasts of youtube-1.json share their host, so only one copy of him
# is used; nr3hmL9bcPU's "Speaker 1" and "Speaker 2" mix in phrases of someone else.
RU_PEOPLE = {"Anna": ("youtube-1.json", "clips/p0_iIC13bs8.opus", "Speaker 2"),
             "Boris": ("youtube-2.json", "clips/nr3hmL9bcPU.opus", "Speaker 3"),
             "Viktor": ("youtube-1.json", "clips/p0_iIC13bs8.opus", "Speaker 1"),
             "You": ("youtube-1.json", "clips/yCX4MRy14zw.opus", "Speaker 1")}
LIBRISPEECH = ("https://datasets-server.huggingface.co/rows?dataset=openslr/librispeech_asr"
               "&config=clean&split=test&length=100&offset=")


def fetch_json(url: str, tries: int = 4):
    for attempt in range(tries):   # (the datasets-server sometimes answers 500)
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                return json.load(r)
        except OSError:
            if attempt == tries - 1:
                raise
            import time
            time.sleep(5)


def decode(path: Path) -> np.ndarray:
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "f32le", "-ac", "1", "-ar", str(RATE), "-"],
                         check=True, capture_output=True).stdout
    return np.frombuffer(raw, np.float32)


def real_voices(work: Path, language: str) -> dict[str, list[tuple[str, np.ndarray]]]:
    """{person: [(text, 16 kHz speech), ...]}: real people (the default; not --espeak), fetched into WORKDIR once.
    Phrases of 2.5-9 s with 4+ words, in which nobody else talks."""
    cache = work / f"real-{language}"
    cache.mkdir(parents=True, exist_ok=True)
    people: dict[str, list[tuple[str, np.ndarray]]] = {}
    if language == "ru":
        for name, (labels, clip, who) in RU_PEOPLE.items():
            for f in (labels, clip):
                if not (cache / Path(f).name).exists():
                    urllib.request.urlretrieve(RU_CLIPS + f, cache / Path(f).name)
            task = next(t for t in json.loads((cache / labels).read_text()) if t["data"]["audio"] == clip)
            x = decode(cache / Path(clip).name)
            result = task["annotations"][0]["result"]
            text = {r["id"]: r["value"]["text"][0].strip() for r in result if r["type"] == "textarea"}
            spans = [(r["value"]["start"], r["value"]["end"], r["value"]["labels"][0], text.get(r["id"], ""))
                     for r in result if r["type"] == "labels"]
            people[name] = [(words, x[int(a * RATE):int(b * RATE)]) for a, b, label, words in spans
                            if label == who and 2.5 <= b - a <= 9 and len(tokens(words)) >= 4
                            and all(o[2] == who or o[1] < a - 0.3 or o[0] > b + 0.3 for o in spans)]
    else:
        index = cache / "librispeech.json"
        if not index.exists():
            rows = []
            for offset in (0, 650, 1300, 1950):   # (the rows are sorted by reader)
                rows += fetch_json(LIBRISPEECH + str(offset))["rows"]
            readers: dict[int, list] = collections.defaultdict(list)
            for row in rows:   # ~2.5-9 s: 7-24 words
                if 7 <= len(row["row"]["text"].split()) <= 24:
                    readers[row["row"]["speaker_id"]].append(row)
            utterances = []
            for reader in sorted(readers, key=lambda r: -len(readers[r]))[:4]:
                for row in readers[reader][:14]:
                    path = cache / f"{row['row_idx']}.flac"
                    urllib.request.urlretrieve(row["row"]["audio"][0]["src"], path)
                    utterances.append({"file": str(path), "text": row["row"]["text"].capitalize() + ".",
                                       "reader": reader})
            index.write_text(json.dumps(utterances))
        by_reader: dict[int, list] = collections.defaultdict(list)
        for u in json.loads(index.read_text()):
            by_reader[u["reader"]].append((u["text"], decode(Path(u["file"]))))
        for name, reader in zip(("Anna", "Boris", "Viktor", "You"), by_reader):
            people[name] = by_reader[reader]
    if len(people) < 4 or min(len(p) for p in people.values()) < 8:
        raise SystemExit(f"not enough real phrases for {language}: {[len(p) for p in people.values()]}")
    return people


def real_lines(people: dict, plan: list[tuple[str, str]], used: collections.Counter) -> list[tuple[str, str]]:
    """The same people in the same order as `plan`, each saying their next own phrase."""
    out = []
    for who, _ in plan:
        out.append((who, people[who][used[who]][0]))
        used[who] += 1
    return out


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
         in_person: bool = False, muted: tuple[str, str] | None = None, voices: dict | None = None,
         real: dict | None = None) -> list[tuple[tuple[str, str], float, float]]:
    """in_person: everyone on the microphone, no system audio. muted: this line goes into
    part 2 (second; after the pause, and out of the first 30 s, where the language is
    detected), and call-mic.jsonl says the call could not hear you meanwhile. real: real
    people's phrases (real_voices) instead of espeak. Returns where each line is said (start
    and end in seconds of the parts joined)."""
    tts = load_tts() if real is None else None
    recorded = {(who, text): x for who, phrases in (real or {}).items() for text, x in phrases}
    rng = np.random.default_rng(7)
    shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir(parents=True)
    half = len(lines) // 2
    chunks = [lines[:half], lines[half:]]
    if muted:
        chunks[1] = chunks[1][:1] + [muted] + chunks[1][1:]
    log, wall, timeline, offset = [], T0, [], 0.0
    for part, chunk in enumerate(chunks, 1):
        remote, mic, at = [], [], 0
        (folder / f"part-{part}.start").write_text(f"{wall}\n")
        for line in chunk:
            who, text = line
            if real is not None:
                x = recorded[(who, text)]
            else:
                variant, slower = (voices or VOICES)[who]
                x = speak(tts, language + variant, text, slower)
            gap = np.zeros(int(rng.uniform(0.4, 1.0) * RATE), np.float32)
            mine = who == "You" or in_person
            remote += [gap, np.zeros_like(x) if mine else x]
            mic += [gap, x if mine else np.zeros_like(x)]
            if line == muted:  # you mute, say it, unmute (the extension logs it within 0.5 s)
                start = wall + (at + gap.size) / RATE
                log += [(start - 0.8, False), (start + x.size / RATE + 0.8, True)]
            timeline.append((line, offset + (at + gap.size) / RATE, offset + (at + gap.size + x.size) / RATE))
            at += gap.size + x.size
        r, m = np.concatenate(remote), np.concatenate(mic)
        if not headphones and not in_person:  # the speakers reach the microphone 30 ms later, -20 dB
            m = m + np.concatenate([np.zeros(480, np.float32), r[:-480]]) * 0.1
        m = m + rng.normal(0, 0.002, m.size).astype(np.float32)  # room noise
        if not in_person:
            write_wav(folder / f"part-{part}-remote.wav", r)
        write_wav(folder / f"part-{part}-mic.wav", m)
        wall += at / RATE + 10  # a 10 s pause between the parts
        offset += at / RATE
    info = {"title": f"Test meeting ({language})", "in_person": in_person}
    (folder / "meeting.json").write_text(json.dumps(info))
    if muted:  # as bmeet-chrome-host writes it: live, every 5 s and at each change
        changes = [(T0, True)] + log
        rows = []
        for (t, live), nxt in zip(changes, changes[1:] + [(wall, None)]):
            rows += [(x, live) for x in np.arange(t, nxt[0], 5.0)]
        (folder / "call-mic.jsonl").write_text("".join(
            json.dumps({"t": round(float(t), 3), "call": True, "live": live}) + "\n" for t, live in rows))
    return timeline


def tokens(text: str) -> list[str]:
    return re.findall(r"\w+", text.lower())


def score(md: str, lines: list[tuple[str, str]], named: bool = False,
          one_mic: bool = False, people_apart: bool = True) -> tuple[bool, list[str], dict]:
    """Which label each word ended up under, against who said it. Only words of 4+ letters
    that one person says are counted, matched loosely (Whisper misspells synthetic speech).
    named: the people must be labelled with their own names (remembered voices).
    people_apart: False (espeak) checks only your label and that none of theirs is under it.
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
    ok = (total > 0 and main.get("You") == "You" and others_as_you <= (1 if one_mic else 0)
          and (not people_apart or (right / total >= (0.85 if one_mic else 0.9) and len(people) == 3
                                    and len({main[p] for p in people}) == 3
                                    and (not named or all(main[p] == p for p in people)))))
    report.append(f"  {right}/{total} words under the right label ({right / max(total, 1):.0%}), "
                  f"{len(people)} people found (3 expected), {others_as_you} of theirs under You"
                  + (", named " + ", ".join(people) if named else ""))
    return ok, report, main


def speaker_pass(python: str, folder: Path, out: Path, env: dict, voices: Path | None,
                 real: bool = False) -> bool:
    out.write_text("# placeholder (voxtype's transcript)\n")
    # Whisper: GigaAM (the default for Russian) was trained on real speech and writes almost
    # nothing for espeak's robotic Russian; its accuracy is measured on real speech instead.
    cmd = [python, "-I", str(TOOL), "--meeting", str(folder), "--output", str(out)] \
        + ([] if real else ["--engine", "whisper"])
    run = subprocess.run(cmd + (["--voices", str(voices)] if voices else []), env=env,
                         capture_output=True, text=True)
    for line in run.stderr.splitlines():
        if re.search(r"voice|done:|ERROR|WARNING|language", line):
            print("  " + line.removeprefix("npu-transcribe: "))
    if run.returncode != 0:
        print(f"FAIL: npu-transcribe exited with {run.returncode}")
    return run.returncode == 0


def muted_words(md: str, muted: tuple[str, str], lines: list[tuple[str, str]]) -> list[str]:
    """Words of the muted sentence (5+ letters, matched loosely; not said in other lines)
    found in the transcript."""
    others = {w for line in lines if line != muted for w in tokens(line[1])}
    said = [w for w in tokens(muted[1]) if len(w) >= 5 and not difflib.get_close_matches(w, others, n=1, cutoff=0.8)]
    body = md.split("## Transcript", 1)[-1]
    return [w for w in tokens(body) if len(w) >= 5 and difflib.get_close_matches(w, said, n=1, cutoff=0.8)]


def replay(src: Path, dst: Path, pointer: Path, speed: float, watch) -> None:
    """Records src's meeting again into dst, as bmeet-meeting-audio does while a meeting runs:
    each part's start time, then its WAV files growing 0.1 s at a time (`speed` x real time),
    a 1 s pause between the parts; `pointer` names dst meanwhile, and call-mic.jsonl gets its
    lines as their time comes. watch(at) runs after each step (at: seconds recorded)."""
    import time
    shutil.rmtree(dst, ignore_errors=True)
    dst.mkdir(parents=True)
    shutil.copy(src / "meeting.json", dst)
    calls = (src / "call-mic.jsonl").read_text().splitlines(keepends=True) if (src / "call-mic.jsonl").exists() else []
    pointer.write_text(f"{dst}\n")
    header = (b"RIFF\0\0\0\0WAVEfmt " + (16).to_bytes(4, "little") + (1).to_bytes(2, "little")
              + (1).to_bytes(2, "little") + RATE.to_bytes(4, "little") + (2 * RATE).to_bytes(4, "little")
              + (2).to_bytes(2, "little") + (16).to_bytes(2, "little") + b"data\0\0\0\0")  # (sizes: not yet)
    offset, part, step = 0.0, 1, RATE // 10
    t0 = time.monotonic()
    while (src / f"part-{part}.start").exists():
        wall = float((src / f"part-{part}.start").read_text())
        (dst / f"part-{part}.start").write_text(f"{wall}\n")
        tracks = {}
        for kind in ("mic", "remote"):
            if (src / f"part-{part}-{kind}.wav").exists():
                with wave.open(str(src / f"part-{part}-{kind}.wav")) as w:
                    tracks[kind] = w.readframes(w.getnframes())
                (dst / f"part-{part}-{kind}.wav").write_bytes(header)
        size = max(len(b) for b in tracks.values()) // 2
        for i in range(0, size, step):
            for kind, data in tracks.items():
                with open(dst / f"part-{part}-{kind}.wav", "ab") as f:
                    f.write(data[2 * i:2 * (i + step)])
            now = wall + (i + step) / RATE
            while calls and json.loads(calls[0])["t"] <= now:
                with open(dst / "call-mic.jsonl", "a") as f:
                    f.write(calls.pop(0))
            at = offset + (i + step) / RATE
            time.sleep(max(0.0, t0 + at / speed - time.monotonic()))
            watch(at)
        offset += size / RATE
        t0 += 1.0
        part += 1
    pointer.unlink()


def live_test(python: str, folder: Path, timeline: list, lines: list[tuple[str, str]],
              muted: tuple[str, str] | None, env: dict, speed: float, real: bool) -> tuple[bool, list[str]]:
    """--live: `npu-transcribe --live` follows a replay of the meeting in folder; its live.md
    must have your words under You and none of theirs, nothing said while muted, and most
    of what was said; reports how soon each line showed up (draft) and was final."""
    import time
    dst = folder.with_name(folder.name + "-live")
    pointer = folder.with_name(folder.name + "-recording")
    env = dict(env, BMEET_RECORDING_FILE=str(pointer))
    proc = subprocess.Popen([python, "-I", str(TOOL), "--live", str(dst)]
                            + ([] if real else ["--engine", "whisper"]), env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    first: dict[int, float] = {}
    final: dict[int, float] = {}
    distinct = [[w for w in tokens(text) if len(w) >= 4] for (_, text), _, _ in timeline]
    wanted = [line for line, _, _ in timeline]

    def found(words: list[str], text: str) -> bool:
        have = set(tokens(text))
        hits = sum(1 for w in words if w in have or difflib.get_close_matches(w, list(have), n=1, cutoff=0.8))
        return bool(words) and hits >= len(words) / 2

    def watch(at: float) -> None:
        try:
            md = (dst / "live.md").read_text().split("## Transcript", 1)[-1]
        except OSError:
            return
        settled = re.sub(r"\*[^*\n]+ …\*", "", md)
        for i, (line, _, _) in enumerate(timeline):
            if line == muted:
                continue
            if i not in first and found(distinct[i], md):
                first[i] = at
            if i not in final and found(distinct[i], settled):
                final[i] = at
    t0 = time.monotonic()
    try:
        replay(folder, dst, pointer, speed, watch)
    except BaseException:
        proc.kill()
        raise
    ended = time.monotonic()
    try:
        _, err = proc.communicate(timeout=120)
    except subprocess.TimeoutExpired:
        proc.kill()
        _, err = proc.communicate()
    wall = time.monotonic() - t0
    for line in err.splitlines():
        if re.search(r"live:|ERROR|WARNING|language", line):
            print("  " + line.removeprefix("npu-transcribe: "))
    report = []
    if proc.returncode != 0 or not (dst / "live.done").exists():
        return False, [f"  npu-transcribe --live exited with {proc.returncode}, live.done "
                       + ("there" if (dst / "live.done").exists() else "missing")]
    md = (dst / "live.md").read_text()
    watch(timeline[-1][2] + (time.monotonic() - ended) * speed)  # (what came once the recording ended)
    shown = len(final)
    late = [final[i] - end for i, (_, _, end) in enumerate(timeline) if i in final]
    early = [first[i] - end for i, (_, _, end) in enumerate(timeline) if i in first]
    if "### Room" in md:  # in person: everyone on the microphone, one label
        ok, report = "### You" not in md and "### Remote" not in md, ["  everything under Room"]
    else:
        ok, report, _ = score(md, [line for line in lines if line != muted], people_apart=False)
    if muted:
        heard = muted_words(md, muted, lines)
        ok &= len(heard) <= 1
        report.append(f"  {len(heard)} word(s) of the muted sentence in the live transcript (at most 1 expected)")
    expected = sum(1 for line in wanted if line != muted)
    ok &= shown >= expected - 1
    report.append(f"  {shown}/{expected} lines shown live; after the end of a line: first seen "
                  f"{np.median(early):+.1f} s (median, max {max(early):+.1f} s), final {np.median(late):+.1f} s "
                  f"(max {max(late):+.1f} s); recording {timeline[-1][2]:.0f} s at {speed:g}x, "
                  f"{wall:.0f} s wall")
    return ok, report


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
    p.add_argument("--espeak", action="store_true")
    p.add_argument("--live", action="store_true")
    p.add_argument("--speed", type=float, default=1.0)
    args = p.parse_args()
    if args.live and args.remember:
        p.error("--live does not go with --remember")
    python = os.environ.get("NPU_PYTHON") or str(Path.home() / ".local/share/bmeet/venv/bin/python")
    name = (f"meeting-{args.language}{'-headphones' if args.headphones else ''}"
            f"{'-in-person' if args.in_person else ''}{'-muted' if args.muted else ''}{'-espeak' if args.espeak else ''}")
    env = dict(os.environ, NPU_TRANSCRIBE_HISTORY=str(args.work / "history.jsonl"))
    voices = args.work / f"{name}-voices" if args.remember or args.in_person else None
    if voices:
        shutil.rmtree(voices, ignore_errors=True)

    lines = SCRIPT[args.language] + (YOU_MORE[args.language] if args.in_person else [])
    muted = MUTED[args.language] if args.muted else None
    real, used = None, collections.Counter()
    if not args.espeak:
        real = real_voices(args.work, args.language)
        lines = real_lines(real, lines, used)
        muted = real_lines(real, [muted], used)[0] if muted else None
    voice_set = VOICES_APART if args.in_person else VOICES
    folder, out = args.work / name, args.work / f"{name}.md"
    timeline = make(folder, lines, args.language, args.headphones, in_person=args.in_person and args.live,
                    muted=muted, voices=voice_set, real=real)
    if args.live:
        ok, report = live_test(python, folder, timeline, lines, muted, env, args.speed, real is not None)
        print("\n".join(report))
        print(("PASS" if ok else "FAIL") + f": {folder.name}-live/live.md")
        return 0 if ok else 1
    if not speaker_pass(python, folder, out, env, voices, real is not None):
        return 1
    ok, report, main_speaker = score(out.read_text(), lines, people_apart=real is not None)
    if muted:
        heard = muted_words(out.read_text(), muted, lines)
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
    lines = SCRIPT_2[args.language] if real is None else real_lines(real, SCRIPT_2[args.language], used)
    make(folder, lines, args.language, args.headphones, in_person=args.in_person, voices=voice_set, real=real)
    if not speaker_pass(python, folder, out, env, voices, real is not None):
        return 1
    ok, report, _ = score(out.read_text(), lines, named=True, one_mic=args.in_person, people_apart=real is not None)
    print("\n".join(report))
    print(("PASS" if ok else "FAIL") + f": {out}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
