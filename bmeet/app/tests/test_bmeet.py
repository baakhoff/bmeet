#!/usr/bin/python3 -I
"""Offscreen tests for the BMeet app: data functions, every page of the window,
single-instance forwarding and (REAL_TRANSCRIBE=1) the npu-transcribe history change; the
interface language and its one table of texts (E), the Transcribe page's engine choice (F), the
feature switches and bmeet-ctl's settings, status and refusals (G).

Safety: works on a TEMPORARY meetings DB with voxtype 1.1.0's schema and synthetic
rows (voxtype's real index.db is never opened); voxtype is replaced
by tests/fake-voxtype (export/delete on that copy only) and npu-transcribe by
tests/fake-npu-transcribe for the GUI flow; state files, history.jsonl, settings
and the Trash all live in the work dir. BMEET_DRY_RUN=1 keeps the app from
recording, starting meetings, switching language or opening files on the desktop.
Speech for the real run is synthesised with the espeak-ng library
(transcribe/tests/make_speech.py).

Run: tests/run-tests.sh [--quick]     (or: tests/test_bmeet.py -v)
Env: BMEET_TEST_WORK (default tests/.work; must be on the home filesystem so
the Trash tests use $XDG_DATA_HOME/Trash), REAL_TRANSCRIBE=1, KEEP_WORK=1,
NPU_PYTHON (Python with openvino-genai for the real runs; default: the installer's venv).
"""
from __future__ import annotations

import ast
import contextlib
import fcntl
import importlib.machinery
import importlib.util
import io
import json
import os
import re
import shlex
import shutil
import signal
import socket
import sqlite3
import string
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True

HERE = Path(__file__).resolve().parent
APP_PATH = HERE.parent / "bmeet"
CTL_PATH = HERE.parent.parent / "bmeet-ctl"
REPO = HERE.parents[2]
MEETING_RUNNER = REPO / "macos/bmeet-meeting"
# a test run must never write QSettings' native place on a Mac (an old run once did: 2026-10-20)
MAC_PREFS = Path.home() / "Library/Preferences/com.bmeet.bmeet.plist"
MAC_PREFS_BEFORE = os.stat(MAC_PREFS).st_mtime_ns if MAC_PREFS.exists() else None
NPU_TRANSCRIBE = REPO / "transcribe/npu-transcribe"
# The installed models: the tests point XDG_DATA_HOME at a scratch folder (below), and
# npu-transcribe looks for its models under it.
NPU_MODELS = os.environ.get("NPU_MODELS_DIR") or str(
    Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "npu-models")
MAKE_SPEECH = REPO / "transcribe/tests/make_speech.py"
NPU_PYTHON = os.environ.get("NPU_PYTHON") or str(
    Path.home() / ".local/share/bmeet/venv/bin/python")
WORK = Path(os.environ.get("BMEET_TEST_WORK") or HERE / ".work").resolve()
REAL = bool(os.environ.get("REAL_TRANSCRIBE"))
# voxtype 1.1.0's meetings index (sqlite3 index.db .schema)
MEETINGS_SCHEMA = """
CREATE TABLE meetings (
    id TEXT PRIMARY KEY,
    title TEXT,
    started_at INTEGER NOT NULL,
    ended_at INTEGER,
    duration_secs INTEGER,
    status TEXT NOT NULL DEFAULT 'active',
    chunk_count INTEGER NOT NULL DEFAULT 0,
    storage_path TEXT,
    audio_retained INTEGER NOT NULL DEFAULT 0,
    model TEXT,
    synced_at INTEGER,
    created_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now'))
);
CREATE INDEX idx_meetings_started_at ON meetings(started_at DESC);
CREATE INDEX idx_meetings_status ON meetings(status);
CREATE TABLE speaker_labels (
    meeting_id TEXT NOT NULL,
    speaker_num INTEGER NOT NULL,
    label TEXT NOT NULL,
    created_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
    PRIMARY KEY (meeting_id, speaker_num),
    FOREIGN KEY (meeting_id) REFERENCES meetings(id) ON DELETE CASCADE
);
"""


def epoch(y, mo, d, h, mi, s) -> int:
    return int(time.mktime((y, mo, d, h, mi, s, 0, 0, -1)))


def minute_stamp(t: float) -> str:
    return time.strftime("%Y-%m-%d_%H%M", time.localtime(t))


# ---------------------------------------------------------------------------- fixture

def make_fixture() -> dict:
    if WORK.exists():
        shutil.rmtree(WORK)
    for sub in ("config", "data", "run", "Meetings", "media", "out"):
        (WORK / sub).mkdir(parents=True)
    kdeglobals = Path.home() / ".config/kdeglobals"  # the user's colours (Breeze reads them for
    if kdeglobals.is_file():                         # the toolbar) under the test XDG_CONFIG_HOME
        shutil.copy(kdeglobals, WORK / "config/kdeglobals")
    db = WORK / "index.db"
    dst = sqlite3.connect(db)
    dst.executescript(MEETINGS_SCHEMA)
    # dictation config the app reads through bmeet-ctl (under the test XDG_CONFIG_HOME)
    (WORK / "config/voxtype").mkdir()
    (WORK / "config/voxtype/config.toml").write_text(
        'engine = "openvino"\n\n[openvino]\nmodel = "/x/whisper-small-int8-ov"\n'
        'device = "NPU"\nlanguage = "en"\n')
    (WORK / "config/bmeet").mkdir()
    (WORK / "config/bmeet/languages").write_text("en de\n")
    (WORK / "config/bmeet/update-check").write_text("off\n")   # (the window's own check: A_UpdateTests turn it on)
    # English, whatever this machine's language is (E_LanguageTests try the others)
    (WORK / "config/bmeet/interface-language").write_text("en\n")
    ids = {k: str(uuid.uuid4()) for k in "ABCD"}
    t = {"A": epoch(2026, 10, 1, 10, 0, 5), "B": epoch(2026, 10, 2, 14, 30, 58),
         "C": epoch(2026, 10, 3, 9, 15, 0), "D": int(time.time()) - 125}
    rows = [("A", "Weekly sync", 3600, "completed", 120), ("B", "Meeting 2026-10-02 14:30", 1800, "completed", 60),
            ("C", "Call with Anna", 600, "completed", 20), ("D", "Live one", None, "active", 4)]
    for k, title, dur, status, chunks in rows:
        dst.execute("INSERT INTO meetings (id, title, started_at, ended_at, duration_secs, status, "
                    "chunk_count, storage_path, model) VALUES (?,?,?,?,?,?,?,?,?)",
                    (ids[k], title, t[k], t[k] + dur if dur else None, dur, status, chunks, None,
                     "/home/x/.local/share/npu-models/whisper-small-int8-ov"))
    dst.commit()
    dst.close()
    md = WORK / "Meetings"
    (md / f"{minute_stamp(t['A'])}-meeting.md").write_text(
        "# Weekly sync\n\n**[00:00:01] You:** We discussed the budget.\n")
    # B started at hh:30:58; the toggle script stamped it a minute later.
    (md / f"{minute_stamp(t['B'] + 10)}-meeting.md").write_text(
        "# Meeting 2026-10-02 14:30\n\n**[00:00:01] Remote:** The roadmap is fine.\n")
    run = WORK / "run"
    (run / "pid").write_text(f"{os.getpid()}\n")
    (run / "state").write_text("idle\n")
    (run / "meeting_state").write_text("idle\n")

    media = WORK / "media"
    files = {}
    for name in ("talk.ogg", "broken.mp3", "a.ogg", "b.ogg", "c.ogg", "d.ogg", "e.ogg", "clip.ogg"):
        (media / name).write_bytes(b"OggS\0dummy")
        files[name] = str(media / name)
    (media / "talk.srt").write_text("1\n00:00:00,000 --> 00:00:02,000\nA kangaroo talk.\n")
    (media / "talk.md").write_text("# talk\n\n- **Model:** turbo\n\n[00:00:00] A kangaroo talk.\n")
    (media / "notes.txt").write_text("not a recording")
    (media / "list.m3u").write_text("#EXTM3U\n")
    hist = [
        {"time": 1760000000, "input": files["talk.ogg"], "outputs": [str(media / "talk.srt"), str(media / "talk.md")],
         "model": "turbo", "language": "en", "device": "NPU", "duration": 125.5, "status": "done"},
        {"time": 1760000100, "input": files["broken.mp3"], "outputs": [], "model": "small", "language": "?",
         "device": "CPU", "duration": 0.0, "status": "failed"},
        {"time": 1760000200, "input": str(media / "gone.ogg"), "outputs": [str(media / "gone.md")],
         "model": "turbo", "language": "ru", "device": "NPU", "duration": 30.0, "status": "done"},
    ]
    lines = [json.dumps(h) for h in hist]
    lines.insert(1, "this is not json")
    (WORK / "history.jsonl").write_text("\n".join(lines) + "\n")

    clip = None
    if REAL:  # speech clip for the real npu-transcribe runs
        loader = importlib.machinery.SourceFileLoader("make_speech", str(MAKE_SPEECH))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        ms = importlib.util.module_from_spec(spec)
        loader.exec_module(ms)
        tts = ms.Espeak()
        (WORK / "real").mkdir()
        clip = WORK / "real" / "hello clip.ogg"  # a space on purpose
        ms.encode(tts.silence(0.5) + tts.say(ms.EN_SHORT, "en"), tts.rate, clip)
    return {"ids": ids, "t": t, "files": files, "media": media, "clip": clip}


FIX = make_fixture()
ENV = {
    "QT_QPA_PLATFORM": "offscreen",
    "XDG_CONFIG_HOME": str(WORK / "config"),
    "XDG_DATA_HOME": str(WORK / "data"),
    "BMEET_MEETING_DB": str(WORK / "index.db"),
    "VOXTYPE_MEETING_DIR": str(WORK / "Meetings"),
    "NPU_TRANSCRIBE_HISTORY": str(WORK / "history.jsonl"),
    "BMEET_RUN_DIR": str(WORK / "run"),
    "BMEET_AUDIO_DIR": str(WORK / "meeting-audio"),
    "BMEET_VOICES_DIR": str(WORK / "voices"),
    "BMEET_VOXTYPE": str(HERE / "fake-voxtype"),
    "BMEET_TRANSCRIBE": str(HERE / "fake-npu-transcribe"),
    "FAKE_VOXTYPE_DB": str(WORK / "index.db"),
    "FAKE_VOXTYPE_LOG": str(WORK / "voxtype-calls.log"),
    "BMEET_DRY_RUN": "1",
    "BMEET_SOCKET": str(WORK / "bmeet.sock"),
    "NPU_MODELS_DIR": NPU_MODELS,
    "BMEET_PLATFORM": "linux",   # (on a Mac too: these tests are the Linux window's, with fake voxtype)
    # ... and should a Mac path run all the same, its runtime folder is this one, never the one of
    # BMeet.app in real use (agent.sock): no socket is ever made here
    "BMEET_RUNTIME_ROOT": str(WORK / "rt"),
    # updates: never GitHub; curl reads these files instead (A_UpdateTests writes them)
    "XDG_CACHE_HOME": str(WORK / "cache"),
    "BMEET_RELEASES_URL": (WORK / "releases.json").as_uri(),
    "BMEET_INSTALL_URL": (WORK / "install-{tag}.sh").as_uri().replace("%7B", "{").replace("%7D", "}"),
}
os.environ.update(ENV)
os.environ.pop("XDG_ACTIVATION_TOKEN", None)

_loader = importlib.machinery.SourceFileLoader("bmeet_app", str(APP_PATH))
_spec = importlib.util.spec_from_loader(_loader.name, _loader)
vn = importlib.util.module_from_spec(_spec)
sys.modules[_loader.name] = vn  # dataclasses look the module up
_loader.exec_module(vn)
QApplication = vn.QApplication
app = QApplication.instance() or QApplication(["bmeet-test"])
if sys.platform == "darwin":
    # Qt's moveToTrash puts files into your own Trash on a Mac (Linux's goes to $XDG_DATA_HOME/Trash,
    # here the work dir): keep the tests' files out of it, in the place the tests look.
    def _work_trash(path: str) -> bool:
        dest = WORK / "data/Trash/files" / Path(path).name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(path, dest)
        return True
    vn.move_to_trash = _work_trash
vn.QGuiApplication.setDesktopFileName("bmeet")


def wait_until(cond, timeout: float = 10.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.02)
    app.processEvents()
    return bool(cond())


def voxtype_calls() -> list[list[str]]:
    try:
        return [json.loads(x) for x in (WORK / "voxtype-calls.log").read_text().splitlines()]
    except OSError:
        return []


def history_lines(path: Path | None = None) -> list[str]:
    return [x for x in (path or WORK / "history.jsonl").read_text().splitlines() if x.strip()]


# ============================================================================ data

