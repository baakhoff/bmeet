#!/usr/bin/env python3
"""Tests of BMeet's macOS parts, without models, permissions or BMeet.app (macOS only, but for
the meeting runner with a stand-in recorder, which runs on Linux too).

Run: ~/.local/share/bmeet/venv/bin/python -I macos/tests/test_macos.py -v
(numpy is needed; bmeet-record is used from macos/.build/release or the install, and its tests
skip without it: swift build -c release --package-path macos)

- npu-transcribe's Whisper on the Neural Engine (WhisperANE): the bmeet-whisper protocol, with a
  stand-in server on a socket of a private runtime folder.
- bmeet-record meeting: two files played instead of the devices (BMEET_RECORD_MIC_FILE /
  BMEET_RECORD_SYSTEM_FILE), following the state file through a pause: parts, start times,
  finished WAV headers, the pointer file.
- bmeet-meeting start / pause / resume / stop: with that recorder, a stand-in for npu-transcribe
  and no RAM disk: the units, the state, the index, the Markdown copy, the speaker pass, cleanup.
- bmeet-meeting with a stand-in recorder too: what goes wrong (a recorder that dies, a live
  transcript that fails, no access to the Meetings folder, a speaker pass ending while the next
  meeting stops), and its units.
- bmeetmac's runtime folder.
- bmeet-ctl's commands to BMeet.app, with a stand-in for its control socket.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import shutil
import socket
import sqlite3
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import wave
from pathlib import Path

HERE = Path(__file__).resolve().parent
MACOS = HERE.parent
REPO = MACOS.parent
PY = sys.executable


def load(path: Path, name: str):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    loader.exec_module(mod)
    return mod


def recorder() -> Path | None:
    for p in (MACOS / ".build/release/bmeet-record", Path.home() / ".local/share/bmeet/bin/bmeet-record"):
        if os.access(p, os.X_OK):
            return p
    return None


def tone_wav(path: Path, seconds: float, freq: float = 440.0, rate: int = 16000) -> None:
    import numpy as np
    t = np.arange(int(seconds * rate)) / rate
    data = (0.3 * np.sin(2 * np.pi * freq * t) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(data.tobytes())


def wav_info(path: Path) -> tuple[int, int, int]:
    """(RIFF size, data size, real data bytes) of a WAV file."""
    data = path.read_bytes()
    riff = struct.unpack("<I", data[4:8])[0]
    pos = data.index(b"data")
    size = struct.unpack("<I", data[pos + 4:pos + 8])[0]
    return riff, size, len(data) - pos - 8


class StandIn(threading.Thread):
    """A Unix socket server answering each JSON line with answer(request, payload)."""

    def __init__(self, path: Path, answer) -> None:
        super().__init__(daemon=True)
        self.path, self.answer, self.requests = path, answer, []
        path.parent.mkdir(parents=True, exist_ok=True)
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(str(path))
        self.sock.listen(8)

    def run(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn:
                f = conn.makefile("rb")
                req = json.loads(f.readline())
                payload = f.read(req.get("samples", 0) * 4) if req.get("samples") else b""
                self.requests.append(req)
                conn.sendall(json.dumps(self.answer(req, payload)).encode() + b"\n")

    def close(self) -> None:
        self.sock.close()


@unittest.skipUnless(sys.platform == "darwin", "macOS only")
class WhisperProtocol(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="bm-"))   # (short: sockets have a 104-byte path limit)
        os.environ["BMEET_RUNTIME_ROOT"] = str(self.tmp)
        self.nt = load(REPO / "transcribe/npu-transcribe", "npu_transcribe_mac_test")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)
        for k in ("BMEET_RUNTIME_ROOT", "XDG_RUNTIME_DIR"):
            os.environ.pop(k, None)

    def test_generate_shapes_like_whisper_pipeline(self) -> None:
        import numpy as np
        def answer(req, payload):
            if req["op"] == "load":
                return {"ok": True, "device": "ANE"}
            self.assertEqual(len(payload), 16000 * 4)
            return {"ok": True, "language": req.get("language") or "ru", "text": " Привет мир.", "device": "ANE",
                    "segments": [{"start": 0.1, "end": 0.9, "text": " Привет мир.",
                                  "words": [{"word": " Привет", "start": 0.1, "end": 0.5},
                                            {"word": " мир.", "start": 0.5, "end": 0.9}]}]}
        server = StandIn(self.tmp / "bmeet/whisper.sock", answer)
        server.start()
        try:
            w = self.nt.WhisperANE(self.nt.MODELS["turbo"], ["ANE", "CPU"], word_timestamps=True, priority=2)
            self.assertEqual(w.device, "ANE")
            r = w.generate(np.zeros(16000, np.float32), language="<|ru|>", task="transcribe",
                           return_timestamps=True, word_timestamps=True)
            self.assertEqual(r.texts, ["Привет мир."])
            self.assertEqual(r.language, "<|ru|>")
            self.assertEqual([(c.text, c.start_ts, c.end_ts) for c in r.chunks], [(" Привет мир.", 0.1, 0.9)])
            self.assertEqual([w_.word for w_ in r.words], [" Привет", " мир."])
            req = server.requests[-1]
            self.assertEqual((req["op"], req["model"], req["language"], req["words"], req["priority"], req["compute"]),
                             ("transcribe", "turbo", "ru", True, 2, "ane"))
            # the block-level code takes it as it takes OpenVINO's answer
            segs, dropped, heard = self.nt.whisper_block(w, 10.0, (np.ones(16000) * 0.1).astype(np.float32), {})
            self.assertEqual(heard, "ru")
            self.assertEqual(segs[0].words[0].start, 10.1)
        finally:
            server.close()

    def test_failed_block_is_skipped(self) -> None:
        import numpy as np
        server = StandIn(self.tmp / "bmeet/whisper.sock", lambda req, _: {"ok": True, "device": "ANE"} if req["op"] == "load"
                         else {"ok": False, "error": "decoder failed"})
        server.start()
        try:
            w = self.nt.WhisperANE(self.nt.MODELS["turbo"], ["ANE", "CPU"])
            self.assertIsNone(w.generate(np.zeros(1600, np.float32)))
            self.assertEqual(sum(r["op"] == "transcribe" for r in server.requests), 2)   # (one retry)
        finally:
            server.close()

    def test_devices(self) -> None:
        self.assertEqual(self.nt.pick_devices("NPU"), ["ANE", "CPU"])
        self.assertEqual(self.nt.pick_devices("CPU"), ["CPU"])


@unittest.skipUnless(sys.platform == "darwin" and recorder(), "macOS with bmeet-record built")
class Recorder(unittest.TestCase):
    def test_meeting_parts_follow_the_state(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="bm-"))
        try:
            tone_wav(tmp / "mic.wav", 20, 440)
            tone_wav(tmp / "remote.wav", 20, 660)
            state, pointer, folder = tmp / "state", tmp / "recording", tmp / "rec"
            state.write_text("recording\n")
            env = dict(os.environ, BMEET_RECORD_MIC_FILE=str(tmp / "mic.wav"),
                       BMEET_RECORD_SYSTEM_FILE=str(tmp / "remote.wav"))
            p = subprocess.Popen([str(recorder()), "meeting", str(folder), "--state", str(state), "--pointer", str(pointer)],
                                 env=env, stderr=subprocess.PIPE, text=True)
            time.sleep(2.0)
            self.assertEqual(pointer.read_text().strip(), str(folder))
            # while it records, the WAV is readable (open sizes) and growing
            riff, size, real = wav_info(folder / "part-1-mic.wav")
            self.assertGreater(real, 16000)
            state.write_text("paused\n")
            time.sleep(1.0)
            state.write_text("recording\n")
            time.sleep(1.5)
            state.write_text("idle\n")
            self.assertEqual(p.wait(timeout=10), 0, p.stderr.read())
            self.assertFalse(pointer.exists())
            for part, least in ((1, 1.5), (2, 1.0)):
                start = float((folder / f"part-{part}.start").read_text())
                self.assertLess(abs(time.time() - start), 30)
                for track in ("mic", "remote"):
                    riff, size, real = wav_info(folder / f"part-{part}-{track}.wav")
                    self.assertEqual(size, real)                 # the header was finished
                    self.assertEqual(riff, real + 36)
                    self.assertGreater(real / 32000, least, (part, track))
                    with wave.open(str(folder / f"part-{part}-{track}.wav")) as w:
                        self.assertEqual((w.getframerate(), w.getnchannels(), w.getsampwidth()), (16000, 1, 2))
            # both tracks of a part start together
            a, b = (wav_info(folder / f"part-1-{t}.wav")[2] for t in ("mic", "remote"))
            self.assertLess(abs(a - b) / 32000, 0.3)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_raw_microphone_to_stdout(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="bm-"))
        try:
            tone_wav(tmp / "mic.wav", 5)
            env = dict(os.environ, BMEET_RECORD_MIC_FILE=str(tmp / "mic.wav"), BMEET_RECORD_SPEED="10")
            p = subprocess.Popen([str(recorder()), "raw"], env=env, stdout=subprocess.PIPE)
            time.sleep(1.5)
            p.terminate()
            out = p.stdout.read()
            p.wait(timeout=5)
            self.assertGreater(len(out), 4 * 32000)   # ~5 s of 16 kHz 16-bit at 10x
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


FAKE_TRANSCRIBE = r'''
import json, os, sys, time
from pathlib import Path
args = sys.argv[1:]
if "--live" in args:
    folder = Path(args[args.index("--live") + 1])
    pointer = Path(os.environ["XDG_RUNTIME_DIR"]) / "bmeet/recording"
    seen = False
    while True:
        try:
            ours = Path(pointer.read_text().strip()) == folder
        except OSError:
            ours = False
        seen |= ours
        if seen and not ours:
            break
        time.sleep(0.2)
    (folder / "live.md").write_text("# Live\n\n## Meeting Info\n\n- **Speakers:** You\n\n"
                                    "## Transcript\n\n### You\n\nhello\n")
    if os.environ.get("FAKE_LIVE_FAIL"):   # (an error: no live.done)
        sys.exit(1)
    (folder / "live.done").touch()
elif "--meeting" in args:
    folder = Path(args[args.index("--meeting") + 1])
    out = Path(args[args.index("--output") + 1])
    assert (folder / "speaker-pass").exists()
    out.write_text("# Speakers\n\n### You\n\nhello\n\n### Person 1\n\nhi\n")
'''


@unittest.skipUnless(sys.platform == "darwin" and recorder(), "macOS with bmeet-record built")
class MeetingRunner(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="bm-"))
        tone_wav(self.tmp / "mic.wav", 30)
        (self.tmp / "fake-transcribe").write_text(FAKE_TRANSCRIBE)
        (self.tmp / "config/bmeet").mkdir(parents=True)
        (self.tmp / "config/bmeet/speakers").write_text("on\n")
        self.env = dict(os.environ, BMEET_RUNTIME_ROOT=str(self.tmp / "run"), XDG_RUNTIME_DIR=str(self.tmp / "run"),
                        XDG_CONFIG_HOME=str(self.tmp / "config"),
                        BMEET_MEETING_DB=str(self.tmp / "index.db"), VOXTYPE_MEETING_DIR=str(self.tmp / "Meetings"),
                        BMEET_NO_RAMDISK="1", BMEET_RECORD=str(recorder()), BMEET_WHISPER="/usr/bin/true",
                        BMEET_RECORD_MIC_FILE=str(self.tmp / "mic.wav"), BMEET_RECORD_SYSTEM_FILE=str(self.tmp / "mic.wav"),
                        BMEET_TRANSCRIBE=str(self.tmp / "fake-transcribe"))
        self.env.pop("BMEET_RUN_DIR", None)

    def tearDown(self) -> None:
        subprocess.run(self.cmd("cleanup"), env=self.env, capture_output=True)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def cmd(self, *args: str) -> list[str]:
        return [PY, "-I", str(MACOS / "bmeet-meeting"), *args]

    def run_cmd(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(self.cmd(*args), env=self.env, capture_output=True, text=True, timeout=120)

    def test_a_meeting(self) -> None:
        run = self.tmp / "run/bmeet"
        r = self.run_cmd("start")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((run / "meeting_state").read_text().strip(), "recording")
        name = (run / "meeting-toggle.started").read_text().strip()
        folder = run / "meeting-audio" / name
        self.assertTrue((folder / "meeting.json").exists())
        time.sleep(1.5)
        self.assertTrue((folder / "part-1-mic.wav").exists())
        self.assertEqual(self.run_cmd("pause").returncode, 0)
        self.assertEqual((run / "meeting_state").read_text().strip(), "paused")
        time.sleep(0.6)
        self.assertEqual(self.run_cmd("resume").returncode, 0)
        time.sleep(1.2)
        self.assertTrue((folder / "part-2-mic.wav").exists())
        r = self.run_cmd("stop")
        self.assertEqual(r.returncode, 0, r.stderr)
        md = Path(r.stdout.strip())
        self.assertTrue(md.name.endswith("-meeting.md"), r.stdout)
        self.assertEqual((run / "meeting_state").read_text().strip(), "idle")
        # the speaker pass replaces the live copy, then the recording is gone
        deadline = time.time() + 20
        while time.time() < deadline and (folder.exists() or "Speakers" not in md.read_text()):
            time.sleep(0.2)
        self.assertIn("Person 1", md.read_text())
        self.assertFalse(folder.exists())
        con = sqlite3.connect(self.tmp / "index.db")
        rows = con.execute("SELECT title, status, ended_at IS NOT NULL, md_path FROM meetings").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1:], ("completed", 1, str(md)))
        # export and delete, as the app calls them
        r = self.run_cmd("list")
        mid = json.loads(r.stdout)[0]["id"]
        r = self.run_cmd("export", "--format", "markdown", "--", mid)
        self.assertIn("Person 1", r.stdout)
        self.assertEqual(self.run_cmd("delete", "--force", "--", mid).returncode, 0)
        self.assertEqual(json.loads(self.run_cmd("list").stdout), [])
        self.assertTrue(md.exists())          # (the Markdown copy stays: the app trashes it if asked)

    def test_titles_stay_apart(self) -> None:
        for _ in range(2):
            self.assertEqual(self.run_cmd("start").returncode, 0)
            time.sleep(0.5)
            self.assertEqual(self.run_cmd("stop").returncode, 0)
        titles = [m["title"] for m in json.loads(self.run_cmd("list").stdout)]
        self.assertEqual(len(set(titles)), 2, titles)


FAKE_RECORD = r'''
# bmeet-record meeting DIR [--mic-only] [--state FILE] [--pointer FILE] without devices: a part per
# stretch of "recording". FAKE_RECORD_DIE_AFTER=SECONDS: it dies then (and leaves its pointer).
import os, signal, sys, time
from pathlib import Path
args = sys.argv[1:]
run = Path(os.environ["XDG_RUNTIME_DIR"]) / "bmeet"
folder = Path(args[1])
state = Path(args[args.index("--state") + 1]) if "--state" in args else run / "meeting_state"
pointer = Path(args[args.index("--pointer") + 1]) if "--pointer" in args else run / "recording"


def first(path):
    try:
        return path.read_text().split("\n")[0].strip()
    except OSError:
        return "idle"


folder.mkdir(parents=True, exist_ok=True)
pointer.write_text(f"{folder}\n")
stop = []
for sig in (signal.SIGINT, signal.SIGTERM):
    signal.signal(sig, lambda *_: stop.append(sig))
t0, part, recording = time.monotonic(), 0, False
die = float(os.environ.get("FAKE_RECORD_DIE_AFTER") or 0)
while not stop and first(state) in ("recording", "paused"):
    if first(state) == "recording" and not recording:
        part += 1
        (folder / f"part-{part}-mic.wav").write_bytes(bytes(44 + 32000))
    recording = first(state) == "recording"
    if die and time.monotonic() - t0 > die:
        os._exit(1)
    time.sleep(0.1)
if first(pointer) == str(folder):
    pointer.unlink()
'''


def alive(pid: int) -> bool:
    """Is `pid` running (a zombie is not)?"""
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    stat = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return bool(stat) and not stat.startswith("Z")


class MeetingStandIns(unittest.TestCase):
    """bmeet-meeting with a stand-in recorder and npu-transcribe, and no RAM disk (on Linux too)."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="bm-"))
        (self.tmp / "fake-transcribe").write_text(FAKE_TRANSCRIBE)
        rec = self.tmp / "fake-record"
        rec.write_text(f"#!{PY}\n" + FAKE_RECORD)
        rec.chmod(0o755)
        (self.tmp / "config/bmeet").mkdir(parents=True)
        (self.tmp / "config/bmeet/speakers").write_text("on\n")
        self.run_dir = self.tmp / "run/bmeet"
        # (XDG_RUNTIME_DIR too: on Linux bmeet-meeting would otherwise use the real one, BMeet's own)
        self.env = dict(os.environ, BMEET_RUNTIME_ROOT=str(self.tmp / "run"), XDG_RUNTIME_DIR=str(self.tmp / "run"),
                        XDG_CONFIG_HOME=str(self.tmp / "config"), XDG_DATA_HOME=str(self.tmp / "data"),
                        BMEET_MEETING_DB=str(self.tmp / "index.db"), VOXTYPE_MEETING_DIR=str(self.tmp / "Meetings"),
                        BMEET_NO_RAMDISK="1", BMEET_RECORD=str(rec), BMEET_WHISPER="/usr/bin/true",
                        BMEET_TRANSCRIBE=str(self.tmp / "fake-transcribe"))
        for k in ("BMEET_RUN_DIR", "FAKE_LIVE_FAIL", "FAKE_RECORD_DIE_AFTER"):
            self.env.pop(k, None)

    def tearDown(self) -> None:
        subprocess.run(self.cmd("cleanup"), env=self.env, capture_output=True, timeout=60)
        if (self.tmp / "locked").exists():
            (self.tmp / "locked").chmod(0o700)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def cmd(self, *args: str) -> list[str]:
        return [PY, "-I", str(MACOS / "bmeet-meeting"), *args]

    def run_cmd(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(self.cmd(*args), env=self.env, capture_output=True, text=True, timeout=120)

    def state(self) -> str:
        return (self.run_dir / "meeting_state").read_text().strip()

    def folder(self) -> Path:
        return self.run_dir / "meeting-audio" / (self.run_dir / "meeting-toggle.started").read_text().strip()

    def rows(self) -> list[tuple]:
        con = sqlite3.connect(self.tmp / "index.db")
        try:
            return con.execute("SELECT status, md_path FROM meetings ORDER BY started_at").fetchall()
        finally:
            con.close()

    def wait_for(self, check, timeout: float = 20.0) -> None:
        deadline = time.monotonic() + timeout
        while not check():
            if time.monotonic() > deadline:
                self.fail("timed out")
            time.sleep(0.1)

    def test_a_finished_pass_leaves_other_recordings(self) -> None:
        # a meeting being stopped: "idle" already, its own speaker pass not started yet
        folder = self.run_dir / "meeting-audio/2026-10-20_1147-2"
        folder.mkdir(parents=True)
        (folder / "part-1-mic.wav").write_bytes(bytes(44))
        (self.run_dir / "meeting-toggle.started").write_text(f"{folder.name}\n")
        (self.run_dir / "meeting_state").write_text("idle\n")
        # the meeting before it: its speaker pass has just ended
        r = self.run_cmd("wait-and-cleanup", "2026-10-20_1147")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue((folder / "part-1-mic.wav").exists())
        self.assertEqual(self.run_cmd("cleanup").returncode, 0)
        self.assertTrue((folder / "part-1-mic.wav").exists())
        (self.run_dir / "meeting-toggle.started").unlink()
        self.assertEqual(self.run_cmd("cleanup").returncode, 0)
        self.assertFalse(folder.exists())          # (nothing owns it now)

    def test_unfinished_live_transcript_is_kept(self) -> None:
        (self.tmp / "config/bmeet/speakers").write_text("off\n")
        self.env["FAKE_LIVE_FAIL"] = "1"
        self.assertEqual(self.run_cmd("start").returncode, 0)
        folder = self.folder()
        time.sleep(0.5)
        r = self.run_cmd("stop")
        self.assertEqual(r.returncode, 0, r.stderr)
        md = Path(r.stdout.strip())
        text = md.read_text()
        self.assertIn("hello", text)
        self.assertLess(text.index("- **Incomplete:**"), text.index("## Transcript"))
        self.assertFalse(folder.exists())
        self.assertEqual(self.rows(), [("completed", str(md))])

    @unittest.skipIf(os.geteuid() == 0, "root writes anywhere")
    def test_meetings_folder_not_writable(self) -> None:
        locked = self.tmp / "locked"
        locked.mkdir()
        locked.chmod(0o500)
        self.env["VOXTYPE_MEETING_DIR"] = str(locked / "Meetings")
        self.assertEqual(self.run_cmd("start").returncode, 0)
        folder = self.folder()
        time.sleep(0.5)
        r = self.run_cmd("stop")
        self.assertEqual(r.returncode, 0, r.stderr)
        md = Path(r.stdout.strip())
        self.assertEqual(md.parent, self.tmp)      # (next to the index)
        self.assertFalse((self.run_dir / "meeting-toggle.started").exists())
        self.wait_for(lambda: not folder.exists() and "Person 1" in md.read_text())
        self.assertEqual(self.rows(), [("completed", str(md))])

    def test_recorder_that_dies_stops_the_meeting(self) -> None:
        self.env["FAKE_RECORD_DIE_AFTER"] = "1"
        t0 = time.monotonic()
        self.assertEqual(self.run_cmd("start").returncode, 0)
        folder = self.folder()
        self.wait_for(lambda: self.state() == "idle", 40)
        self.assertLess(time.monotonic() - t0, 40)  # (--live did not wait 60 s for the recording to end)
        self.wait_for(lambda: not folder.exists())
        [(status, md)] = self.rows()
        self.assertEqual(status, "completed")
        self.assertIn("Person 1", Path(md).read_text())

    def test_units(self) -> None:
        saved = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(saved)))
        os.environ.update(self.env)
        bm = load(MACOS / "bmeet-meeting", "bmeet_meeting_test")
        pid_file = self.tmp / "child.pid"
        code = ("import os, signal, time; signal.signal(signal.SIGINT, signal.SIG_IGN); "
                f"open({str(pid_file)!r}, 'w').write(str(os.getpid())); time.sleep(60)")
        self.assertTrue(bm.unit_start("bmeet-test", [PY, "-c", code]))
        self.wait_for(lambda: pid_file.exists() and pid_file.read_text())
        child = int(pid_file.read_text())
        # its command ignores SIGINT: after the timeout, the whole unit is killed
        bm.unit_stop("bmeet-test", timeout=1)
        self.wait_for(lambda: not alive(child), 5)
        self.assertFalse((bm.UNITS / "bmeet-test.pid").exists())
        # a pid file left behind, its number now another process's: not the unit
        (bm.UNITS / "bmeet-test.pid").write_text(f"{os.getpid()}\n")
        self.assertFalse(bm.unit_active("bmeet-test"))


