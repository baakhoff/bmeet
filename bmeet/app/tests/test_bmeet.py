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

    def test_ctl_speakers_pending(self):
        audio = WORK / "meeting-audio"
        self.assertEqual(vn.ctl.status()["speakers_pending"], [])   # no folder at all
        (audio / "2026-10-12_1041").mkdir(parents=True)               # still recording
        (audio / "2026-10-12_0900").mkdir()
        (audio / "2026-10-12_0900" / "speaker-pass").touch()          # pass running
        try:
            self.assertEqual(vn.ctl.status()["speakers_pending"], ["2026-10-12_0900"])
        finally:
            shutil.rmtree(audio)

    def test_export_cmd(self):
        self.assertEqual(vn.export_cmd("ID", "json", "/o.json")[-5:], ["--metadata", "--output", "/o.json", "--", "ID"])
        cmd = vn.export_cmd("ID")
        self.assertEqual(cmd[1:5], ["meeting", "export", "--format", "markdown"])
        self.assertEqual(cmd[-2:], ["--", "ID"])
        self.assertIn("--timestamps", cmd)


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
            (WORK / "run" / "meeting_state").write_text("idle\n")
            self.assertTrue(wait_until(lambda: (w.poll(), w.meet_label.text())[1] == "No meeting", 5))
            self.assertEqual(w.statusBar().currentMessage(), "Meeting saved")
            self.assertEqual(w.meetings_page.current_key(), FIX["ids"]["D"])  # newest meeting selected
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
        p.ask_names = lambda speakers, has_fp: asked.append((speakers, has_fp)) or (
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
        self.assertTrue(wait_until(lambda: "zeppelin" in p.view.toPlainText()))
        self.state("idle", "idle")
        self.assertTrue(p.act_delete.isEnabled())

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
            r = subprocess.run([str(CTL_PATH), "open", "transcribe", "--file", FIX["files"]["a.ogg"]],
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
        self.assertRegex(r.stderr, r"\nnpu-transcribe: done: hello clip\.ogg: 00:00:0\d of audio in [\d.]+ s on ")
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