class A_DataTests(unittest.TestCase):
    def test_paths_from_ctl(self):
        self.assertIsNotNone(vn.ctl, vn.CTL_ERROR)
        self.assertEqual(vn.MEETING_DB, WORK / "index.db")
        self.assertEqual(vn.MEETING_MD_DIR, WORK / "Meetings")
        self.assertEqual(vn.FILE_HISTORY, WORK / "history.jsonl")
        self.assertEqual(vn.RUN_DIR, WORK / "run")
        self.assertFalse((CTL_PATH.parent / "__pycache__" / "bmeet-ctl.cpython-314.pyc").exists())

    def test_format_helpers(self):
        self.assertEqual(vn.fmt_duration(None), "–")
        self.assertEqual(vn.fmt_duration(59.6), "1:00")
        self.assertEqual(vn.fmt_duration(3725), "1:02:05")
        self.assertEqual(vn.tilde(str(Path.home() / "Documents/x.md")), "~/Documents/x.md")
        self.assertEqual(vn.tilde("/tmp/x"), "/tmp/x")
        self.assertEqual(vn.safe_filename("a/b: c\n"), "a b: c")
        self.assertEqual(vn.safe_filename("..."), "transcript")

    def test_parse_cli(self):
        p = vn.parse_cli
        self.assertEqual(p([], "/w"), {"page": None, "id": None, "files": []})
        self.assertEqual(p(["meetings", "--id", "X"], "/w"), {"page": "meetings", "id": "X", "files": []})
        self.assertEqual(p(["--id=Y"], "/w")["id"], "Y")
        self.assertEqual(p(["transcribe", "--file", "a b.mp3"], "/w"),
                         {"page": "transcribe", "id": None, "files": ["/w/a b.mp3"]})
        self.assertEqual(p(["/x/1.mp4", "rel/2.ogg", "file:///x/3%204.ogg"], "/w")["files"],
                         ["/x/1.mp4", "/w/rel/2.ogg", "/x/3 4.ogg"])
        self.assertEqual(p(["files"], "/w")["page"], "files")
        self.assertEqual(p(["a.ogg", "files"], "/w")["files"], ["/w/a.ogg", "/w/files"])  # page only first
        self.assertEqual(p(["--", "--weird.ogg"], "/w")["files"], ["/w/--weird.ogg"])
        with self.assertRaises(ValueError):
            p(["--bogus"], "/w")
        with self.assertRaises(ValueError):
            p(["--id"], "/w")

    def test_parse_transcribe_lines(self):
        f = vn.parse_transcribe_line
        r = f("npu-transcribe: loading whisper-large-v3-turbo-int8-ov on NPU (the first NPU run compiles "
              "the model: ~1 min small, ~4 min turbo; cached afterwards)\n")
        self.assertEqual((r["kind"], r["compile"]), ("loading", True))
        self.assertEqual(f("npu-transcribe: loading whisper-small-int8-ov on CPU")["compile"], False)
        self.assertEqual(f("npu-transcribe: reloading the NPU pipeline so language auto-detection "
                           "starts fresh")["kind"], "loading")
        self.assertEqual(f("npu-transcribe: model ready on NPU in 3.2 s")["kind"], "ready")
        self.assertEqual(f("npu-transcribe: language: ru (auto-detected from the first block)")["language"], "ru")
        r = f("talk: part 1.ogg:  40%  00:02:00/00:05:00  elapsed 00:00:10  speed 12.0x")
        self.assertEqual(r["kind"], "progress")
        self.assertAlmostEqual(r["fraction"], 0.4)
        self.assertEqual((r["done"], r["total"], r["speed"]), (120, 300, 12.0))
        r = f("x.ogg:   ?%  00:02:00/?  elapsed 00:00:10  speed -")
        self.assertEqual((r["kind"], r["fraction"], r["speed"]), ("progress", None, None))
        r = f("x.ogg: 100%  01:00:00/01:00:00  elapsed 00:05:00  speed 12.0x")
        self.assertEqual(r["fraction"], 1.0)
        self.assertEqual(f("npu-transcribe: ERROR: x.ogg: no audio stream in this file")["text"],
                         "x.ogg: no audio stream in this file")
        self.assertEqual(f("npu-transcribe: error: argument --model: expected one argument")["kind"], "error")
        self.assertEqual(f("npu-transcribe: WARNING: NPU not available")["kind"], "warning")
        self.assertEqual(f("npu-transcribe: done: x.ogg: 00:00:05 of audio")["kind"], "done")
        self.assertEqual(f("npu-transcribe: interrupted")["kind"], "interrupted")
        self.assertEqual(f("npu-transcribe: [1/2] /x/a.ogg")["kind"], "info")
        self.assertEqual(f("Traceback (most recent call last):")["kind"], "other")

    def test_load_meetings(self):
        ms = vn.load_meetings()
        ids, t = FIX["ids"], FIX["t"]
        self.assertEqual([m.id for m in ms], [ids["D"], ids["C"], ids["B"], ids["A"]])
        by = {m.id: m for m in ms}
        self.assertTrue(by[ids["A"]].md_path.endswith(f"{minute_stamp(t['A'])}-meeting.md"))
        self.assertTrue(by[ids["B"]].md_path.endswith(f"{minute_stamp(t['B'] + 10)}-meeting.md"))
        self.assertIsNone(by[ids["C"]].md_path)
        self.assertIsNone(by[ids["D"]].md_path)
        self.assertEqual(by[ids["A"]].duration(), 3600)
        self.assertIsNone(by[ids["D"]].duration())
        self.assertEqual(vn.load_meetings(WORK / "nope.db"), [])

    def test_assign_same_minute(self):
        d = WORK / "same-minute"
        d.mkdir(exist_ok=True)
        t0 = epoch(2026, 9, 1, 12, 0, 10)
        first, second = d / f"{minute_stamp(t0)}-meeting.md", d / f"{minute_stamp(t0)}-meeting-{t0 + 99}.md"
        first.write_text("1")
        second.write_text("2")
        os.utime(first, (t0 + 60, t0 + 60))
        os.utime(second, (t0 + 120, t0 + 120))  # the later meeting's copy is written later
        m1, m2 = vn.Meeting("1", "one", t0), vn.Meeting("2", "two", t0 + 30)
        m3 = vn.Meeting("3", "other day", t0 + 86400)
        vn.assign_meeting_mds([m2, m3, m1], d)
        self.assertEqual((m1.md_path, m2.md_path, m3.md_path), (str(first), str(second), None))

    def test_assign_same_minute_after_the_pass(self):
        """A meeting stopped and the next started at once (2026-10-15): the first one's speaker
        pass rewrote its file after the second one's was written, so modification times
        no longer tell them apart; a title does only when no other file of that minute has it,
        else the order they were written in. The app and bmeet-ctl (the widget) agree."""
        d = WORK / "same-minute-2"
        d.mkdir(exist_ok=True)
        t0 = epoch(2026, 10, 15, 11, 35, 27)
        stamp = minute_stamp(t0)
        short, room = d / f"{stamp}-meeting.md", d / f"{stamp}-meeting-{t0 + 2500}.md"
        theirs = d / f"{stamp}-meeting-speakers.md"            # the pass's version of an edited copy
        theirs2 = d / f"{stamp}-meeting-{t0 + 2500}-speakers.md"
        first, second = "Meeting 2026-10-15 11:35", "In-person meeting 2026-10-15 11:35"
        cases = [  # (file -> its title; None: no such file), meetings' titles, expected copies
            ({short: first, room: second}, (first, second), (short, room)),
            ({short: second, room: first}, (first, second), (room, short)),    # titles say which
            ({short: second, room: second}, (first, second), (short, room)),   # 2026-10-15 itself
            ({short: first, room: first}, (first, first), (short, room)),      # written order
            ({short: None, room: second}, (first, second), (None, room)),      # not the next one's
            ({short: first, room: second, theirs: first}, (first, second), (short, room)),
            ({short: None, room: first}, (first, first), (room, None)),        # (ends unknown)
            ({short: None, room: first}, (first, first, 10, 2500), (None, room)),  # its end says whose
            ({short: first, theirs: first, room: second, theirs2: second}, (first, second), (short, room)),
            ({short: first, theirs: first, room: first, theirs2: first}, (first, first), (short, room)),
            ({short: first, theirs: first}, (first, second), (short, None)),   # still recording: not the twin
            ({short: "Budget review", theirs: first}, (first, second), (short, None)),  # you renamed it
            ({short: None, theirs: first}, (first, second), (theirs, None)),   # the twin alone is the copy
        ]
        for files, (ta, tb, *ends), want in cases:
            for f in (short, room, theirs, theirs2):
                f.unlink(missing_ok=True)
            for i, (f, title) in enumerate(files.items()):
                if title is not None:
                    f.write_text(f"# {title}\n")
                    os.utime(f, (t0 + 2600 - i, t0 + 2600 - i))   # (the first one rewritten last)
            ea, eb = (t0 + ends[0], t0 + ends[1]) if ends else (None, None)
            a, b = vn.Meeting("a", ta, t0, ended_at=ea), vn.Meeting("b", tb, t0 + 12, ended_at=eb)
            vn.assign_meeting_mds([b, a], d)
            self.assertEqual((a.md_path, b.md_path), tuple(str(x) if x else None for x in want), files)
            saved, vn.ctl.MEETING_MD_DIR = vn.ctl.MEETING_MD_DIR, d
            try:
                got = vn.ctl.pair_mds([("a", t0, ta, ea), ("b", t0 + 12, tb, eb)])
            finally:
                vn.ctl.MEETING_MD_DIR = saved
            self.assertEqual((got.get("a"), got.get("b")), (a.md_path, b.md_path), files)
        for f in (short, room, theirs, theirs2):
            f.unlink(missing_ok=True)

    def test_pass_running_on_the_right_meeting(self):
        """The marker names the transcript its pass replaces: the other meeting of the minute
        is not busy meanwhile. A marker that names none (before 0.8.1): the minute's meetings."""
        stamp = "2026-10-15_1135"
        page = vn.MeetingsPage.__new__(vn.MeetingsPage)
        page.live_id, page.live_state = None, "idle"
        a = vn.Meeting("a", "A", 0, md_path=f"/x/{stamp}-meeting.md")
        b = vn.Meeting("b", "B", 0, md_path=f"/x/{stamp}-meeting-1792059436.md")
        page.speakers_pending = {b.md_path}
        self.assertEqual((vn.MeetingsPage.pass_running(page, a), vn.MeetingsPage.pass_running(page, b)), (False, True))
        page.speakers_pending = {stamp}
        self.assertEqual((vn.MeetingsPage.pass_running(page, a), vn.MeetingsPage.pass_running(page, b)), (True, True))
        page.speakers_pending = {f"/x/./{stamp}-meeting-1792059436.md"}   # (spelt another way)
        self.assertTrue(vn.MeetingsPage.pass_running(page, b))
        # no copy yet (the export failed): the one the pass is going to write is of its minute
        t = epoch(2026, 10, 15, 11, 35, 27)
        c = vn.Meeting("c", "C", t, ended_at=t + 600)
        page.speakers_pending = {f"/x/{stamp}-meeting.md"}
        self.assertTrue(vn.MeetingsPage.pass_running(page, c))
        page.speakers_pending = {"/x/2026-10-15_1210-meeting.md"}
        self.assertFalse(vn.MeetingsPage.pass_running(page, c))
        # ... but not the next meeting of that minute, still recording (its pass comes later)
        d = vn.Meeting("d", "D", t + 12)
        page.speakers_pending, page.live_id, page.live_state = {f"/x/{stamp}-meeting.md"}, "d", "recording"
        self.assertFalse(vn.MeetingsPage.pass_running(page, d))
        page.live_state = "idle"                   # (stopping: not live any more, not ended yet)
        self.assertFalse(vn.MeetingsPage.pass_running(page, d))
        page.live_state, page.speakers_pending = "recording", {stamp}   # an old marker: still not while live
        self.assertFalse(vn.MeetingsPage.pass_running(page, d))

    def test_widget_recent_pairs_past_its_limit(self):
        """bmeet-ctl recent (the widget): the oldest meeting listed must not get the copy of a
        meeting of the same minute that is just past the list's end."""
        d = WORK / "recent-limit"
        d.mkdir(exist_ok=True)
        t0 = epoch(2026, 10, 15, 11, 35, 27)
        stamp = minute_stamp(t0)
        mine, first = d / f"{stamp}-meeting-{t0 + 2500}.md", d / f"{stamp}-meeting.md"
        first.write_text("# Meeting 2026-10-15 11:35\n")
        mine.write_text("# Meeting 2026-10-15 11:35\n")   # (a pair from before 0.8.1: one title)
        db = d / "index.db"
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE meetings (id TEXT PRIMARY KEY, title TEXT, started_at INTEGER, ended_at INTEGER, "
                    "duration_secs INTEGER, status TEXT)")
        con.executemany("INSERT INTO meetings VALUES (?, ?, ?, ?, ?, 'completed')", [
            ("a", "Meeting 2026-10-15 11:35", t0, t0 + 10, 10),
            ("b", "Meeting 2026-10-15 11:35", t0 + 12, t0 + 2500, 2488),
            ("c", "Later", t0 + 9000, t0 + 9600, 600)])
        con.commit()
        con.close()
        saved = vn.ctl.MEETING_MD_DIR, vn.ctl.MEETING_DB, vn.ctl.FILE_HISTORY
        vn.ctl.MEETING_MD_DIR, vn.ctl.MEETING_DB, vn.ctl.FILE_HISTORY = d, db, d / "none.jsonl"
        try:
            items = vn.ctl.recent(2)
        finally:
            vn.ctl.MEETING_MD_DIR, vn.ctl.MEETING_DB, vn.ctl.FILE_HISTORY = saved
        self.assertEqual([(i["id"], i["path"]) for i in items], [("c", None), ("b", str(mine))])

    def test_assign_neighbouring_minute_by_title(self):
        # The toggle stamped the file 15:32; voxtype said the meeting started at 15:33:00.
        d = WORK / "next-minute"
        d.mkdir(exist_ok=True)
        t0 = epoch(2026, 10, 13, 15, 33, 0)
        mine = d / f"{minute_stamp(t0 - 5)}-meeting.md"
        mine.write_text("# In-person meeting 2026-10-13 15:32\n\ntext\n")
        other = d / f"{minute_stamp(t0 + 60)}-meeting.md"   # someone else's, a minute later
        other.write_text("# Meeting 2026-10-13 15:34\n")
        m = vn.Meeting("1", "In-person meeting 2026-10-13 15:32", t0)
        n = vn.Meeting("2", "Another title", t0 + 200)
        vn.assign_meeting_mds([m, n], d)
        self.assertEqual((m.md_path, n.md_path), (str(mine), None))
        saved, vn.ctl.MEETING_MD_DIR = vn.ctl.MEETING_MD_DIR, d  # bmeet-ctl recent (the widget)
        try:
            self.assertEqual(vn.ctl.meeting_md(t0, "In-person meeting 2026-10-13 15:32"), str(mine))
            self.assertIsNone(vn.ctl.meeting_md(t0, "Something else"))
        finally:
            vn.ctl.MEETING_MD_DIR = saved

    def test_mac_copy_next_to_the_index(self):
        """On a Mac, macos/bmeet-meeting saves the copy next to the index when it cannot write in
        the Meetings folder (no access to Documents): the window and bmeet-ctl recent find it
        there too. On Linux that folder is voxtype's, and not looked in."""
        d = WORK / "spare"
        (d / "Meetings").mkdir(parents=True, exist_ok=True)
        t0 = epoch(2026, 10, 23, 19, 27, 5)
        copy = d / f"{minute_stamp(t0)}-meeting.md"
        copy.write_text("# Meeting 2026-10-23 19:27\n")
        db = d / "index.db"
        db.unlink(missing_ok=True)
        con = sqlite3.connect(db)
        con.executescript(MEETINGS_SCHEMA)
        con.execute("INSERT INTO meetings (id, title, started_at, ended_at, duration_secs, status) "
                    "VALUES ('a', 'Meeting 2026-10-23 19:27', ?, ?, 60, 'completed')", (t0, t0 + 60))
        con.commit()
        con.close()
        saved = vn.MAC, vn.ctl.MAC, vn.ctl.MEETING_MD_DIR, vn.ctl.MEETING_DB, vn.ctl.FILE_HISTORY
        vn.ctl.MEETING_MD_DIR, vn.ctl.MEETING_DB, vn.ctl.FILE_HISTORY = d / "Meetings", db, d / "none.jsonl"
        try:
            for mac, want in ((False, None), (True, str(copy))):
                vn.MAC = vn.ctl.MAC = mac
                self.assertEqual([m.md_path for m in vn.load_meetings(db, d / "Meetings")], [want], mac)
                self.assertEqual([i["path"] for i in vn.ctl.recent(5)], [want], mac)
        finally:
            vn.MAC, vn.ctl.MAC, vn.ctl.MEETING_MD_DIR, vn.ctl.MEETING_DB, vn.ctl.FILE_HISTORY = saved

    def test_history_load_and_remove(self):
        path = WORK / "hist-copy.jsonl"
        shutil.copy(WORK / "history.jsonl", path)
        es = vn.load_history(path)
        self.assertEqual([e.name for e in es], ["gone.ogg", "broken.mp3", "talk.ogg"])  # newest first
        talk = es[2]
        self.assertEqual(talk.output(".md"), str(FIX["media"] / "talk.md"))
        self.assertEqual(talk.text_path(), str(FIX["media"] / "talk.md"))
        self.assertEqual([e.state() for e in es], ["Files missing", "Failed", "Done"])
        self.assertTrue(talk.matches_path(str(FIX["media"] / "talk.srt")))
        self.assertTrue(talk.matches_path(FIX["files"]["talk.ogg"]))
        self.assertEqual(vn.remove_history_entries({es[1].raw}, path), 1)
        self.assertEqual([e.name for e in vn.load_history(path)], ["gone.ogg", "talk.ogg"])
        self.assertIn("this is not json", path.read_text())  # other lines untouched
        self.assertEqual(vn.remove_history_entries({"nothing"}, path), 0)
        self.assertEqual(vn.load_history(WORK / "missing.jsonl"), [])

    def test_search(self):
        self.assertTrue(vn.matches([], "x"))
        self.assertTrue(vn.matches(vn.search_terms("Budget  WEEKLY"), "Weekly sync", "the budget"))
        self.assertFalse(vn.matches(vn.search_terms("budget roadmap"), "Weekly sync", "the budget"))
        self.assertTrue(vn.matches(vn.search_terms("привет"), None, "ПРИВЕТ мир"))

    def test_is_media(self):
        m = FIX["media"]
        self.assertTrue(vn.is_media(str(m / "talk.ogg")))
        self.assertTrue(vn.is_media(str(m / "x.mp4")))  # by name, need not exist
        self.assertTrue(vn.is_media(str(m / "x.mkv")))
        self.assertFalse(vn.is_media(str(m / "talk.md")))
        self.assertFalse(vn.is_media(str(m / "notes.txt")))
        self.assertFalse(vn.is_media(str(m / "list.m3u")))

    def test_ctl_status(self):
        run = WORK / "run"
        (run / "state").write_text("recording\n")
        (run / "meeting_state").write_text("paused\n")
        st = vn.ctl.status()
        self.assertEqual((st["daemon"], st["dictation"], st["meeting"]), ("active", "recording", "paused"))
        self.assertEqual(st["meeting_id"], FIX["ids"]["D"])
        self.assertEqual(st["meeting_started"], FIX["t"]["D"])
        (run / "state").write_text("idle\n")
        (run / "meeting_state").write_text("idle\n")
        self.assertEqual(vn.ctl.status()["meeting_id"], None)

    def test_ctl_on_a_mac(self):
        """bmeet-ctl's macOS side, with BMeet.app faked. macOS deletes BMeet.app's pid file from the
        temp folder after 3 days: BMeet.app is then asked. `bmeet FILE` (bmeet-ctl open) makes FILE
        absolute before BMeet.app starts the window, in /, and answers --version and --help itself."""
        import contextlib
        import io
        from types import SimpleNamespace as NS
        from unittest import mock
        ctl, sent, running = vn.ctl, [], [True]
        fake = NS(agent=lambda command, timeout=5.0: sent.append(command) or {"ok": True},
                  agent_running=lambda: running[0])
        with mock.patch.object(ctl, "MAC", True), mock.patch.object(ctl, "bmeetmac", fake, create=True), \
                mock.patch.object(ctl, "RUN", WORK / "run-mac"):   # (no pid file there)
            self.assertTrue(ctl.daemon_running())
            running[0] = False
            self.assertFalse(ctl.daemon_running())
            ctl.RUN = WORK / "run"   # (a pid file of a running process: no need to ask)
            self.assertTrue(ctl.daemon_running())
            here = os.getcwd()
            for args in (["talk.m4a"], ["meetings", "--id", "x y"], ["files", "--file", "a.md", "--file=b.md", "~/c.ogg"],
                         ["--id=1", "transcribe", "meetings", "--", "--x", "files"], ["file:///m/a%20b.ogg", "-"]):
                sent.clear()
                self.assertEqual(ctl.main(["open", *args]), 0)
                self.assertEqual(sent[0]["op"], "open")
                self.assertEqual(vn.parse_cli(sent[0]["args"], cwd="/"), vn.parse_cli(args, cwd=here), args)
            self.assertEqual(sent[0]["args"], ["file:///m/a%20b.ogg", os.path.join(here, "-")])
            sent.clear()
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                self.assertEqual(ctl.main(["open", "--version"]), 0)
                self.assertEqual(ctl.main(["open", "meetings", "--help"]), 0)
                self.assertEqual(ctl.main(["open", "--nope"]), 2)
                self.assertEqual(ctl.main(["open", "--id"]), 2)
            self.assertEqual(sent, [])
            self.assertTrue(out.getvalue().startswith(f"bmeet {ctl.__version__}\nBMeet - "), out.getvalue()[:80])
            self.assertIn("unknown option or missing value: --nope", err.getvalue())

    SPEAKER_MD = ("# Meeting\n\n## Meeting Info\n\n- **Speakers:** You, Person 1, Person 2, Person 3\n\n"
                  "## Transcript\n\n### Person 1\n\n*[00:00]* Hello, Person 2 here is late.\n\n"
                  "### You\n\n*[00:04]* Hi.\n\n### Person 2\n\n*[00:06]* Sorry.\n\n"
                  "### Person 3\n\n*[00:09]* Me again.\n")

    def test_md_speakers_and_rename(self):
        self.assertEqual(vn.md_speakers(self.SPEAKER_MD),
                         [("Person 1", "Hello, Person 2 here is late."), ("Person 2", "Sorry."),
                          ("Person 3", "Me again.")])
        out = vn.rename_speakers(self.SPEAKER_MD, {"Person 1": "Viktor", "Person 3": "Viktor",
                                                   "Person 2": "Анна"})
        self.assertIn("- **Speakers:** You, Viktor, Анна\n", out)
        self.assertIn("### Viktor\n\n*[00:00]* Hello, Person 2 here is late.", out)  # text untouched
        self.assertIn("### Анна\n", out)
        self.assertEqual(out.count("### Viktor"), 2)
        self.assertNotIn("Person 3", out)
        self.assertTrue(out.endswith("Me again.\n"))
        self.assertEqual(vn.rename_speakers("### Person 1", {"Person 1": "A"}), "### A")  # no newline at end

    def test_name_problems(self):
        for ok in ("Viktor", "Анна Петрова", "Dr. O'Neil"):
            self.assertIsNone(vn.name_problem(ok), ok)
        for bad in ("", "You", "Person 4", "Remote", "a, b", "# x", "x" * 41, "*bold*"):
            self.assertIsNotNone(vn.name_problem(bad), bad)

    def test_remember_named_voices(self):
        voices = WORK / "voices"
        md = str(WORK / "Meetings/2026-10-12_1041-meeting.md")
        side = vn.meeting_voices_path(md)
        self.assertEqual(side, voices / "meetings/2026-10-12_1041-meeting.json")
        try:
            self.assertEqual(vn.remember_named(md, {"Person 1": "Viktor"}), [])  # no fingerprints
            side.parent.mkdir(parents=True)
            side.write_text(json.dumps({"version": 1, "model": "m.onnx",
                                        "people": {"Person 1": [1.0, 0.0], "Person 2": [0.0, 1.0]}}))
            self.assertEqual(vn.remember_named(md, {"Person 1": "Viktor"}), ["Viktor"])
            store = json.loads((voices / "voices.json").read_text())
            self.assertEqual(store["model"], "m.onnx")
            self.assertEqual(store["voices"]["Viktor"]["fingerprints"], [[1.0, 0.0]])
            self.assertEqual((voices / "voices.json").stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(side.read_text())["people"],
                             {"Viktor": [1.0, 0.0], "Person 2": [0.0, 1.0]})
            # a typo fixed: this meeting's fingerprint moves to the new name
            self.assertEqual(vn.remember_named(md, {"Viktor": "Victor"}), ["Victor"])
            self.assertEqual(vn.remembered_voices(), {"Victor": 1})
            # the same person in another meeting: a second fingerprint
            md2 = str(WORK / "Meetings/2026-10-13_0900-meeting.md")
            vn.meeting_voices_path(md2).write_text(json.dumps({"model": "m.onnx",
                                                               "people": {"Person 1": [0.9, 0.1]}}))
            vn.remember_named(md2, {"Person 1": "Victor"})
            self.assertEqual(vn.remembered_voices(), {"Victor": 2})
            vn.forget_voice("Victor")
            self.assertEqual(vn.remembered_voices(), {})
            vn.set_remember_voices(True)
            self.assertTrue(vn.remember_voices_on())
            vn.set_remember_voices(False)  # off forgets everything
            self.assertFalse(vn.remember_voices_on())
            self.assertFalse(voices.exists())
        finally:
            shutil.rmtree(voices, ignore_errors=True)
            vn.REMEMBER_FILE.unlink(missing_ok=True)

    IN_PERSON_MD = ("# In-person meeting\n\n## Meeting Info\n\n- **Speakers:** You, Person 1\n"
                    "- **Microphone:** split by voice (in-person meeting)\n\n## Transcript\n\n"
                    "### You\n\n*[00:00]* Shall we start?\n\n### Person 1\n\n*[00:03]* Yes.\n")

    def test_in_person_you_can_be_named(self):
        self.assertTrue(vn.mic_split(self.IN_PERSON_MD))
        self.assertFalse(vn.mic_split(self.SPEAKER_MD))
        self.assertEqual(vn.md_speakers(self.IN_PERSON_MD), [("You", "Shall we start?"), ("Person 1", "Yes.")])
        self.assertIsNotNone(vn.name_problem("You"))
        self.assertIsNone(vn.name_problem("You", you_allowed=True))
        self.assertIsNotNone(vn.name_problem("Person ?", you_allowed=True))
        out = vn.rename_speakers(self.IN_PERSON_MD, {"You": "Anna", "Person 1": "You"})
        self.assertIn("### Anna\n\n*[00:00]* Shall we start?", out)
        self.assertIn("### You\n\n*[00:03]* Yes.", out)
        self.assertIn("- **Speakers:** Anna, You\n", out)

    def test_remember_your_own_voice(self):
        voices = WORK / "voices"
        md = str(WORK / "Meetings/2026-10-13_1500-meeting.md")
        side = vn.meeting_voices_path(md)
        try:
            side.parent.mkdir(parents=True)
            # npu-transcribe learned your voice from an online meeting before
            (voices / "you.json").write_text(json.dumps({"version": 1, "model": "m.onnx",
                                                         "fingerprints": [[0.0, 1.0]], "meetings": ["online"]}))
            # in person it took Anna for you, and you for Person 1
            side.write_text(json.dumps({"model": "m.onnx", "people": {"You": [1.0, 0.0], "Person 1": [0.0, 0.9]}}))
            self.assertEqual(vn.own_voice_meetings(), 1)
            self.assertEqual(sorted(vn.remember_named(md, {"You": "Anna", "Person 1": "You"})), ["Anna", "You"])
            own = json.loads((voices / "you.json").read_text())
            self.assertEqual(own["fingerprints"], [[0.0, 1.0], [0.0, 0.9]])
            self.assertEqual(own["meetings"], ["online", side.stem])
            self.assertEqual(vn.remembered_voices(), {"Anna": 1})  # You is not a remembered name
            self.assertEqual(json.loads(side.read_text())["people"], {"Anna": [1.0, 0.0], "You": [0.0, 0.9]})
            # changed your mind: that was Boris after all
            vn.remember_named(md, {"You": "Boris"})
            self.assertEqual(json.loads((voices / "you.json").read_text())["fingerprints"], [[0.0, 1.0]])
            self.assertEqual(vn.remembered_voices(), {"Anna": 1, "Boris": 1})
            vn.forget_own_voice()
            self.assertEqual(vn.own_voice_meetings(), 0)
        finally:
            shutil.rmtree(voices, ignore_errors=True)

    def test_ctl_speakers_pending(self):
        audio = WORK / "meeting-audio"
        self.assertEqual(vn.ctl.status()["speakers_pending"], [])   # no folder at all
        (audio / "2026-10-12_1041").mkdir(parents=True)               # still recording
        (audio / "2026-10-12_0900").mkdir()
        (audio / "2026-10-12_0900" / "speaker-pass").touch()          # pass running
        (audio / "2026-10-12_1041-2").mkdir()                         # a second one that minute,
        (audio / "2026-10-12_1041-2" / "speaker-pass").write_text("/m/2026-10-12_1041-meeting-9.md\n")
        try:                                                          # (its marker names its transcript)
            self.assertEqual(vn.ctl.status()["speakers_pending"],
                             ["/m/2026-10-12_1041-meeting-9.md", "2026-10-12_0900"])
        finally:
            shutil.rmtree(audio)

    def test_export_from_the_markdown_copy(self):
        md = ("# In-person meeting 2026-10-14 13:29\n\n## Meeting Info\n\n- **Date:** 2026-10-14 13:29\n"
              "- **Duration:** 32m 1s\n- **Speakers:** Денис, You, Person 2\n- **Microphone:** split by voice\n\n"
              "## Transcript\n\n### Денис\n\n*[00:00]* — Вот это метан, типа?\n\n*[00:31]* Да.\n\n"
              "### You\n\n*[01:02]* Нет, это пропан.\n\n### Person 2\n\n*[1:00:05]* Всё, спасибо.\n")
        m = vn.Meeting(id="ID", title="voxtype's title", started_at=1791977340, ended_at=None,
                       duration_secs=1921, status="completed", chunk_count=60, storage_path=None, model=None)
        title, info, paragraphs = vn.md_meeting(md)
        self.assertEqual(title, "In-person meeting 2026-10-14 13:29")
        self.assertEqual(info["Speakers"], "Денис, You, Person 2")
        self.assertEqual(paragraphs, [("00:00", "Денис", "— Вот это метан, типа?"), ("00:31", "Денис", "Да."),
                                      ("01:02", "You", "Нет, это пропан."), ("1:00:05", "Person 2", "Всё, спасибо.")])
        self.assertEqual(vn.meeting_export(m, md, "markdown"), md)
        txt = vn.meeting_export(m, md, "text")
        self.assertTrue(txt.startswith("In-person meeting 2026-10-14 13:29\nDate: 2026-10-14 13:29\n"), txt)
        self.assertIn("[00:00] Денис:\n— Вот это метан, типа?\n[00:31] Да.\n\n[01:02] You:\nНет, это пропан.", txt)
        js = json.loads(vn.meeting_export(m, md, "json"))
        self.assertEqual(js["metadata"]["title"], "In-person meeting 2026-10-14 13:29")
        self.assertEqual(js["transcript"]["speakers"], ["Денис", "You", "Person 2"])
        self.assertEqual([(x["startMs"], x["speaker"]) for x in js["transcript"]["segments"]],
                         [(0, "Денис"), (31000, "Денис"), (62000, "You"), (3605000, "Person 2")])
        self.assertEqual(js["transcript"]["wordCount"], 11)

    def test_export_cmd(self):
        self.assertEqual(vn.export_cmd("ID", "json", "/o.json")[-5:], ["--metadata", "--output", "/o.json", "--", "ID"])
        cmd = vn.export_cmd("ID")
        self.assertEqual(cmd[1:5], ["meeting", "export", "--format", "markdown"])
        self.assertEqual(cmd[-2:], ["--", "ID"])
        self.assertIn("--timestamps", cmd)


    def test_paragraphs_redo_only_the_end(self):
        """MarkdownView.show_paragraphs: the same page as a full render, without one."""
        para = lambda i: f"*[00:{i:02d}]* Line {i} of the meeting."
        start = ["### You"] + [para(i) for i in range(5)] + ["### Remote", para(5), "*still talking …*"]
        cases = [start[:-1] + [para(6), "*more …*"],                         # a line final, a new draft
                 start[:-1] + [para(6), "### You", para(7)],                 # a new heading at the end
                 start[:3] + ["*[00:02]* Line 2 changed."] + start[4:],      # one in the middle
                 start[:-2],                                                 # some gone at the end
                 ["*(nothing said yet)*"]]
        for case in cases:
            view, full = vn.MarkdownView(), vn.MarkdownView()
            view.show_paragraphs(start)
            full_renders = []
            view.show_markdown = lambda text, f=view.show_markdown: (full_renders.append(1), f(text))
            view.show_paragraphs(case)
            full.setMarkdown("\n\n".join(case))
            self.assertEqual(view.toHtml(), full.toHtml(), case)
            self.assertEqual(view.document().blockCount(), len(case))
            if case is cases[0] or case is cases[1]:
                self.assertEqual(full_renders, [])   # only the end was redone