@unittest.skipUnless(sys.platform == "darwin", "macOS only")
class RuntimeFolder(unittest.TestCase):
    def test_the_temp_folder_whatever_the_shell_says(self) -> None:
        mac = load(MACOS / "bmeetmac.py", "bmeetmac_runtime_test")
        saved = dict(os.environ)
        try:
            os.environ.pop("BMEET_RUNTIME_ROOT", None)
            os.environ["XDG_RUNTIME_DIR"] = "/tmp/run-from-a-shell"
            self.assertEqual(mac.runtime(), Path(str(mac.temp_dir()).rstrip("/")) / "bmeet")
            os.environ["BMEET_RUNTIME_ROOT"] = "/tmp/bm-tests"
            self.assertEqual(mac.runtime(), Path("/tmp/bm-tests/bmeet"))
        finally:
            os.environ.clear()
            os.environ.update(saved)


@unittest.skipUnless(sys.platform == "darwin", "macOS only")
class CtlToApp(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="bm-"))
        os.environ.update(BMEET_RUNTIME_ROOT=str(self.tmp), XDG_CONFIG_HOME=str(self.tmp / "config"))
        os.environ.pop("BMEET_PLATFORM", None)
        (self.tmp / "config/bmeet").mkdir(parents=True)
        (self.tmp / "config/bmeet/languages").write_text("en ru\n")
        self.server = StandIn(self.tmp / "bmeet/agent.sock", lambda req, _: {"ok": True})
        self.server.start()
        self.ctl = load(REPO / "bmeet/bmeet-ctl", "bmeet_ctl_mac_test")

    def tearDown(self) -> None:
        self.server.close()
        shutil.rmtree(self.tmp, ignore_errors=True)
        for k in ("BMEET_RUNTIME_ROOT", "XDG_RUNTIME_DIR", "XDG_CONFIG_HOME"):
            os.environ.pop(k, None)

    def test_commands(self) -> None:
        self.assertEqual(self.ctl.main(["dictate", "--clipboard"]), 0)
        self.assertEqual(self.ctl.main(["meeting", "start", "--in-person"]), 0)
        self.assertEqual(self.ctl.main(["meeting", "pause"]), 0)
        self.assertEqual(self.ctl.main(["meeting", "pause", "--in-person"]), 2)
        self.assertEqual(self.ctl.main(["lang", "toggle"]), 0)
        self.assertEqual((self.tmp / "config/bmeet/language").read_text().strip(), "ru")
        self.assertEqual(self.ctl.main(["lang", "toggle"]), 0)
        self.assertEqual((self.tmp / "config/bmeet/language").read_text().strip(), "en")
        ops = [(r["op"], r.get("action"), r.get("clipboard"), r.get("in_person"), r.get("language"))
               for r in self.server.requests]
        self.assertEqual(ops, [("dictate", None, True, None, None), ("meeting", "start", None, True, None),
                               ("meeting", "pause", None, False, None), ("language", None, None, None, "ru"),
                               ("language", None, None, None, "en")])

    def test_status_reads_the_app(self) -> None:
        run = self.tmp / "bmeet"
        (run / "pid").write_text(f"{os.getpid()}\n")
        (run / "state").write_text("recording\n")
        (run / "meeting_state").write_text("idle\n")
        st = self.ctl.status()
        self.assertEqual((st["daemon"], st["dictation"], st["meeting"], st["device"]), ("active", "recording", "idle", "ANE"))
        self.assertEqual(st["languages"], ["en", "ru"])


if __name__ == "__main__":
    unittest.main()
