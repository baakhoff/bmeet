#!/usr/bin/python3 -I
"""Offscreen tests for the BMeet app: data functions, every page of the window,
single-instance forwarding and (REAL_TRANSCRIBE=1) the npu-transcribe history change.

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

import importlib.machinery
import importlib.util
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import unittest
import uuid
from pathlib import Path

sys.dont_write_bytecode = True

HERE = Path(__file__).resolve().parent
APP_PATH = HERE.parent / "bmeet"
CTL_PATH = HERE.parent.parent / "bmeet-ctl"
REPO = HERE.parents[2]
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

    # -- structure
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
        self.assertEqual(a.args()[:5], ["--model", "small", "--language", "de", "--translate"])
        self.assertEqual(a.options_text(), "small · de → English")
        self.assertEqual([Path(o).name for o in b.outputs], ["b.srt", "b.md"])
        self.assertEqual(b.language_found, "en")
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
        self.assertEqual(w2.transcribe_page.options()["model"], "small")  # saved by start_ready
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


if __name__ == "__main__":
    try:
        result = unittest.main(exit=False, verbosity=2 if "-v" in sys.argv else 1).result
    finally:
        if not os.environ.get("KEEP_WORK"):
            shutil.rmtree(WORK, ignore_errors=True)
    sys.exit(0 if result.wasSuccessful() else 1)