# ============================================================================ updates

def bump(version: str, part: int = 2) -> str:
    v = [int(x) for x in version.split(".")]
    v[part] += 1
    return ".".join(map(str, v[:part + 1] + [0] * (2 - part)))


class A_UpdateTests(unittest.TestCase):
    """bmeet-ctl update: this system's newest release on GitHub (a file here), what it keeps, and
    the installer it runs. The window's banner is in B_WindowTests."""

    def setUp(self):
        self.ctl = vn.ctl
        self.installed = (REPO / "VERSION").read_text().strip()
        self.next = bump(self.installed)
        shutil.rmtree(WORK / "cache", ignore_errors=True)
        (WORK / "config/bmeet/update-check").write_text("on\n")
        (WORK / "data/bmeet").mkdir(parents=True, exist_ok=True)
        self.releases([("v" + self.next, False, False), ("v" + self.installed, False, False)])

    def tearDown(self):
        (WORK / "config/bmeet/update-check").write_text("off\n")
        (WORK / "run/meeting_state").write_text("idle\n")
        shutil.rmtree(WORK / "cache", ignore_errors=True)
        (WORK / "data/bmeet/install-options").unlink(missing_ok=True)

    def releases(self, rows) -> None:
        (WORK / "releases.json").write_text(json.dumps([
            {"tag_name": tag, "draft": draft, "prerelease": pre, "html_url": f"https://example.org/{tag}",
             "published_at": "2026-10-28T10:00:00Z"} for tag, draft, pre in rows]))

    def ctl_run(self, *args, env=None) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-I", str(CTL_PATH), *args], capture_output=True, text=True,
                              timeout=60, env={**os.environ, **(env or {})})

    def check_json(self, *extra) -> dict:
        r = self.ctl_run("update", "--check", "--json", *extra)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def test_the_newest_release_of_this_system(self):
        # another system's release, a draft and a pre-release are newer, and not for here
        self.releases([("macos-v" + bump(self.next, 0), False, False), ("v" + bump(self.next, 1), True, False),
                       ("v" + bump(self.next, 1), False, True), ("v" + self.installed, False, False),
                       ("v" + self.next, False, False), ("vnot-a-version", False, False)])
        first = self.check_json()
        self.assertEqual(first["installed"], self.installed)
        self.assertEqual(first["update"], {"version": self.next, "tag": "v" + self.next, "installed": self.installed,
                                           "url": f"https://example.org/v{self.next}"})
        self.assertTrue(first["new"])                       # BMeet.app says so once
        self.assertFalse(self.check_json()["new"])
        st = self.ctl.status()                             # the window and the widget: no network
        self.assertEqual((st["version"], st["update"]["version"]), (self.installed, self.next))
        r = self.ctl_run("update", "--check")
        self.assertIn(f"BMeet {self.next} is available (this is {self.installed})", r.stdout)

    def test_none_newer(self):
        self.releases([("v" + self.installed, False, False), ("macos-v" + self.next, False, False)])
        self.assertIsNone(self.check_json()["update"])
        self.assertIsNone(self.ctl.status()["update"])
        self.assertIn(f"BMeet {self.installed} is the newest release", self.ctl_run("update", "--check").stdout)
        r = self.ctl_run("update")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("is the newest release", r.stdout)
        # a check that saw a release, after the update to it: nothing left to show
        (WORK / "cache/bmeet/update.json").write_text(json.dumps({
            "platform": "linux", "latest": {"version": self.installed, "tag": "v" + self.installed}}))
        self.assertIsNone(self.ctl.status()["update"])

    def test_automatic_checks_wait(self):
        missing = {"BMEET_RELEASES_URL": (WORK / "missing.json").as_uri()}
        self.check_json()                                 # asked now: the answer is kept
        r = self.ctl_run("update", "--check", "--auto", "--json", env=missing)
        self.assertEqual((r.returncode, json.loads(r.stdout)["error"]), (0, None))     # a day to go: not asked
        self.assertEqual(json.loads(r.stdout)["update"]["version"], self.next)
        state = json.loads((WORK / "cache/bmeet/update.json").read_text())
        state["tried"] -= 86400 + 5
        (WORK / "cache/bmeet/update.json").write_text(json.dumps(state))
        r = self.ctl_run("update", "--check", "--auto", "--json", env=missing)
        answer = json.loads(r.stdout)
        self.assertEqual(r.returncode, 0)                  # (the apps do not care; the next try says)
        self.assertIn("cannot reach GitHub", answer["error"])
        self.assertEqual(answer["update"]["version"], self.next)    # the last answer still holds
        r = self.ctl_run("update", "--check", "--auto", "--json")   # an hour after a failed try
        self.assertIsNone(json.loads(r.stdout)["error"])
        self.assertFalse(json.loads((WORK / "cache/bmeet/update.json").read_text())["ok"])   # (not asked)
        self.assertEqual(self.ctl_run("update", "--check", env=missing).returncode, 1)     # you asked: it says
        # automatic checks off: never asked
        shutil.rmtree(WORK / "cache")
        (WORK / "config/bmeet/update-check").write_text("off\n")
        r = self.ctl_run("update", "--check", "--auto", "--json")
        self.assertIsNone(json.loads(r.stdout)["update"])
        self.assertFalse((WORK / "cache/bmeet/update.json").exists())

    def test_the_release_installer_with_the_kept_options(self):
        log = WORK / "installer-args"
        installer = WORK / f"install-v{self.next}.sh"
        installer.write_text(f'#!/bin/bash\nprintf "%s\\n" "$@" > {shlex.quote(str(log))}\n')
        (WORK / "data/bmeet/install-options").write_text("--no-turbo\n--no-panel\nnot an option\n")
        try:
            r = self.ctl_run("update", "--", "--skip-system")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn(f"Updating BMeet {self.installed} -> {self.next}", r.stdout)
            self.assertEqual(log.read_text().split(), ["--no-turbo", "--no-panel", "--skip-system"])
            log.unlink()
            # not while a meeting is recorded, or its speaker pass runs (the installer restarts both)
            (WORK / "run/meeting_state").write_text("recording\n")
            r = self.ctl_run("update")
            self.assertEqual(r.returncode, 1)
            self.assertIn("a meeting is being recorded", r.stderr)
            (WORK / "run/meeting_state").write_text("idle\n")
            rec = WORK / "meeting-audio/2026-10-28_1000"
            rec.mkdir(parents=True)
            (rec / "speaker-pass").write_text("x.md\n")
            r = self.ctl_run("update")
            shutil.rmtree(WORK / "meeting-audio")
            self.assertEqual(r.returncode, 1)
            self.assertIn("telling the speakers", r.stderr)
            self.assertFalse(log.exists())
            # an installer that fails: its exit code
            installer.write_text("#!/bin/bash\nexit 3\n")
            self.assertEqual(self.ctl_run("update").returncode, 3)
        finally:
            installer.unlink(missing_ok=True)
            log.unlink(missing_ok=True)

    def test_in_a_terminal(self):
        args = WORK / "terminal-args"
        term = WORK / "fake-terminal"
        term.write_text(f'#!/bin/bash\nprintf "%s\\n" "$@" > {shlex.quote(str(args))}\n')
        term.chmod(0o755)
        try:
            r = self.ctl_run("update", "--terminal", env={"BMEET_TERMINAL": f"{term} -e"})
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertTrue(wait_until(args.exists, 5))
            got = args.read_text().splitlines()
            self.assertEqual(got[:3], ["-e", "bash", "-c"])
            self.assertIn(f"{CTL_PATH} update;", got[3])
            self.assertIn("read -r -n 1", got[3])         # the window stays open at the end
        finally:
            args.unlink(missing_ok=True)
            term.unlink(missing_ok=True)

    def test_wrong_use(self):
        for args in (["--bogus"], ["--json"], ["--terminal", "--check"], ["--check", "--", "--no-turbo"],
                     ["--auto"]):
            r = self.ctl_run("update", *args)
            self.assertEqual(r.returncode, 2, args)
            self.assertIn("usage: bmeet-ctl update", r.stderr)


# ============================================================================ window

class B_WindowTests(unittest.TestCase):
    win: vn.MainWindow
    errors: list

    @classmethod
    def setUpClass(cls):
        cls.errors = []
        cls.win = vn.MainWindow()
        cls.win.error_box = lambda title, text: cls.errors.append((title, text))
        cls.win.handle_request({"page": None, "id": None, "files": []}, initial=True)
        cls.win.show()
        wait_until(lambda: False, 0.3)

    @classmethod
    def tearDownClass(cls):
        cls.win.force_quit = True
        cls.win.close()

    def setUp(self):
        self.errors.clear()

    def state(self, dictation="idle", meeting="idle", pid=None):
        run = WORK / "run"
        (run / "state").write_text(dictation + "\n")
        (run / "meeting_state").write_text(meeting + "\n")
        (run / "pid").write_text(f"{pid or os.getpid()}\n")
        self.win.poll()
        app.processEvents()

    def select(self, page, key):
        self.assertTrue(page.select_key(key), key)
        app.processEvents()

    def test_update_banner(self):
        """A newer release (bmeet-ctl's last check): the banner, its buttons, Later. A newer version
        installed while the window runs: Restart, which opens the new window once this one is gone."""
        w, b = self.win, self.win.update_banner
        cache = WORK / "cache/bmeet/update.json"
        installed = (REPO / "VERSION").read_text().strip()
        new = bump(installed)
        detached, opened = [], []
        orig = vn.run_detached, vn.open_url, vn.__version__
        vn.run_detached = lambda cmd: detached.append(cmd)
        vn.open_url = lambda url: opened.append(url.toString())
        try:
            self.assertTrue(b.wrapper.isHidden())
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps({"platform": "linux", "latest": {
                "version": new, "tag": f"v{new}", "url": f"https://example.org/v{new}"}}))
            self.state()
            self.assertFalse(b.wrapper.isHidden())
            self.assertEqual(b.text.text(), f"BMeet {new} is available (you have {installed}).")
            self.assertTrue(w.update_btn.isVisibleTo(b) and w.update_news_btn.isVisibleTo(b))
            self.assertFalse(w.restart_btn.isVisibleTo(b))
            w.update_news_btn.click()
            self.assertEqual(opened, [f"https://example.org/v{new}"])
            w.update_btn.click()
            self.assertEqual(detached[-1][-3:], [str(vn.CTL_PATH), "update", "--terminal"])
            w.hide_update()                                   # Later: not again for this release
            self.state()
            self.assertTrue(b.wrapper.isHidden())
            cache.write_text(json.dumps({"platform": "linux", "latest": {
                "version": bump(new), "tag": f"v{bump(new)}", "url": ""}}))
            self.state()                                      # a newer one: shown again
            self.assertFalse(b.wrapper.isHidden())
            self.assertFalse(w.update_news_btn.isVisibleTo(b))   # (no release page)
            cache.unlink()
            self.state()
            self.assertTrue(b.wrapper.isHidden())
            # updated while it ran: this window is the old version
            vn.__version__ = "0.0.1"
            self.state()
            self.assertFalse(b.wrapper.isHidden())
            self.assertIn(f"BMeet {installed} is installed. Restart this window", b.text.text())
            self.assertTrue(w.restart_btn.isVisibleTo(b))
            self.assertFalse(w.update_btn.isVisibleTo(b))
            w2 = vn.MainWindow()                              # Restart: the new window after this one
            w2._restart = True
            w2.shutdown()
            sh = detached[-1]
            self.assertEqual(sh[:2], ["/bin/sh", "-c"])
            self.assertEqual(sh[4:], [str(os.getpid()), sys.executable, str(vn.CTL_PATH), w2.page()])
            w2.deleteLater()
        finally:
            vn.run_detached, vn.open_url, vn.__version__ = orig
            cache.unlink(missing_ok=True)
            w._update_hidden = None
            self.state()
        self.assertTrue(b.wrapper.isHidden())

    # -- structure
    def test_00_settings_are_the_tests_own(self):
        """The window keeps its settings in $XDG_CONFIG_HOME/bmeet/bmeet.conf on every system (a
        Mac's QSettings would use ~/Library/Preferences): here, the work dir's."""
        self.assertEqual(Path(self.win.settings.fileName()), WORK / "config/bmeet/bmeet.conf")
        self.assertEqual(self.win.settings.format(), vn.QSettings.Format.IniFormat)

    def test_01_pages_build(self):
        w = self.win
        for i, name in enumerate(vn.PAGES):
            w.set_page(name)
            self.assertEqual(w.stack.currentIndex(), i)
            self.assertEqual(w.windowTitle(), vn.PAGE_TITLES[name])
            self.assertEqual(w.page(), name)
        self.assertEqual(w.nav.item(0).text(), "Meetings (4)")
        self.assertEqual(w.nav.item(1).text(), "Transcribed files (3)")
        self.assertEqual(w.nav.item(2).text(), "Transcribe a file")
        self.assertFalse(w.ctl_banner.wrapper.isVisible())
        self.assertTrue(w.toolbar.isVisible())
        self.assertIsNone(w.createPopupMenu())
        self.assertEqual(w.meetings_page.tree.topLevelItemCount(), 4)

    # -- control bar
    def test_02_status_idle(self):
        self.state("idle", "idle")
        w = self.win
        self.assertEqual(w.dict_label.text(), "Ready to dictate")
        self.assertTrue(w.act_dictate.isEnabled())
        self.assertFalse(w.act_dictate.isChecked())
        self.assertEqual(w.act_dictate.text(), "Dictate to clipboard")
        self.assertIn("Meta+H", w.act_dictate.toolTip())
        self.assertFalse(w.act_cancel.isVisible())
        self.assertEqual(w.meet_label.text(), "No meeting")
        self.assertTrue(w.act_meet_start.isEnabled())
        self.assertFalse(w.act_meet_pause.isEnabled())
        self.assertFalse(w.act_meet_stop.isEnabled())
        self.assertEqual(w.act_lang.text(), "EN")  # from the test voxtype config
        self.assertTrue(w.act_lang.isEnabled())
        self.assertIn("German", w.act_lang.toolTip())  # next of "en de"
        self.assertFalse(w.daemon_banner.wrapper.isVisible())

    def test_03_status_recording_and_paused_meeting(self):
        self.state("recording", "paused")
        w = self.win
        self.assertEqual(w.dict_label.text(), "Listening…")
        self.assertTrue(w.act_dictate.isChecked())
        self.assertEqual(w.act_dictate.text(), "Stop dictation")
        self.assertTrue(w.act_cancel.isVisible())
        self.assertRegex(w.meet_label.text(), r"^Paused \d+:\d\d$")
        minutes, seconds = w.meet_label.text().split()[1].split(":")
        self.assertGreaterEqual(int(minutes) * 60 + int(seconds), 125)  # started 125+ s ago (DB time)
        self.assertEqual(w.act_meet_pause.text(), "Resume")
        self.assertTrue(w.act_meet_stop.isEnabled())
        self.assertFalse(w.act_meet_start.isEnabled())
        self.assertFalse(w.act_lang.isEnabled())
        self.assertIn("Stop the meeting", w.act_lang.toolTip())
        item = w.meetings_page.item_for(FIX["ids"]["D"])
        self.assertEqual(item.text(3), "Paused")
        frozen = w.meet_label.text()
        wait_until(lambda: False, 1.2)
        w.poll()
        self.assertEqual(w.meet_label.text(), frozen)  # the timer stands still while paused
        self.state("transcribing", "recording")
        self.assertEqual(w.dict_label.text(), "Transcribing…")
        self.assertFalse(w.act_dictate.isEnabled())
        self.assertRegex(w.meet_label.text(), r"^Meeting \d+:\d\d$")
        self.assertEqual(w.act_meet_pause.text(), "Pause")
        self.assertEqual(item.text(3), "Recording")
        self.state("idle", "idle")
        self.assertEqual(item.text(3), "Active")

    def test_04_actions(self):
        w = self.win
        detached, helpers = [], []

        def fake_detached(cmd):  # "voxtype-meeting-toggle stop" = a process that runs for 1 s
            detached.append(cmd)
            if cmd[-1] == "stop":
                helpers.append(subprocess.Popen(["sleep", "1"]))
                return helpers[-1].pid
            return None
        orig = vn.run_detached
        vn.run_detached = fake_detached
        vn.DRY_RUN = False  # real (fake-voxtype) calls for record/pause
        try:
            self.state("idle", "idle")
            w.act_dictate.trigger()
            self.assertFalse(w.act_dictate.isChecked())  # state comes from the daemon, not the click
            w.act_cancel.trigger()
            self.assertTrue(wait_until(lambda: ["record", "cancel"] in voxtype_calls()
                                       and ["record", "toggle", "--clipboard"] in voxtype_calls()), voxtype_calls())
            # dictation finished -> clipboard hint
            self.state("recording", "idle")
            w.act_dictate.trigger()
            self.state("transcribing", "idle")
            self.state("idle", "idle")
            self.assertIn("clipboard", w.statusBar().currentMessage())
            w.act_lang.trigger()
            self.assertTrue(detached[-1][0].endswith("voxtype-lang") and detached[-1][1] == "de")
            self.assertFalse(w.act_lang.isEnabled())
            self.assertEqual(w.act_lang.text(), "…")
            w._lang_pending = None
            w.act_meet_start.trigger()
            self.assertEqual(detached[-1][1:], ["start"])
            self.assertEqual(w.meet_label.text(), "Starting…")
            self.state("idle", "recording")
            w.act_meet_pause.trigger()
            self.assertTrue(wait_until(lambda: ["meeting", "pause"] in voxtype_calls()))
            self.state("idle", "paused")
            w.act_meet_pause.trigger()
            self.assertTrue(wait_until(lambda: ["meeting", "resume"] in voxtype_calls()))
            w.act_meet_stop.trigger()
            self.assertEqual(detached[-1][1:], ["stop"])
            self.assertEqual(w.meet_label.text(), "Saving transcript…")
            self.assertFalse(w.act_meet_start.isEnabled())
            self.assertFalse(w.act_meet_room.isEnabled())
            (WORK / "run" / "meeting_state").write_text("idle\n")
            self.assertTrue(wait_until(lambda: (w.poll(), w.meet_label.text())[1] == "No meeting", 5))
            self.assertEqual(w.statusBar().currentMessage(), "Meeting saved")
            self.assertEqual(w.meetings_page.current_key(), FIX["ids"]["D"])  # newest meeting selected
            self.assertTrue(w.act_meet_room.isEnabled())
            w.act_meet_room.trigger()  # in person: microphone only
            self.assertEqual(detached[-1][1:], ["start", "--in-person"])
            self.assertEqual(w.meet_label.text(), "Starting…")
            w._meeting_pending = None  # (the fake never starts it)
        finally:
            vn.run_detached = orig
            vn.DRY_RUN = True
            self.state("idle", "idle")
            for h in helpers:
                h.wait()

    def test_05_daemon_off_banner(self):
        dead = subprocess.Popen(["true"])
        dead.wait()
        self.state("idle", "recording", pid=dead.pid)
        w = self.win
        self.assertTrue(w.daemon_banner.wrapper.isVisible())
        self.assertEqual(w.dict_label.text(), "Voxtype is off")
        self.assertFalse(w.act_dictate.isEnabled())
        self.assertFalse(w.act_meet_start.isEnabled())
        self.assertFalse(w.act_meet_stop.isEnabled())
        self.assertEqual(w.meet_label.text(), "No meeting")
        w.start_daemon_btn.click()  # dry run: logged only
        self.assertTrue(wait_until(lambda: w.start_daemon_btn.isEnabled()))
        self.state("idle", "idle")
        self.assertFalse(w.daemon_banner.wrapper.isVisible())

    # -- meetings
    def test_10_meeting_from_markdown_copy(self):
        w, p = self.win, self.win.meetings_page
        w.set_page("meetings")
        self.select(p, FIX["ids"]["A"])
        self.assertEqual(p.title.text(), "Weekly sync")
        self.assertIn("budget", p.view.toPlainText())
        self.assertIn("Markdown copy:", p.meta.text())
        self.assertIn("1:00:00", p.meta.text())
        self.assertFalse(p.act_refresh.isVisible())
        self.assertTrue(p.act_delete.isEnabled())
        p.act_copy.trigger()
        self.assertIn("budget", QApplication.clipboard().text())
        p.act_folder.trigger()  # dry run

    def test_17_speaker_pass_replaces_transcript(self):
        """While the speaker pass runs the meeting says so; when it replaces the Markdown
        copy, the new text is shown without selecting the meeting again."""
        w, p = self.win, self.win.meetings_page
        w.set_page("meetings")
        self.select(p, FIX["ids"]["B"])
        md = Path(p.meetings[FIX["ids"]["B"]].md_path)
        before = md.read_text()
        marker = WORK / "meeting-audio" / md.name[:15] / "speaker-pass"
        marker.parent.mkdir(parents=True)
        marker.touch()
        try:
            self.state()
            self.assertIn("Telling the speakers apart", p.meta.text())
            md.write_text("# Meeting B\n\n## Transcript\n\n### Person 1\n\n*[00:01]* The budget.\n")
            os.utime(md, (time.time() + 5, time.time() + 5))  # a new mtime even on coarse clocks
            shutil.rmtree(marker.parent)
            self.state()
            self.assertTrue(wait_until(lambda: "Person 1" in p.view.toPlainText()), p.view.toPlainText())
            self.assertNotIn("Telling the speakers apart", p.meta.text())
        finally:
            shutil.rmtree(marker.parent, ignore_errors=True)
            md.write_text(before)
            self.state()

    def test_18_name_speakers(self):
        w, p = self.win, self.win.meetings_page
        w.set_page("meetings")
        self.select(p, FIX["ids"]["B"])
        self.assertFalse(p.act_names.isEnabled())  # no speaker headings in this transcript
        md = Path(p.meetings[FIX["ids"]["B"]].md_path)
        before = md.read_text()
        side = vn.meeting_voices_path(str(md))
        asked = []
        p.ask_names = lambda speakers, has_fp, you_allowed: asked.append((speakers, has_fp, you_allowed)) or (
            {"Person 1": "Viktor"}, True)
        try:
            md.write_text(A_DataTests.SPEAKER_MD)
            side.parent.mkdir(parents=True)
            side.write_text(json.dumps({"model": "m.onnx", "people": {"Person 1": [1.0, 0.0]}}))
            self.state()  # (reload: the file changed)
            self.select(p, FIX["ids"]["B"])
            self.assertTrue(wait_until(lambda: p.act_names.isEnabled()))
            p.act_names.trigger()
            self.assertEqual(asked[0][0][0], ("Person 1", "Hello, Person 2 here is late."))
            self.assertTrue(asked[0][1])  # this meeting has fingerprints
            self.assertFalse(asked[0][2])  # an online meeting: You is your microphone
            self.assertIn("### Viktor", md.read_text())
            self.assertEqual(vn.remembered_voices(), {"Viktor": 1})
            self.assertTrue(vn.remember_voices_on())
            self.assertTrue(wait_until(lambda: "### Viktor" in p.current_text), p.current_text)
            # while a speaker pass runs it cannot be renamed
            marker = WORK / "meeting-audio" / md.name[:15] / "speaker-pass"
            marker.parent.mkdir(parents=True)
            marker.touch()
            self.state()
            self.assertFalse(p.act_names.isEnabled())
            shutil.rmtree(marker.parent)
            self.state()
            self.assertTrue(p.act_names.isEnabled())
        finally:
            md.write_text(before)
            shutil.rmtree(WORK / "voices", ignore_errors=True)
            vn.REMEMBER_FILE.unlink(missing_ok=True)
            del p.ask_names
            self.state()

    def test_19_name_speakers_dialog(self):
        vn.set_remember_voices(False)
        dlg = vn.NameSpeakersDialog(self.win, [("Person 1", "Hello."), ("Viktor", "Hi.")], False)
        try:
            self.assertEqual(dlg.fields["Person 1"].text(), "")      # a label: empty field
            self.assertEqual(dlg.fields["Viktor"].text(), "Viktor")  # a name: prefilled
            self.assertFalse(dlg.remember.isChecked())
            dlg.fields["Person 1"].setText("Person 7")
            dlg.accept()
            self.assertFalse(dlg.isHidden() and dlg.result() == vn.QDialog.DialogCode.Accepted)
            self.assertIn("Person 7", dlg.error.text())
            dlg.fields["Person 1"].setText("You")  # online: You is your microphone
            self.assertTrue(dlg.problems())
            dlg.you_allowed = True                  # in person: you can name yourself
            self.assertEqual(dlg.problems(), [])
            dlg.fields["Person 1"].setText(" Anna ")
            dlg.remember.setChecked(True)
            self.assertIn("next meeting", dlg.note.text())  # no fingerprints in this one
            self.assertEqual(dlg.names(), {"Person 1": "Anna"})
            self.assertEqual(dlg.problems(), [])
        finally:
            dlg.deleteLater()
            vn.REMEMBER_FILE.unlink(missing_ok=True)

    def test_11_meeting_via_export(self):
        p = self.win.meetings_page
        cid = FIX["ids"]["C"]
        self.select(p, cid)
        self.assertTrue(wait_until(lambda: "zeppelin" in p.view.toPlainText()), p.view.toPlainText())
        self.assertIn("no Markdown copy", p.meta.text())
        p.act_copy.trigger()
        self.assertIn(f"zeppelin-{cid[:8]}", QApplication.clipboard().text())
        exports = [c for c in voxtype_calls() if c[:2] == ["meeting", "export"] and c[-1] == cid]
        self.assertTrue(exports and "--timestamps" in exports[0] and "--speakers" in exports[0]
                        and "--metadata" in exports[0], exports)

    def test_12_export_to_files(self):
        p, cid, out = self.win.meetings_page, FIX["ids"]["C"], WORK / "out"
        txt = p.export_to(cid, str(out / "anna"), "Plain text (*.txt)")
        js = p.export_to(cid, str(out / "anna.json"), "Markdown (*.md)")  # extension wins
        md = p.export_to(cid, str(out / "anna.md"))
        self.assertEqual(txt, str(out / "anna.txt"))
        self.assertTrue(wait_until(lambda: all(Path(x).exists() for x in (txt, js, md))))
        self.assertTrue(Path(txt).read_text().startswith("Call with Anna"))
        self.assertEqual(json.loads(Path(js).read_text())["title"], "Call with Anna")
        self.assertTrue(Path(md).read_text().startswith("# Call with Anna"))
        self.assertTrue(wait_until(lambda: "Exported to" in self.win.statusBar().currentMessage()))
        p.export_to("00000000-0000-0000-0000-000000000000", str(out / "missing.md"))
        self.assertTrue(wait_until(lambda: self.errors))
        self.assertEqual(self.errors[0][0], "Export failed")

    def test_12b_export_uses_the_markdown_copy(self):
        """Not voxtype's own transcript, which is the one from before the speaker pass."""
        p, bid, out = self.win.meetings_page, FIX["ids"]["B"], WORK / "out"
        before = len(voxtype_calls())
        md, txt = p.export_to(bid, str(out / "b.md")), p.export_to(bid, str(out / "b.txt"))
        self.assertEqual(Path(md).read_text(), Path(p.meetings[bid].md_path).read_text())
        self.assertIn("[00:00:01] Remote:\nThe roadmap is fine.", Path(txt).read_text())
        self.assertEqual(json.loads(Path(p.export_to(bid, str(out / "b.json"))).read_text())
                         ["transcript"]["segments"][0]["text"], "The roadmap is fine.")
        self.assertEqual(voxtype_calls()[before:], [])
        self.assertIn("Exported to", self.win.statusBar().currentMessage())

    def test_13_search(self):
        w, mp, fp = self.win, self.win.meetings_page, self.win.files_page
        w.search.setText("budget")
        w.apply_search()
        self.assertEqual(mp.counts(), (1, 4))
        self.assertEqual(mp.current_key(), FIX["ids"]["A"])
        self.assertEqual(w.nav.item(0).text(), "Meetings (1/4)")
        self.assertEqual(fp.counts(), (0, 3))
        self.assertTrue(mp.view.extraSelections())  # highlighted
        # B has a Markdown copy; C's text only comes from voxtype export (fetched in the background)
        w.search.setText("roadmap")
        w.apply_search()
        self.assertEqual(mp.counts(), (1, 4))
        mp.export_cache.clear()
        word = f"zeppelin-{FIX['ids']['C'][:8]}"
        w.search.setText(word.upper())
        w.apply_search()
        self.assertTrue(wait_until(lambda: mp.counts() == (1, 4)), mp.counts())
        self.assertEqual(mp.current_key(), FIX["ids"]["C"])
        w.search.setText("kangaroo")
        w.apply_search()
        self.assertEqual(fp.counts(), (1, 3))
        self.assertEqual(fp.current().name, "talk.ogg")
        w.search.setText("no-such-word-anywhere")
        w.apply_search()
        self.assertEqual(mp.detail_stack.currentIndex(), 0)
        self.assertIn("Nothing matches", mp.placeholder.text())
        w.search.clear()
        w.apply_search()
        self.assertEqual(mp.counts(), (4, 4))

    def test_14_delete_meeting_and_markdown(self):
        p, aid = self.win.meetings_page, FIX["ids"]["A"]
        md = p.meetings[aid].md_path
        self.select(p, aid)
        p.ask_delete = lambda m: (True, True)
        p.act_delete.trigger()
        self.assertTrue(wait_until(lambda: aid not in p.meetings and not Path(md).exists()), self.errors)
        self.assertIn(["meeting", "delete", "--force", "--", aid], voxtype_calls())
        self.assertFalse(Path(md).exists())
        self.assertTrue((WORK / "data/Trash/files" / Path(md).name).exists())
        self.assertEqual(self.win.nav.item(0).text(), "Meetings (3)")
        self.assertFalse(self.errors)

    def test_15_live_meeting_cannot_be_deleted(self):
        p, did = self.win.meetings_page, FIX["ids"]["D"]
        self.state("idle", "recording")
        self.select(p, did)
        self.assertFalse(p.act_delete.isEnabled())
        self.assertTrue(p.act_refresh.isVisible())
        self.assertIn("shows up here as people talk", p.view.toPlainText())  # (no live transcript)
        self.state("idle", "idle")
        self.assertTrue(p.act_delete.isEnabled())

    def test_15b_live_transcript_follows_the_meeting(self):
        p, did = self.win.meetings_page, FIX["ids"]["D"]
        live = WORK / "meeting-audio" / "2026-10-12_1041" / "live.md"
        live.parent.mkdir(parents=True)
        (WORK / "run" / "meeting-toggle.started").write_text("2026-10-12_1041\n")
        lines = "".join(f"*[00:{i:02d}]* Line number {i} of the meeting.\n\n" for i in range(60))
        try:
            live.write_text("# Meeting\n\n## Transcript\n\n### You\n\n" + lines + "*Still talking …*\n")
            self.state("idle", "idle")
            self.select(p, FIX["ids"]["B"])
            self.state("idle", "recording")    # it starts: the window shows it
            self.assertEqual(p.current_key(), did)
            self.assertIn("Line number 59", p.view.toPlainText())
            self.assertIn("Still talking", p.view.toPlainText())
            bar = p.view.verticalScrollBar()
            self.assertTrue(wait_until(lambda: bar.maximum() > 0 and bar.value() == bar.maximum()))
            live.write_text(live.read_text() + "\n### Remote\n\n*[01:00]* A new zeppelin line.\n")
            self.state("idle", "recording")   # (the window polls every second)
            self.assertIn("zeppelin", p.view.toPlainText())
            self.assertTrue(wait_until(lambda: bar.value() == bar.maximum()))   # still at the end
            bar.setValue(0)                    # you scrolled up to read: it stays there
            live.write_text(live.read_text() + "\n*[01:05]* Another one.\n")
            self.state("idle", "recording")
            self.assertIn("Another one", p.view.toPlainText())
            wait_until(lambda: False, 0.2)
            self.assertEqual(bar.value(), 0)
            self.assertTrue(p.act_copy.isEnabled())
        finally:
            shutil.rmtree(live.parent.parent)
            (WORK / "run" / "meeting-toggle.started").unlink()
            self.state("idle", "idle")

    def test_16_db_change_reloads_list(self):
        p = self.win.meetings_page
        con = sqlite3.connect(WORK / "index.db")
        new_id = str(uuid.uuid4())
        con.execute("INSERT INTO meetings (id, title, started_at, status) VALUES (?, 'Fresh', ?, 'completed')",
                    (new_id, int(time.time())))
        con.commit()
        con.close()
        self.assertTrue(wait_until(lambda: new_id in p.meetings, 4))
        con = sqlite3.connect(WORK / "index.db")
        con.execute("DELETE FROM meetings WHERE id = ?", (new_id,))
        con.commit()
        con.close()
        self.assertTrue(wait_until(lambda: new_id not in p.meetings, 4))

    # -- transcribed files
    def test_20_files_details(self):
        w, p = self.win, self.win.files_page
        w.set_page("files")
        by_name = {e.name: k for k, e in p.entries.items()}
        self.select(p, by_name["talk.ogg"])
        self.assertIn("kangaroo", p.view.toPlainText())
        self.assertTrue(p.act_srt.isEnabled() and p.act_notes.isEnabled() and p.act_copy.isEnabled())
        self.assertIn("turbo on NPU", p.meta.text())
        p.act_copy.trigger()
        self.assertIn("kangaroo", QApplication.clipboard().text())
        p.act_srt.trigger()
        p.act_folder.trigger()
        self.select(p, by_name["broken.mp3"])
        self.assertIn("Transcription failed", p.view.toPlainText())
        self.assertFalse(p.act_srt.isEnabled())
        self.select(p, by_name["gone.ogg"])
        self.assertIn("moved or deleted", p.view.toPlainText())
        self.assertEqual(p.item_for(by_name["gone.ogg"]).text(4), "Files missing")

    def test_21_remove_entry_keeps_files(self):
        p = self.win.files_page
        key = next(k for k, e in p.entries.items() if e.name == "broken.mp3")
        self.select(p, key)
        p.ask_remove = lambda e, outs: (True, False)
        p.act_remove.trigger()
        self.assertNotIn(key, p.entries)
        self.assertNotIn(key, history_lines())
        self.assertTrue(Path(FIX["files"]["broken.mp3"]).exists())
        self.assertEqual(self.win.nav.item(1).text(), "Transcribed files (2)")

    def test_22_remove_entry_and_trash_transcripts(self):
        p = self.win.files_page
        key = next(k for k, e in p.entries.items() if e.name == "talk.ogg")
        self.select(p, key)
        seen = []
        p.ask_remove = lambda e, outs: (seen.extend(outs), (True, True))[1]
        p.act_remove.trigger()
        self.assertEqual(sorted(Path(x).name for x in seen), ["talk.md", "talk.srt"])
        self.assertNotIn(key, history_lines())
        self.assertFalse((FIX["media"] / "talk.md").exists())
        self.assertTrue((WORK / "data/Trash/files/talk.md").exists())
        self.assertTrue((WORK / "data/Trash/files/talk.srt").exists())
        self.assertTrue(Path(FIX["files"]["talk.ogg"]).exists())  # the recording itself is never trashed

    # -- requests (command line / second launch)
    def test_30_routing(self):
        w, ids = self.win, FIX["ids"]
        w.handle_request({"page": "meetings", "id": ids["B"], "files": []})
        self.assertEqual((w.page(), w.meetings_page.current_key()), ("meetings", ids["B"]))
        w.set_page("files")
        w.handle_request({"page": None, "id": None, "files": [w.meetings_page.meetings[ids["C"]].md_path or
                                                                w.meetings_page.meetings[ids["B"]].md_path]})
        self.assertEqual((w.page(), w.meetings_page.current_key()), ("meetings", ids["B"]))
        gone = next(k for k, e in w.files_page.entries.items() if e.name == "gone.ogg")
        w.handle_request({"page": None, "id": None, "files": [str(FIX["media"] / "gone.md")]})
        self.assertEqual((w.page(), w.files_page.current_key()), ("files", gone))
        w.handle_request({"page": None, "id": None, "files": [FIX["files"]["clip.ogg"]]})
        self.assertEqual(w.page(), "transcribe")
        self.assertEqual([(j.name, j.state) for j in w.transcribe_page.jobs], [("clip.ogg", "ready")])
        w.handle_request({"page": "transcribe", "id": None, "files": [FIX["files"]["clip.ogg"]]})
        self.assertEqual(len(w.transcribe_page.jobs), 1)  # no duplicate
        w.handle_request({"page": None, "id": None, "files": [str(FIX["media"] / "notes.txt")]})
        self.assertEqual(len(w.transcribe_page.jobs), 1)
        self.assertIn("Not a recording", w.statusBar().currentMessage())
        w.handle_request({"page": "meetings", "id": "no-such-id", "files": []})
        self.assertIn("not found", w.statusBar().currentMessage())
        w.handle_request({"page": "files", "id": None, "files": []})
        self.assertEqual(w.page(), "files")
        w.transcribe_page.queue.selectAll()
        w.transcribe_page.remove_selected()
        self.assertEqual(w.transcribe_page.jobs, [])

    # -- transcribe page (fake npu-transcribe)
    def test_31_transcribe_queue(self):
        w, tp = self.win, self.win.transcribe_page
        w.set_page("transcribe")
        tp.set_options("small", "de", True)
        self.assertEqual(tp.add_files([FIX["files"]["a.ogg"], FIX["files"]["b.ogg"], str(FIX["media"] / "notes.txt")]), 2)
        self.assertIn("not audio or video", w.statusBar().currentMessage())
        self.assertEqual(tp.start_btn.text(), "Transcribe 2 files")
        fractions, phases = set(), set()
        tp.start_ready()
        self.assertEqual(w.nav.item(2).text(), "Transcribe a file (2)")

        def watch():
            job = tp.running()
            if job:
                fractions.add(job.fraction)
                phases.add(job.phase)
                self.assertTrue(tp.run_frame.isVisible())
            return all(j.state == "done" for j in tp.jobs)
        self.assertTrue(wait_until(watch, 20), [(j.state, j.error, j.log) for j in tp.jobs])
        self.assertTrue({0.4, 0.8} & {round(f, 2) for f in fractions if f}, fractions)  # parsed progress
        a, b = tp.jobs
        self.assertEqual(a.args()[:7], ["--engine", "whisper", "--model", "small", "--language", "de", "--translate"])
        self.assertEqual(a.options_text(), "Whisper small · de → English")
        self.assertEqual([Path(o).name for o in b.outputs], ["b.srt", "b.md"])
        self.assertEqual(b.language_found, "")   # (given, so not detected; F_EngineTests detect one)
        self.assertEqual(b.ran, "whisper-small-int8-ov")   # (its done line)
        self.assertFalse(tp.run_frame.isVisible())
        # finished queue -> the result opens on the files page
        self.assertEqual(w.page(), "files")
        self.assertEqual(w.files_page.current().name, "b.ogg")
        self.assertIn("wombat", w.files_page.view.toPlainText())
        self.assertEqual(w.nav.item(2).text(), "Transcribe a file")
        w.set_page("transcribe")
        tp.queue.setCurrentItem(a.item)
        tp.show_result()
        self.assertEqual(w.files_page.current().name, "a.ogg")
        tp.log_toggle.setChecked(True)
        tp.queue.setCurrentItem(b.item)
        self.assertIn("model ready on NPU", tp.log_view.toPlainText())
        tp.clear_finished()
        self.assertEqual(tp.jobs, [])

    def test_32_transcribe_cancel(self):
        tp = self.win.transcribe_page
        os.environ["FAKE_TRANSCRIBE_DELAY"] = "3"
        try:
            tp.add_files([FIX["files"]["c.ogg"]])
            tp.start_ready()
            job = tp.jobs[-1]
            self.assertTrue(wait_until(lambda: job.phase.startswith("Preparing"), 5), job.log)
            tp.cancel_current()
            self.assertTrue(wait_until(lambda: job.state == "cancelled", 8), (job.state, job.log))
            self.assertIn("npu-transcribe: interrupted", job.log)
            self.assertFalse((FIX["media"] / "c.md").exists())
        finally:
            os.environ.pop("FAKE_TRANSCRIBE_DELAY")

    def test_33_transcribe_failure(self):
        tp = self.win.transcribe_page
        os.environ["FAKE_TRANSCRIBE_FAIL"] = "1"
        try:
            tp.add_files([FIX["files"]["d.ogg"]])
            tp.start_ready()
            job = tp.jobs[-1]
            self.assertTrue(wait_until(lambda: job.state == "failed", 10), job.log)
            self.assertEqual(job.error, "ffmpeg failed: Invalid data found when processing input")
            self.assertTrue(job.status_text().startswith("Failed: ffmpeg failed"))
            self.assertIn("ffmpeg failed", self.win.statusBar().currentMessage())
        finally:
            os.environ.pop("FAKE_TRANSCRIBE_FAIL")
        self.win.files_page.reload()
        e = next(e for e in self.win.files_page.entries.values() if e.name == "d.ogg")
        self.assertEqual(e.state(), "Failed")

    def test_34_missing_transcriber(self):
        orig = vn.TRANSCRIBE
        vn.TRANSCRIBE = str(WORK / "no-such-npu-transcribe")
        try:
            tp = self.win.transcribe_page
            tp.add_files([FIX["files"]["e.ogg"]])
            tp.start_ready()
            job = tp.jobs[-1]
            self.assertTrue(wait_until(lambda: job.state == "failed", 5))
            self.assertIn("could not start", job.error)
        finally:
            vn.TRANSCRIBE = orig
            tp.clear_finished()

    # -- settings
    def test_40_settings_persist(self):
        w = self.win
        w.set_page("files")
        w.transcribe_page.log_toggle.setChecked(True)
        w.save_settings()
        conf = WORK / "config/bmeet/bmeet.conf"
        self.assertTrue(conf.exists())
        self.assertIn("page=files", conf.read_text())
        w2 = vn.MainWindow()
        w2.handle_request({"page": None, "id": None, "files": []}, initial=True)
        self.assertEqual(w2.page(), "files")
        self.assertEqual(w2.transcribe_page.options()["engine"], "small")  # saved by start_ready
        self.assertTrue(w2.transcribe_page.log_toggle.isChecked())
        w2.force_quit = True
        w2.close()


# ============================================================================ single instance

class LogReader:
    """Collects a child's stderr lines in a thread."""

    def __init__(self, proc: subprocess.Popen) -> None:
        self.proc, self.lines = proc, []
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        for line in self.proc.stderr:
            self.lines.append(line.rstrip("\n"))

    def wait_for(self, text: str, timeout: float = 15) -> bool:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if any(text in x for x in self.lines):
                return True
            time.sleep(0.05)
        return False


class C_SingleInstanceTests(unittest.TestCase):
    env = dict(os.environ, BMEET_DEBUG="1", BMEET_SOCKET=str(WORK / "si.sock"))

    def launch(self, *args, cwd=None) -> tuple[subprocess.Popen, LogReader]:
        p = subprocess.Popen([sys.executable, "-I", str(APP_PATH), *args], env=self.env, cwd=cwd,
                             stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        return p, LogReader(p)

    def second(self, *args, cwd=None) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-I", str(APP_PATH), *args], env=self.env, cwd=cwd,
                              capture_output=True,
                              text=True, timeout=30)

    def stop(self, p: subprocess.Popen) -> int:
        if p.poll() is None:
            p.send_signal(signal.SIGTERM)
        code = p.wait(timeout=15)
        time.sleep(0.1)  # let the reader thread drain the pipe
        p.stderr.close()
        return code

    def test_forwarding(self):
        ids = FIX["ids"]
        p1, log1 = self.launch("files")
        try:
            self.assertTrue(log1.wait_for("bmeet: ready"), log1.lines)
            self.assertTrue(any('"page": "files"' in x for x in log1.lines))
            t0 = time.monotonic()
            r = self.second("meetings", "--id", ids["B"])
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("forwarded to the running window", r.stderr)
            self.assertLess(time.monotonic() - t0, 10)
            self.assertTrue(log1.wait_for(f'"page": "meetings", "id": "{ids["B"]}"'), log1.lines)
            # relative --file from another cwd arrives as an absolute path and is queued
            r = self.second("--file", "clip.ogg", cwd=FIX["media"])
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertTrue(log1.wait_for(json.dumps(FIX["files"]["clip.ogg"])), log1.lines)
            self.assertTrue(log1.wait_for("message: Added 1 file"), log1.lines)
            # bmeet-ctl open (as the panel widget calls it) reaches the same window; the
            # app runs with this test's Python (CI has PyQt6 only there, not in /usr/bin/python3)
            app = WORK / "bmeet-app"
            app.write_text(f'#!/bin/sh\nexec "{sys.executable}" -I "{APP_PATH}" "$@"\n')
            app.chmod(0o755)
            ctl = [sys.executable, "-I", str(CTL_PATH)] if sys.platform == "darwin" else [str(CTL_PATH)]
            r = subprocess.run(ctl + ["open", "transcribe", "--file", FIX["files"]["a.ogg"]],
                               env=dict(self.env, BMEET_APP=str(app)), capture_output=True,
                               text=True, timeout=10)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertTrue(log1.wait_for(f'"page": "transcribe", "id": null, "files": ["{FIX["files"]["a.ogg"]}"]'),
                            log1.lines)
            self.assertEqual(p1.poll(), None)  # still the one window
        finally:
            self.assertEqual(self.stop(p1), 0)
        self.assertFalse(any("Traceback" in x for x in log1.lines), log1.lines)

    def test_stale_socket_and_race(self):
        p1, log1 = self.launch()
        self.assertTrue(log1.wait_for("bmeet: ready"), log1.lines)
        p1.kill()  # crash: the socket file stays behind
        self.stop(p1)
        self.assertTrue(Path(self.env["BMEET_SOCKET"]).exists())
        p2, log2 = self.launch("meetings")
        try:
            self.assertTrue(log2.wait_for("bmeet: ready"), log2.lines)
        finally:
            self.assertEqual(self.stop(p2), 0)
        # two launches at the same time: exactly one window
        pa, loga = self.launch("files")
        pb, logb = self.launch("transcribe")
        try:
            end = time.monotonic() + 20
            while time.monotonic() < end and not (pa.poll() is not None or pb.poll() is not None):
                time.sleep(0.1)
            exited = [p for p in (pa, pb) if p.poll() is not None]
            self.assertEqual(len(exited), 1, (loga.lines, logb.lines))
            self.assertEqual(exited[0].returncode, 0)
            winner, winlog = (pb, logb) if exited[0] is pa else (pa, loga)
            self.assertTrue(winlog.wait_for("bmeet: ready"))
            loser_log = loga if exited[0] is pa else logb
            self.assertTrue(loser_log.wait_for("forwarded"), loser_log.lines)
        finally:
            for p in (pa, pb):
                self.stop(p)


# ============================================================================ real npu-transcribe

@unittest.skipUnless(REAL, "set REAL_TRANSCRIBE=1 (runs Whisper small on the NPU)")
class D_RealTranscribeTests(unittest.TestCase):
    KEYS = {"time", "input", "outputs", "model", "language", "device", "duration", "status"}

    def run_tool(self, *args, history: Path | None, timeout=900) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env.pop("NPU_TRANSCRIBE_HISTORY", None)
        if history is not None:
            env["NPU_TRANSCRIBE_HISTORY"] = str(history)
        return subprocess.run([NPU_PYTHON, "-I", str(NPU_TRANSCRIBE), *args], env=env,
                              capture_output=True, text=True,
                              timeout=timeout)

    def test_1_history_line_done(self):
        hist = WORK / "real" / "history.jsonl"
        r = self.run_tool("--model", "small", "--language", "en", "--", str(FIX["clip"]), history=hist)
        self.assertEqual(r.returncode, 0, r.stderr)
        outputs = r.stdout.split("\n")[:-1]
        self.assertEqual([Path(o).suffix for o in outputs], [".srt", ".md"])  # stdout unchanged: just paths
        self.assertRegex(r.stderr, r"\nnpu-transcribe: done: hello clip\.ogg: 00:00:0\d of audio in [\d.]+ s with \S+ on ")
        lines = history_lines(hist)
        self.assertEqual(len(lines), 1)
        h = json.loads(lines[0])
        self.assertEqual(set(h), self.KEYS)
        self.assertIsInstance(h["time"], int)
        self.assertLess(abs(h["time"] - time.time()), 120)
        self.assertEqual(h["input"], str(FIX["clip"]))
        self.assertEqual(h["outputs"], [str(Path(o).absolute()) for o in outputs])
        self.assertEqual((h["model"], h["language"], h["status"]), ("small", "en", "done"))
        self.assertIn(h["device"], ("NPU", "CPU"))
        self.assertIsInstance(h["duration"], float)
        self.assertTrue(5 < h["duration"] < 15, h["duration"])
        self.assertIn("quick brown fox", Path(outputs[1]).read_text().lower())
        print(f"\n    real run: device {h['device']}, {h['duration']} s of audio", file=sys.stderr)

    def test_2_history_line_failed_and_unwritable(self):
        hist = WORK / "real" / "history-fail.jsonl"
        r = self.run_tool("--model", "small", "--", str(WORK / "real" / "missing.ogg"),
                          str(FIX["media"] / "notes.txt"), history=hist)
        self.assertEqual(r.returncode, 1)
        hs = [json.loads(x) for x in history_lines(hist)]
        self.assertEqual([(Path(h["input"]).name, h["status"], h["outputs"], h["language"]) for h in hs],
                         [("missing.ogg", "failed", [], "?"), ("notes.txt", "failed", [], "?")])
        self.assertTrue(all(set(h) == self.KEYS and h["duration"] == 0.0 for h in hs))
        # a history that cannot be written must not break the transcription
        r = self.run_tool("--model", "small", "--language", "en", "--overwrite", "--format", "txt",
                          "--out", str(WORK / "real" / "out2"), "--", str(FIX["clip"]),
                          history=Path("/proc/version/history.jsonl"))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("WARNING: could not update /proc/version/history.jsonl", r.stderr)
        self.assertTrue((WORK / "real" / "out2" / "hello clip.txt").exists())

    def test_3_default_history_path(self):
        """Without the env var the line goes to ~/.local/share/npu-transcribe/history.jsonl
        (directory created on demand). Our test line is removed again afterwards."""
        default = Path.home() / ".local/share/npu-transcribe/history.jsonl"
        dir_existed, before = default.parent.exists(), default.read_text() if default.exists() else None
        try:
            r = self.run_tool("--model", "small", "--language", "en", "--format", "txt", "--overwrite",
                              "--out", str(WORK / "real" / "out3"), "--", str(FIX["clip"]), history=None)
            self.assertEqual(r.returncode, 0, r.stderr)
            h = json.loads(history_lines(default)[-1])
            self.assertEqual((h["input"], h["status"]), (str(FIX["clip"]), "done"))
        finally:
            if before is not None:
                vn.remove_history_entries({x for x in history_lines(default) if str(WORK) in x}, default)
            elif default.exists():
                default.unlink()
                if not dir_existed:
                    default.parent.rmdir()

    def test_4_transcribe_page_and_files_list(self):
        """The real tool through the GUI: progress parsed from its stderr, the history
        line written, and the Transcribed files page lists and shows the result."""
        orig = vn.TRANSCRIBE
        wrapper = WORK / "real" / "npu-transcribe"  # like the installer's ~/.local/bin wrapper
        wrapper.write_text(f'#!/bin/sh\nexec "{NPU_PYTHON}" -I "{NPU_TRANSCRIBE}" "$@"\n')
        wrapper.chmod(0o755)
        vn.TRANSCRIBE = str(wrapper)
        win = vn.MainWindow()
        win.error_box = lambda *a: None
        try:
            win.set_page("transcribe")
            tp = win.transcribe_page
            tp.set_options("small", "auto", False)
            tp.add_files([str(FIX["clip"])])
            tp.start_ready()
            job = tp.jobs[-1]
            phases = set()
            self.assertTrue(wait_until(lambda: (phases.add(job.phase), job.state not in ("queued", "running"))[1],
                                       900), job.log)
            self.assertEqual(job.state, "done", "\n".join(job.log))
            self.assertTrue(any(p.startswith(("Loading", "Preparing")) for p in phases), phases)
            progress = [vn.parse_transcribe_line(x) for x in job.log]
            self.assertIn(1.0, [p["fraction"] for p in progress if p["kind"] == "progress"], job.log)
            self.assertEqual(job.language_found, "en")
            self.assertEqual(len(job.outputs), 2)
            self.assertTrue(any(x.startswith("npu-transcribe: done:") for x in job.log))
            self.assertEqual(win.page(), "files")
            e = win.files_page.current()
            self.assertEqual((e.input, e.status, e.model), (str(FIX["clip"]), "done", "small"))
            self.assertEqual(sorted(e.outputs), sorted(job.outputs))
            self.assertIn("quick brown fox", win.files_page.view.toPlainText().lower())
            self.assertEqual(win.files_page.item_for(win.files_page.current_key()).text(4), "Done")
        finally:
            vn.TRANSCRIBE = orig
            win.force_quit = True
            win.close()


# ============================================================================ settings files, helpers

BMEET_CONFIG = WORK / "config/bmeet"
FEATURE_FILES = ("dictation", "meetings", "in-person-meetings", "file-transcription")


def set_config(name: str, value: str | None) -> None:
    """One of ~/.config/bmeet's files (here the work dir's); None removes it (its default)."""
    if value is None:
        (BMEET_CONFIG / name).unlink(missing_ok=True)
    else:
        (BMEET_CONFIG / name).write_text(value + "\n")


def reset_config() -> None:
    for name in FEATURE_FILES:
        set_config(name, None)
    set_config("languages", "en de")
    set_config("language", None)
    set_config("interface-language", "en")


def new_window() -> vn.MainWindow:
    w = vn.MainWindow()
    w.error_box = lambda *a: None
    w.handle_request({"page": None, "id": None, "files": []}, initial=True)
    w.show()
    app.processEvents()
    return w


def close_window(w: vn.MainWindow) -> None:
    w.force_quit = True
    w.close()
    w.deleteLater()
    app.processEvents()
    vn.texts.use("en")


class StandInAgent:
    """BMeet.app's control socket (agent.sock) in a runtime folder of the test's own: notes each
    request (one JSON line) and answers it with answer(request)."""

    def __init__(self, path: Path, answer) -> None:
        self.path, self.answer, self.requests = path, answer, []
        path.parent.mkdir(parents=True, exist_ok=True)
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(str(path))
        self.sock.listen(5)
        self.sock.settimeout(0.1)
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        while not self.stopped.is_set():
            try:
                conn, _ = self.sock.accept()
            except (TimeoutError, socket.timeout):
                continue
            except OSError:
                return
            with conn:
                conn.settimeout(5)
                line = conn.makefile("rb").readline()
                request = json.loads(line)
                self.requests.append(request)
                conn.sendall(json.dumps(self.answer(request)).encode() + b"\n")

    def close(self) -> None:
        self.stopped.set()
        self.thread.join(5)
        self.sock.close()
        self.path.unlink(missing_ok=True)


def load_bmeetmac():
    loader = importlib.machinery.SourceFileLoader("bmeetmac_test", str(REPO / "macos/bmeetmac.py"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


@contextlib.contextmanager
def runtime_root():
    """A runtime folder for bmeetmac (BMEET_RUNTIME_ROOT), short for the socket path limit, and
    the environment as it was afterwards: never the real one of BMeet.app in real use."""
    root = Path(tempfile.mkdtemp(prefix="bm-"))
    saved = {k: os.environ.get(k) for k in ("BMEET_RUNTIME_ROOT", "XDG_RUNTIME_DIR", "PATH")}
    os.environ["BMEET_RUNTIME_ROOT"] = str(root)
    try:
        yield root
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(root, ignore_errors=True)


def recorder_script(log: Path) -> Path:
    """A program that notes its arguments in `log` (a stand-in for the file manager)."""
    script = WORK / f"record-{log.stem}"
    script.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" >> {shlex.quote(str(log))}\n')
    script.chmod(0o755)
    return script


# ============================================================================ texts and the interface language

def tr_calls(path: Path) -> list[tuple[int, str, ast.expr | None, ast.expr | None]]:
    """(line, function, first text, second text) of each tr() / texts.tr() / N_() / ntr() call."""
    calls = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        name = f.id if isinstance(f, ast.Name) else (f"{f.value.id}.{f.attr}" if isinstance(f, ast.Attribute)
                                                     and isinstance(f.value, ast.Name) else None)
        if name in ("tr", "N_", "texts.tr"):
            calls.append((node.lineno, name, node.args[0] if node.args else None, None))
        elif name in ("ntr", "texts.ntr"):
            calls.append((node.lineno, name, node.args[1], node.args[2]))
    return calls


def literal(node: ast.expr | None) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def fields(text: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(text) if name is not None}


def latin_words(text: str) -> set[str]:
    """Words in Latin letters, without paths and file names (they name things, not text)."""
    words = set()
    for token in text.split():
        if "/" not in token and "~" not in token:
            words.update(w.strip(".-") for w in re.findall(r"[A-Za-z][A-Za-z0-9+.-]*", token))
    return words - {""}


# what may stay in Latin letters in the Russian window: names of products, models, keys, formats,
# and of transcript contents (You, Person 1), which stay English
LATIN_OK = {"BMeet", "Whisper", "GigaAM", "v3", "large-v3", "turbo", "small", "Markdown", "JSON", "NPU", "CPU",
            "Meta+H", "Meta+Alt+H", "Meta+Alt+M", "Meta+Alt+Shift+M", "Meta+Shift+H", "Ctrl+V", "Ctrl+F",
            "voxtype", "Voxtype", "npu-transcribe", "You", "Person", "Remote", "NAME.srt", "NAME.md", "srt", "md",
            "EN", "DE", "RU", "Dolphin", "Transcribe", "OK", "bmeet-ctl", "update",
            "whisper-small-int8-ov"}   # (the model in the test's voxtype config: a name)


class E_LanguageTests(unittest.TestCase):
    """The texts a person sees: one table (bmeettext) for the window and bmeet-meeting's
    notifications, the interface language from ~/.config/bmeet/interface-language, and a window
    that speaks Russian."""

    def setUp(self):
        reset_config()
        self.saved_env = {k: os.environ.get(k) for k in ("LC_ALL", "LC_MESSAGES", "LANG")}

    def tearDown(self):
        reset_config()
        for k, v in self.saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        vn.texts.use("en")

    def test_every_text_has_a_russian_line(self):
        """Each tr() text of the window and of bmeet-meeting has its Russian line, with the same
        {fields}, and each Russian line belongs to a text (on a Mac the window writes ⌃⌥, ⌘ and
        Finder: those versions have lines of their own). A text made at run time (an f-string)
        would have none: tr() takes literals, or values marked with N_()."""
        ru, needed, problems = vn.texts.RU, {}, []
        for path in (APP_PATH, MEETING_RUNNER):
            for line, name, first, second in tr_calls(path):
                text = literal(first)
                if isinstance(first, ast.Call) and getattr(first.func, "id", "") == "mac_words":
                    continue   # (the window's own tr() and ntr(), which hand their text on)
                if text is None:
                    if isinstance(first, (ast.JoinedStr, ast.BinOp)) or name != "tr" or path != APP_PATH:
                        problems.append(f"{path.name}:{line}: {name}() of a text made at run time")
                    continue   # (a value marked with N_ where it was written, checked there)
                plural = literal(second)
                variants = {text}
                if path == APP_PATH and name in ("tr", "N_", "ntr"):   # (texts.tr: already one platform's)
                    with mock.patch.object(vn, "MAC", True):
                        variants.add(vn.mac_words(text))
                for v in variants:
                    needed[v] = (f"{path.name}:{line}", plural)
        self.assertEqual(problems, [])
        self.assertEqual(sorted(t for t in needed if t not in ru), [], "texts without a Russian line")
        self.assertEqual(sorted(t for t in ru if t not in needed), [], "Russian lines without a text")
        for text, (where, plural) in needed.items():
            line = ru[text]
            if plural is None:
                self.assertIsInstance(line, str, where)
                self.assertEqual(fields(line), fields(text), where)
            else:   # (a number's three forms)
                self.assertIsInstance(line, tuple, where)
                self.assertEqual(len(line), 3, where)
                for form in line:
                    self.assertEqual(fields(form), fields(plural), where)
        for line in [x for v in ru.values() for x in (v if isinstance(v, tuple) else [v])] \
                + list(vn.texts.RU_LANGUAGES.values()):
            self.assertNotIn("ё", line.lower())   # (the glossary: no ё)

    def test_numbers_and_language_names(self):
        vn.texts.use("ru")
        self.assertEqual([vn.ntr(n, "{n} file", "{n} files") for n in (1, 2, 5, 11, 21, 22, 112)],
                         ["1 файл", "2 файла", "5 файлов", "11 файлов", "21 файл", "22 файла", "112 файлов"])
        self.assertEqual((vn.language_name("de"), vn.language_name("de", True), vn.language_name("xx")),
                         ("Немецкий", "немецкий", "XX"))
        vn.texts.use("en")
        self.assertEqual([vn.ntr(n, "{n} file", "{n} files") for n in (1, 2)], ["1 file", "2 files"])
        self.assertEqual((vn.language_name("de"), vn.language_name("de", True)), ("German", "German"))

    def test_interface_language_from_the_setting(self):
        """en and ru as set; system: the first of the system's languages that BMeet has, else
        English (Linux: LC_ALL, LC_MESSAGES, LANG; a Mac: its list of preferred languages)."""
        t = vn.texts
        for value, want in (("en", "en"), ("ru", "ru"), ("RU", "ru")):
            set_config("interface-language", value)
            self.assertEqual((t.setting(), t.interface_language()), (value.lower(), want))
        for value in ("system", "fr", None):   # (missing, or not one BMeet has: system)
            set_config("interface-language", value)
            self.assertEqual(t.setting(), "system")
        with mock.patch.object(t, "MAC", False):
            for env, want in (({"LC_ALL": "ru_RU.UTF-8", "LANG": "en_US.UTF-8"}, "ru"),
                              ({"LC_ALL": "", "LC_MESSAGES": "ru_RU.UTF-8", "LANG": "en_US.UTF-8"}, "ru"),
                              ({"LC_ALL": "", "LC_MESSAGES": "", "LANG": "ru_RU.UTF-8"}, "ru"),
                              ({"LC_ALL": "de_DE.UTF-8"}, "en"), ({"LC_ALL": "C.UTF-8"}, "en"),
                              ({"LC_ALL": "", "LC_MESSAGES": "", "LANG": ""}, "en")):
                os.environ.update(env)
                self.assertEqual(t.interface_language(), want, env)
        with mock.patch.object(t, "MAC", True):   # (a stand-in for the Mac's list)
            for languages, want in ((["de", "ru", "en"], "ru"), (["en", "ru"], "en"), (["fr", "de"], "en")):
                with mock.patch.object(t, "_mac_languages", lambda langs=languages: langs):
                    self.assertEqual(t.interface_language(), want, languages)
            os.environ.update(LC_ALL="ru_RU.UTF-8")   # (the list unreadable: the locale)
            with mock.patch.object(t, "_mac_languages", lambda: []):
                self.assertEqual(t.interface_language(), "ru")
        set_config("interface-language", "en")
        os.environ.update(LC_ALL="ru_RU.UTF-8")
        self.assertEqual(t.interface_language(), "en")   # (set: the system does not matter)
        if sys.platform == "darwin":   # the real list: codes, whatever this Mac's languages are
            self.assertTrue(all(re.fullmatch(r"[a-z]{2,3}", c) for c in t._mac_languages()), t._mac_languages())

    def test_the_window_in_russian(self):
        """interface-language ru: the window's pages, engine choices, languages and controls in
        Russian, and nothing English left in what it shows but names."""
        set_config("interface-language", "ru")
        set_config("languages", "en de ru")
        models = WORK / "models-ru"
        for f in ("model.xml", "model.bin", "tokens.txt", "model.mlmodelc"):
            (models / "gigaam-v3-ctc-ru").mkdir(parents=True, exist_ok=True)
            (models / "gigaam-v3-ctc-ru" / f).touch()
        with mock.patch.object(vn, "MODELS_DIR", models):
            w = new_window()
            try:
                self.assertEqual(w.ui_language, "ru")
                self.assertEqual([w.nav.item(i).text().split(" (")[0] for i in range(3)],
                                 ["Встречи", "Расшифрованные файлы", "Расшифровать файл"])
                tp = w.transcribe_page
                self.assertEqual([tp.engine.itemText(i) for i in range(tp.engine.count())],
                                 ["Автоматически", "GigaAM v3 (русский)", "Whisper large-v3 turbo", "Whisper small"])
                self.assertEqual([tp.language.itemText(i) for i in range(tp.language.count())],
                                 ["Определить автоматически", "Английский", "Немецкий", "Русский"])
                self.assertEqual((w.act_meet_start.text(), w.act_meet_room.text()), ("Начать встречу", "Очная встреча"))
                self.assertEqual(w.meetings_page.tree.headerItem().text(0), "Название")
                self.assertIn("Различаем", vn.tr("Telling the speakers apart… the transcript is replaced when that "
                                                 "is done."))
                shown = []
                data = {p.title for p in (w.meetings_page, w.files_page)} | {p.meta for p in (w.meetings_page,
                                                                                            w.files_page)}
                for widget in [w, *w.findChildren(vn.QWidget)]:
                    if widget in data:   # (a meeting's or a file's own title and details)
                        continue
                    shown.append(widget.toolTip())
                    for attr in ("text", "placeholderText", "windowTitle"):
                        if hasattr(widget, attr) and callable(getattr(widget, attr)):
                            with contextlib.suppress(TypeError):
                                shown.append(getattr(widget, attr)())
                for act in w.findChildren(vn.QAction):
                    shown += [act.text(), act.toolTip()]
                for combo in w.findChildren(vn.QComboBox):
                    shown += [combo.itemText(i) for i in range(combo.count())]
                for tree in w.findChildren(vn.QTreeWidget):
                    shown += [tree.headerItem().text(i) for i in range(tree.columnCount())]
                shown += [w.nav.item(i).text() for i in range(w.nav.count())]
                shown += [w.meetings_page.empty_text(), w.files_page.empty_text(), tp.hint.text()]
                words = set().union(*(latin_words(s) for s in shown if isinstance(s, str)))
                self.assertEqual(sorted(words - LATIN_OK), [], "English left in the Russian window")
            finally:
                close_window(w)

    def test_qt_translations(self):
        """Qt's own texts too (standard buttons, a Mac's application menu), where Qt has them."""
        before = vn.QApplication.translate("QPlatformTheme", "Cancel")
        vn.install_qt_translations(app, "en")
        self.assertEqual(vn.QApplication.translate("QPlatformTheme", "Cancel"), before)
        from PyQt6.QtCore import QLibraryInfo
        if not (Path(QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)) / "qtbase_ru.qm").exists():
            self.skipTest("this Qt has no Russian translations")
        vn.install_qt_translations(app, "ru")
        try:
            self.assertEqual(vn.QApplication.translate("QPlatformTheme", "Cancel"), "Отмена")
        finally:
            for t in app.findChildren(vn.QObject):
                if type(t).__name__ == "QTranslator":
                    app.removeTranslator(t)
                    t.deleteLater()
        self.assertEqual(vn.QApplication.translate("QPlatformTheme", "Cancel"), before)

    def test_language_changed_while_open(self):
        """Another interface language while the window runs: it says it switches when it opens
        again, with Restart (its texts are made with each widget, so not redone in place)."""
        w = new_window()
        b = w.language_banner
        try:
            self.assertTrue(b.wrapper.isHidden())
            set_config("interface-language", "ru")
            w.poll()
            self.assertFalse(b.wrapper.isHidden())
            self.assertEqual(b.text.text(), "This window switches to Russian when it opens again.")
            self.assertEqual([x.text() for x in b.findChildren(vn.QPushButton)], ["Restart", "Later"])
            self.assertEqual(w.act_meet_start.text(), "Start meeting")   # (still English until then)
            w.hide_language()                                             # Later: not again for ru
            w.poll()
            self.assertTrue(b.wrapper.isHidden())
            set_config("interface-language", "system")
            with mock.patch.dict(os.environ, {"LC_ALL": "en_US.UTF-8"}), mock.patch.object(vn.texts, "MAC", False):
                w.poll()
            self.assertTrue(b.wrapper.isHidden())                         # (system is English here: the same)
            detached = []
            with mock.patch.object(vn, "run_detached", detached.append):
                set_config("interface-language", "ru")
                w._language_hidden = None
                w.poll()
                b.findChildren(vn.QPushButton)[0].click()                 # Restart: this window, then the new one
                self.assertEqual(detached[-1][:2], ["/bin/sh", "-c"])
                self.assertIn('"$3" open "$4"', detached[-1][2])
                self.assertEqual(detached[-1][-2:], [str(vn.CTL_PATH), w.page()])
        finally:
            close_window(w)

    def test_meeting_notifications_in_russian(self):
        """bmeet-meeting's notifications come from the same table: one that needs nothing
        recorded (a start while another is starting) reaches a stand-in BMeet.app in Russian."""
        with runtime_root() as root:
            agent = StandInAgent(root / "bmeet/agent.sock", lambda req: {"ok": True})
            env = dict(os.environ, BMEET_RUNTIME_ROOT=str(root), XDG_CONFIG_HOME=str(WORK / "config"),
                       XDG_DATA_HOME=str(root / "data"), BMEET_MEETING_DB=str(root / "index.db"),
                       VOXTYPE_MEETING_DIR=str(root / "Meetings"))
            try:
                with open(root / "bmeet/meeting.lock", "w") as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX)          # (a start or stop is running)
                    for language, title in (("ru", "Встреча уже начинается или останавливается"),
                                            ("en", "Meeting start or stop already running")):
                        set_config("interface-language", language)
                        r = subprocess.run([sys.executable, "-I", str(MEETING_RUNNER), "start"], env=env,
                                           capture_output=True, text=True, timeout=60)
                        self.assertEqual(r.returncode, 0, r.stderr)
                        self.assertEqual(agent.requests[-1]["op"], "notify")
                        self.assertEqual(agent.requests[-1]["title"], title)
            finally:
                agent.close()
            self.assertFalse((root / "bmeet/meeting_state").exists())   # (nothing was started)


# ============================================================================ the engine choice

class F_EngineTests(unittest.TestCase):
    """Transcribe a file: Engine (Automatic, GigaAM v3, Whisper turbo, Whisper small), what
    each passes to npu-transcribe, GigaAM ↔ Russian ↔ Translate, the Options column, the
    options saved, and the languages offered (those that are on)."""
    win: vn.MainWindow

    @classmethod
    def setUpClass(cls):
        reset_config()
        set_config("languages", "en de ru")
        cls.models = WORK / "models"
        cls.patch = mock.patch.object(vn, "MODELS_DIR", cls.models)
        cls.patch.start()
        os.environ["NPU_MODELS_DIR"] = str(cls.models)   # (the fake npu-transcribe's GigaAM)
        cls.gigaam(True)
        cls.win = new_window()

    @classmethod
    def tearDownClass(cls):
        close_window(cls.win)
        cls.patch.stop()
        os.environ["NPU_MODELS_DIR"] = NPU_MODELS
        reset_config()

    @classmethod
    def gigaam(cls, installed: bool) -> None:
        """A stand-in for GigaAM v3's files (the ones of Linux and of a Mac), or none."""
        folder = cls.models / "gigaam-v3-ctc-ru"
        shutil.rmtree(folder, ignore_errors=True)
        if installed:
            (folder / "model.mlmodelc").mkdir(parents=True)
            for f in ("model.xml", "model.bin", "tokens.txt"):
                (folder / f).touch()

    def offers(self) -> tuple[list[str], list[str]]:
        tp = self.win.transcribe_page
        return ([tp.engine.itemData(i) for i in range(tp.engine.count())],
                [tp.language.itemData(i) for i in range(tp.language.count())])

    def choose(self, combo, value) -> None:
        combo.setCurrentIndex(combo.findData(value))   # (as a click: its reactions run)

    def setUp(self):
        set_config("languages", "en de ru")
        self.gigaam(True)
        self.win.poll()
        self.win.transcribe_page.set_options("auto", "auto", False)   # (not what earlier tests saved)

    def test_engines_offered(self):
        tp = self.win.transcribe_page
        self.assertEqual(self.offers(), (["auto", "gigaam", "turbo", "small"], ["auto", "en", "de", "ru"]))
        self.assertEqual([tp.engine.itemText(i) for i in range(4)],
                         ["Automatic", "GigaAM v3 (Russian)", "Whisper large-v3 turbo", "Whisper small"])
        self.assertIn("GigaAM v3 for Russian, with Whisper turbo redoing the sentences", tp.hint.text())
        self.gigaam(False)                       # (installed --no-gigaam): not offered
        self.win.poll()
        self.assertEqual(self.offers()[0], ["auto", "turbo", "small"])
        self.assertIn("GigaAM v3 for Russian is not installed", tp.hint.text())
        self.gigaam(True)
        set_config("languages", "en de")         # Russian off: not offered either
        self.win.poll()
        self.assertEqual(self.offers(), (["auto", "turbo", "small"], ["auto", "en", "de"]))
        set_config("languages", "en de ru")      # back while the window is open
        self.win.poll()
        self.assertEqual(self.offers()[0], ["auto", "gigaam", "turbo", "small"])

    def test_gigaam_russian_translate(self):
        tp = self.win.transcribe_page
        tp.set_options("auto", "de", True)
        self.choose(tp.engine, "gigaam")         # GigaAM: Russian, no translation
        self.assertEqual(tp.options(), {"engine": "gigaam", "language": "ru", "translate": False})
        self.assertIn("GigaAM v3 transcribes the Russian speech, and Whisper turbo redoes", tp.hint.text())
        tp.translate.setChecked(True)            # Translate: not GigaAM (small translates best)
        self.assertEqual(tp.options(), {"engine": "small", "language": "ru", "translate": True})
        self.assertIn("Translation uses the small model", tp.hint.text())
        tp.translate.setChecked(False)
        self.choose(tp.engine, "gigaam")
        self.choose(tp.language, "de")           # another language: not GigaAM
        self.assertEqual(tp.options(), {"engine": "auto", "language": "de", "translate": False})
        self.choose(tp.language, "auto")
        self.choose(tp.engine, "gigaam")
        self.assertEqual(tp.options()["language"], "ru")
        self.choose(tp.engine, "turbo")          # (Whisper keeps the language)
        self.assertEqual(tp.options(), {"engine": "turbo", "language": "ru", "translate": False})
        tp.set_options("gigaam", "en", True)     # (saved options that break the rules: GigaAM's win)
        self.assertEqual(tp.options(), {"engine": "gigaam", "language": "ru", "translate": False})
        tp.set_options("gigaam", "ru", False)
        set_config("languages", "en de")         # Russian turned off while GigaAM is chosen
        self.win.poll()
        self.assertEqual(tp.options()["engine"], "auto")
        self.assertEqual(tp.options()["language"], "auto")

    def test_what_each_choice_passes(self):
        J = vn.Job
        self.assertEqual(J("/x.ogg", engine="auto").args(),
                         ["--engine", "auto", "--model", "turbo", "--language", "auto", "--", "/x.ogg"])
        self.assertEqual(J("/x.ogg", engine="gigaam", language="ru").args()[:6],
                         ["--engine", "gigaam", "--model", "turbo", "--language", "ru"])
        self.assertEqual(J("/x.ogg", engine="turbo", language="de").args()[:6],
                         ["--engine", "whisper", "--model", "turbo", "--language", "de"])
        self.assertEqual(J("/x.ogg", engine="small", language="en", translate=True).args(),
                         ["--engine", "whisper", "--model", "small", "--language", "en", "--translate", "--", "/x.ogg"])
        # the Options column before it runs: what will run
        for kw, want in (({"engine": "gigaam", "language": "ru"}, "GigaAM v3 · ru"),
                         ({"engine": "auto", "language": "ru"}, "GigaAM v3 · ru"),
                         ({"engine": "auto", "language": "auto"}, "Automatic · auto"),
                         ({"engine": "auto", "language": "de"}, "Whisper turbo · de"),
                         ({"engine": "turbo", "language": "auto"}, "Whisper turbo · auto"),
                         ({"engine": "auto", "language": "ru", "translate": True}, "Whisper turbo · ru → English"),
                         ({"engine": "small", "language": "en"}, "Whisper small · en")):
            self.assertEqual(J("/x.ogg", state="queued", **kw).options_text(), want, kw)
        self.gigaam(False)
        self.assertEqual(J("/x.ogg", state="queued", engine="auto", language="ru").options_text(), "Whisper turbo · ru")
        self.assertEqual(J("/x.ogg").options_text(), "–")   # (ready: its options are taken at the start)

    def test_runs_and_shows_what_ran(self):
        """Through the queue (the fake npu-transcribe chooses as the real one): the Options
        column says what ran, the Files page names the model, the options are saved."""
        w, tp = self.win, self.win.transcribe_page
        w.set_page("transcribe")
        tp.clear_finished()
        tp.set_options("gigaam", "ru", False)
        tp.add_files([FIX["files"]["a.ogg"]])
        tp.start_ready()
        tp.set_options("auto", "auto", False)
        tp.add_files([FIX["files"]["b.ogg"]])
        tp.start_ready()
        a, b = tp.jobs
        self.assertEqual((a.item.text(1), b.item.text(1)), ("GigaAM v3 · ru", "Automatic · auto"))
        self.assertTrue(wait_until(lambda: all(j.state == "done" for j in tp.jobs), 20), [j.log for j in tp.jobs])
        self.assertEqual(a.args()[:2], ["--engine", "gigaam"])
        self.assertEqual((a.ran, a.item.text(1)), ("gigaam-v3-ctc", "GigaAM v3 · ru"))
        self.assertEqual((b.language_found, b.item.text(1)), ("en", "Whisper turbo · en"))
        w.files_page.reload()
        entry = next(k for k, e in w.files_page.entries.items() if e.name == "a.ogg" and e.model == "gigaam")
        self.assertEqual(w.files_page.item_for(entry).text(3), "GigaAM v3 · NPU")
        self.assertEqual(w.settings.value("transcribe/engine"), "auto")   # (the last start's)
        # Transcribe again: the file is queued, and takes the page's options when started
        self.assertTrue(w.files_page.select_key(entry))
        tp.set_options("gigaam", "ru", False)
        w.files_page.again()
        self.assertEqual(w.page(), "transcribe")
        self.assertEqual(tp.jobs[-1].state, "ready")
        tp.start_ready()
        self.assertEqual(tp.jobs[-1].args()[:6], ["--engine", "gigaam", "--model", "turbo", "--language", "ru"])
        self.assertTrue(wait_until(lambda: tp.jobs[-1].state == "done", 20))
        tp.clear_finished()

    def test_options_saved_and_restored(self):
        s = self.win.settings
        s.setValue("transcribe/engine", "gigaam")
        s.setValue("transcribe/language", "ru")
        s.setValue("transcribe/translate", False)
        w2 = vn.MainWindow()
        try:
            self.assertEqual(w2.transcribe_page.options(), {"engine": "gigaam", "language": "ru", "translate": False})
        finally:
            close_window(w2)
        # from before the engine choice: a Whisper size (turbo was Automatic: no --engine)
        s.remove("transcribe/engine")
        for model, engine in (("turbo", "auto"), ("small", "small"), (None, "auto")):
            if model:
                s.setValue("transcribe/model", model)
            else:
                s.remove("transcribe/model")
            s.setValue("transcribe/language", "de")
            w2 = vn.MainWindow()
            try:
                self.assertEqual(w2.transcribe_page.options()["engine"], engine, model)
            finally:
                close_window(w2)
        s.setValue("transcribe/engine", "gigaam")   # GigaAM saved, but not installed any more
        self.gigaam(False)
        w2 = vn.MainWindow()
        try:
            self.assertEqual(w2.transcribe_page.options(), {"engine": "auto", "language": "de", "translate": False})
        finally:
            close_window(w2)

    def test_languages_that_are_off(self):
        """Only the languages that are on, and Detect automatically: English too is one of them
        (no longer offered when it is off), on the page and on the toolbar's language button."""
        w, tp = self.win, self.win.transcribe_page
        tp.set_options("auto", "de", False)
        set_config("languages", "de ru")
        set_config("language", None)
        w.poll()
        self.assertEqual(self.offers()[1], ["auto", "de", "ru"])
        self.assertEqual(tp.options()["language"], "de")
        set_config("languages", "ru")            # the chosen one turned off: Detect automatically
        w.poll()
        self.assertEqual(self.offers()[1], ["auto", "ru"])
        self.assertEqual(tp.options()["language"], "auto")
        st = vn.ctl.status()                     # the toolbar's button cycles the languages that are on
        self.assertEqual((st["languages"], st["next_language"]), (["ru"], "ru"))   # (voxtype's says en)
        self.assertIn("switch to Russian", w.act_lang.toolTip())
        self.assertNotIn("German", w.act_lang.toolTip())


# ============================================================================ features, bmeet-ctl

class G_FeatureTests(unittest.TestCase):
    """~/.config/bmeet/dictation, meetings, in-person-meetings, file-transcription: a feature that
    is off disappears from the window while it is open, what it made stays listed; bmeet-ctl's
    status, its refusals, and `bmeet-ctl settings`."""
    win: vn.MainWindow

    @classmethod
    def setUpClass(cls):
        reset_config()
        cls.win = new_window()

    @classmethod
    def tearDownClass(cls):
        close_window(cls.win)
        reset_config()

    def setUp(self):
        reset_config()
        (WORK / "run/meeting_state").write_text("idle\n")
        (WORK / "run/state").write_text("idle\n")
        self.win.poll()

    def tearDown(self):
        reset_config()
        (WORK / "run/meeting_state").write_text("idle\n")
        (WORK / "run/state").write_text("idle\n")
        self.win.poll()

    def visible(self) -> dict[str, bool]:
        w = self.win
        acts = {"dictation state": w.act_dict_state, "dictate": w.act_dictate, "language": w.act_lang,
                "cancel": w.act_cancel, "start meeting": w.act_meet_start, "in person": w.act_meet_room,
                "meeting state": w.act_meet_state, "pause": w.act_meet_pause, "stop": w.act_meet_stop,
                "transcribe again": w.files_page.act_again}
        out = {name: act.isVisible() for name, act in acts.items()}
        out["transcribe page"] = not w.nav.item(2).isHidden()
        out["Ctrl+O"] = all(s.isEnabled() if isinstance(s, vn.QShortcut) else s.isVisible()
                            for s in w.transcribe_shortcuts)
        return out

    def test_each_feature_hides_its_controls(self):
        w = self.win
        everything = {"dictation state": True, "dictate": True, "language": True, "cancel": False,
                      "start meeting": True, "in person": True, "meeting state": True, "pause": True,
                      "stop": True, "transcribe again": True, "transcribe page": True, "Ctrl+O": True}
        self.assertEqual(self.visible(), everything)
        counts = (w.meetings_page.tree.topLevelItemCount(), w.files_page.tree.topLevelItemCount())
        for files, gone in (
                ({"dictation": "off"}, {"dictation state", "dictate", "language"}),
                ({"meetings": "off"}, {"start meeting"}),
                ({"in-person-meetings": "off"}, {"in person"}),
                ({"meetings": "off", "in-person-meetings": "off"},
                 {"start meeting", "in person", "meeting state", "pause", "stop"}),
                ({"file-transcription": "off"}, {"transcribe again", "transcribe page", "Ctrl+O"})):
            for name, value in files.items():
                set_config(name, value)
            w.poll()                                 # (open: it follows at once)
            self.assertEqual({k for k, v in self.visible().items() if not v} - {"cancel"}, gone, files)
            # the history stays
            self.assertEqual((w.meetings_page.tree.topLevelItemCount(), w.files_page.tree.topLevelItemCount()),
                             counts, files)
            reset_config()
            w.poll()                                 # turned on again: back
            self.assertEqual(self.visible(), everything, files)
        set_config("dictation", "off")               # (Cancel shows while one dictates, but not while off)
        (WORK / "run/state").write_text("recording\n")
        w.poll()
        self.assertFalse(w.act_cancel.isVisible())
        set_config("dictation", None)
        w.poll()
        self.assertTrue(w.act_cancel.isVisible())

    def test_a_running_meeting_can_still_be_stopped(self):
        set_config("meetings", "off")
        set_config("in-person-meetings", "off")
        (WORK / "run/meeting_state").write_text("recording\n")
        self.win.poll()
        v = self.visible()
        self.assertEqual((v["start meeting"], v["in person"], v["stop"], v["pause"]), (False, False, True, True))
        self.assertTrue(self.win.act_meet_stop.isEnabled())

    def test_file_transcription_off(self):
        """No Transcribe page, ⌘3/⌘O (Ctrl+O) or Transcribe again; a recording opened with BMeet is
        not queued; the Transcribed files page stays with its list, saying nothing that starts one."""
        w = self.win
        w.set_page("transcribe")
        set_config("file-transcription", "off")
        w.poll()
        self.assertEqual(w.page(), "files")          # (it was open: its history instead)
        w.set_page("transcribe")
        self.assertEqual(w.page(), "files")
        before = len(w.transcribe_page.jobs)
        w.handle_request({"page": None, "id": None, "files": [FIX["files"]["clip.ogg"]]})
        self.assertEqual(len(w.transcribe_page.jobs), before)
        self.assertIn("File transcription is turned off", w.statusBar().currentMessage())
        self.assertEqual(w.files_page.empty_text(), "No transcribed files yet.")
        w.open_files()                               # (⌘O: nothing; a dry run never shows a dialog anyway)
        self.assertEqual(w.page(), "files")
        set_config("file-transcription", None)
        w.poll()
        self.assertIn("Transcribe a file", w.files_page.empty_text())
        w.set_page("transcribe")
        self.assertEqual(w.page(), "transcribe")

    def test_meetings_page_text_follows(self):
        """An empty list says how to start a meeting, only the kinds that are on."""
        p = self.win.meetings_page
        with mock.patch.object(vn, "MEETING_DB", WORK / "no-meetings.db"):
            p.reload()                                   # (no meetings: the placeholder shows)
            try:
                self.assertEqual(p.detail_stack.currentIndex(), 0)
                self.assertIn("Press “Start meeting” above", p.placeholder.text())
                set_config("meetings", "off")
                self.win.poll()
                self.assertIn("Press “In person” above", p.placeholder.text())
                set_config("in-person-meetings", "off")
                self.win.poll()
                self.assertEqual(p.placeholder.text(), "No meetings yet.")
            finally:
                reset_config()
                self.win.poll()
        p.reload()
        self.assertEqual(p.tree.topLevelItemCount(), len(vn.load_meetings()))

    def test_mac_menus(self):
        """A Mac's menus: Settings… and About BMeet open BMeet.app's Settings window (bmeet-ctl
        settings), Transcribe Files… (⌘O) and Transcribe a File (⌘3) go with file transcription."""
        from PyQt6.QtWidgets import QMenu
        w2 = vn.MainWindow()
        detached = []
        try:
            vn.apply_mac_look(w2, w2.split.widget(0))
            menus = {a.text(): a for m in w2.menuBar().findChildren(QMenu) for a in m.actions() if a.text()}
            with mock.patch.object(vn, "run_detached", detached.append):
                menus["Settings…"].trigger()
                menus["About BMeet"].trigger()
            self.assertEqual([c[-2:] for c in detached], [[str(vn.CTL_PATH), "settings"], ["settings", "about"]])
            self.assertEqual(menus["Settings…"].shortcut().toString(), "Ctrl+,")
            set_config("file-transcription", "off")
            w2.poll()
            self.assertFalse(menus["Transcribe Files…"].isVisible() or menus["Transcribe a File"].isVisible())
            self.assertTrue(menus["Transcribed Files"].isVisible())
            set_config("dictation", "off")
            w2.poll()
            controls = next(m for m in w2.menuBar().findChildren(QMenu) if m.title() == "Controls")
            self.assertIn(w2.act_dictate, controls.actions())   # (the same action as the toolbar's: gone too)
            self.assertFalse(w2.act_dictate.isVisible())
        finally:
            close_window(w2)

    # -- bmeet-ctl
    OLD_KEYS = {"daemon", "dictation", "live_dictation", "meeting", "meeting_id", "meeting_title", "meeting_started",
                "meeting_live", "speakers_pending", "language", "next_language", "languages", "model", "device",
                "version", "update"}   # (the panel widget reads them)

    def test_ctl_status_keys(self):
        st = vn.ctl.status()
        self.assertEqual(set(st), self.OLD_KEYS | {"features", "interface_language"})
        self.assertEqual(st["features"], dict.fromkeys(vn.FEATURES, True))
        self.assertEqual(st["interface_language"], "en")
        set_config("meetings", "off")
        set_config("file-transcription", "OFF")
        set_config("dictation", "on")
        set_config("interface-language", "ru")
        st = vn.ctl.status()
        self.assertEqual(st["features"], {"dictation": True, "meetings": False, "in_person_meetings": True,
                                          "file_transcription": False})
        self.assertEqual(st["interface_language"], "ru")
        r = subprocess.run([sys.executable, "-I", str(CTL_PATH), "status"], capture_output=True, text=True, timeout=30)
        self.assertEqual(json.loads(r.stdout)["features"]["meetings"], False)   # (the widget's way)

    def test_ctl_live_dictation_follows_the_platform(self):
        set_config("live-dictation", None)
        try:
            self.assertEqual(vn.ctl.live_dictation(), "off")          # Linux: off without the file
            with mock.patch.object(vn.ctl, "MAC", True):
                self.assertEqual(vn.ctl.live_dictation(), "on")       # a Mac: on, as BMeet.app reads it
                set_config("live-dictation", "off")
                self.assertEqual(vn.ctl.live_dictation(), "off")
            set_config("live-dictation", "panel")
            self.assertEqual(vn.ctl.live_dictation(), "panel")
        finally:
            set_config("live-dictation", None)

    def test_ctl_mac_language_is_one_that_is_on(self):
        ctl = vn.ctl
        with mock.patch.object(ctl, "MAC", True):
            set_config("languages", "ru en")
            self.assertEqual(ctl.config()["openvino"]["language"], "ru")   # (none set: the first)
            set_config("language", "en")
            self.assertEqual(ctl.config()["openvino"]["language"], "en")
            set_config("language", "de")                                   # (turned off: the first)
            self.assertEqual(ctl.config()["openvino"]["language"], "ru")

    def test_ctl_refusals(self):
        """dictate, lang and starting a meeting refuse while their feature is off (status 3, why on
        stderr); stopping, pausing and resuming a meeting still work. BMeet.app is a stand-in."""
        ctl, sent = vn.ctl, []
        from types import SimpleNamespace as NS
        fake = NS(agent=lambda command, timeout=5.0: sent.append(command) or {"ok": True}, agent_running=lambda: True)

        def run(*args) -> tuple[int, str]:
            err = io.StringIO()
            with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
                code = ctl.main(list(args))
            return code, err.getvalue()
        with mock.patch.object(ctl, "MAC", True), mock.patch.object(ctl, "bmeetmac", fake, create=True):
            set_config("dictation", "off")
            for args in (["dictate"], ["dictate", "--clipboard"], ["lang", "de"], ["lang"]):
                code, err = run(*args)
                self.assertEqual(code, ctl.TURNED_OFF, args)
                self.assertIn("dictation is turned off", err)
            self.assertEqual(sent, [])
            self.assertFalse((BMEET_CONFIG / "language").exists())
            self.assertEqual(run("cancel")[0], 0)                       # (cancelling is never refused)
            set_config("dictation", None)
            self.assertEqual(run("dictate")[0], 0)
            self.assertEqual(sent[-1]["op"], "dictate")
            sent.clear()
            set_config("meetings", "off")
            code, err = run("meeting", "start")
            self.assertEqual((code, sent), (ctl.TURNED_OFF, []))
            self.assertIn("online meetings are turned off", err)
            self.assertEqual(run("meeting", "toggle")[0], ctl.TURNED_OFF)   # (it would start one)
            self.assertEqual(run("meeting", "start", "--in-person")[0], 0)  # (in person is on)
            self.assertEqual(sent[-1], {"op": "meeting", "action": "start", "in_person": True})
            set_config("in-person-meetings", "off")
            self.assertEqual(run("meeting", "toggle", "--in-person")[0], ctl.TURNED_OFF)
            (WORK / "run/meeting_state").write_text("recording\n")      # one runs: it can be stopped
            sent.clear()
            for args in (["meeting", "toggle"], ["meeting", "stop"], ["meeting", "pause"], ["meeting", "resume"]):
                self.assertEqual(run(*args)[0], 0, args)
            self.assertEqual([c["action"] for c in sent], ["toggle", "stop", "pause", "resume"])
            self.assertEqual(run("meeting", "start")[0], ctl.TURNED_OFF)
        # the same as a program on Linux (only what is refused: the rest would run voxtype)
        set_config("dictation", "off")
        for args in (["dictate"], ["lang", "de"], ["meeting", "start"]):
            r = subprocess.run([sys.executable, "-I", str(CTL_PATH), *args], capture_output=True, text=True, timeout=30)
            self.assertEqual(r.returncode, 3, (args, r.stderr))
            self.assertRegex(r.stderr, r"^bmeet-ctl: .* turned off in BMeet's settings \(~/\.config/bmeet/")

    def test_ctl_settings(self):
        """`bmeet-ctl settings [PAGE]`: the settings op to BMeet.app's control socket (a stand-in,
        in a runtime folder of the test's own); without an answer, or from a BMeet.app without
        that op, and on Linux: the settings folder in the file manager (a stand-in)."""
        ctl, log = vn.ctl, WORK / "file-manager.log"
        log.unlink(missing_ok=True)
        manager = recorder_script(log)
        saved = os.environ.get("BMEET_FILE_MANAGER")
        os.environ["BMEET_FILE_MANAGER"] = str(manager)
        try:
            with runtime_root() as root, mock.patch.object(ctl, "MAC", True), \
                    mock.patch.object(ctl, "bmeetmac", load_bmeetmac(), create=True):
                answers = {"settings": {"ok": True}}
                agent = StandInAgent(root / "bmeet/agent.sock",
                                     lambda req: answers.get(req.get("op"), {"ok": False, "error": "unknown op"}))
                try:
                    self.assertEqual(ctl.main(["settings", "features"]), 0)
                    self.assertEqual(ctl.main(["settings"]), 0)
                    self.assertEqual(agent.requests, [{"op": "settings", "page": "features"},
                                                      {"op": "settings", "page": "general"}])
                    self.assertFalse(log.exists())
                    answers.clear()                                  # a BMeet.app of before: unknown op
                    self.assertEqual(ctl.main(["settings", "about"]), 0)
                    self.assertEqual(log.read_text().splitlines(), [str(BMEET_CONFIG)])
                finally:
                    agent.close()
                self.assertEqual(ctl.main(["settings", "shortcuts"]), 0)  # not running at all
                self.assertEqual(log.read_text().splitlines(), [str(BMEET_CONFIG)] * 2)
                with contextlib.redirect_stderr(io.StringIO()) as err:
                    self.assertEqual(ctl.main(["settings", "bogus"]), 2)
                    self.assertEqual(ctl.main(["settings", "general", "about"]), 2)
                self.assertIn("usage: bmeet-ctl settings [general|features|languages|shortcuts|about]", err.getvalue())
            log.unlink()
            r = subprocess.run([sys.executable, "-I", str(CTL_PATH), "settings"], capture_output=True, text=True,
                               timeout=30)                           # Linux: the folder
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(log.read_text().splitlines(), [str(BMEET_CONFIG)])
        finally:
            if saved is None:
                os.environ.pop("BMEET_FILE_MANAGER", None)
            else:
                os.environ["BMEET_FILE_MANAGER"] = saved
            log.unlink(missing_ok=True)

    def test_z_mac_preferences_untouched(self):
        """No test wrote QSettings' native place on a Mac (a run before bmeet.conf once did)."""
        now = os.stat(MAC_PREFS).st_mtime_ns if MAC_PREFS.exists() else None
        self.assertEqual(now, MAC_PREFS_BEFORE)


if __name__ == "__main__":
    try:
        result = unittest.main(exit=False, verbosity=2 if "-v" in sys.argv else 1).result
    finally:
        if not os.environ.get("KEEP_WORK"):
            shutil.rmtree(WORK, ignore_errors=True)
    sys.exit(0 if result.wasSuccessful() else 1)
