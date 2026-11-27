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
  finished WAV headers, the pointer file; a device clock that runs fast; its runtime folder.
- bmeet-meeting start / pause / resume / stop: with that recorder, a stand-in for npu-transcribe
  and no RAM disk: the units, the state, the index, the Markdown copy, the speaker pass, cleanup.
- bmeet-meeting with a stand-in recorder too: what goes wrong (a recorder that dies, a live
  transcript that fails, no access to the Meetings folder, a speaker pass ending while the next
  meeting stops), and its units.
- bmeetmac's runtime folder.
- bmeet-whisper itself (built, no models): its runtime folder, and it removes only its own socket
  and pid file when it ends.
- bmeet-ctl's commands to BMeet.app, with a stand-in for its control socket; its update check
  (BMeet for macOS's own releases) and the Update button's Terminal window.
- fetch-models (any system): a download that is cut off goes on from its .part, with a stand-in
  for curl.
- prepare-coreml (any system with numpy): a compute plan that macOS cannot make (14.2, 14.3) is
  only reported.
"""
from __future__ import annotations

import contextlib
import hashlib
import importlib.machinery
import importlib.util
import io
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


def set_state(path: Path, state: str) -> None:
    """The meeting state, written as bmeet-meeting writes it: a new file put in place. (Written
    over in place, the recorder can read it empty between the two steps: "idle", it ends.)"""
    tmp = path.with_name(path.name + ".new")
    tmp.write_text(state + "\n")
    tmp.replace(path)


def wait_until(check, timeout: float = 10.0) -> bool:
    """check() turns true within timeout (a file it reads that is not there yet: not yet)."""
    end = time.monotonic() + timeout
    while True:
        try:
            if check():
                return True
        except (OSError, ValueError, struct.error):
            pass
        if time.monotonic() > end:
            return False
        time.sleep(0.05)


def wav_ended(path: Path) -> bool:
    """The recorder finished the WAV's header (while it records, the sizes are open)."""
    riff, size, real = wav_info(path)
    return size == real


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
            folder.mkdir()   # (bmeet-meeting makes it)
            env = dict(os.environ, BMEET_RECORD_MIC_FILE=str(tmp / "mic.wav"),
                       BMEET_RECORD_SYSTEM_FILE=str(tmp / "remote.wav"))
            p = subprocess.Popen([str(recorder()), "meeting", str(folder), "--state", str(state), "--pointer", str(pointer)],
                                 env=env, stderr=subprocess.PIPE, text=True)
            time.sleep(2.0)
            # (CI's Macs can be slow: each step waits for the recorder, not a set time)
            self.assertTrue(wait_until(pointer.exists), "no pointer file")
            self.assertEqual(pointer.read_text().strip(), str(folder))
            # while it records, the WAV is readable (open sizes) and growing
            self.assertTrue(wait_until(lambda: wav_info(folder / "part-1-mic.wav")[2] > 16000), "part 1 not growing")
            self.assertFalse(wav_ended(folder / "part-1-mic.wav"))
            set_state(state, "paused")
            self.assertTrue(wait_until(lambda: wav_ended(folder / "part-1-remote.wav")), "part 1 did not end")
            time.sleep(0.5)
            set_state(state, "recording")
            self.assertTrue(wait_until((folder / "part-2.start").exists), "part 2 did not start")
            time.sleep(1.5)
            set_state(state, "idle")
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

    def test_a_fast_device_clock_does_not_drift_ahead(self) -> None:
        # The system audio from a device whose clock runs 25% fast (more samples than their host
        # times allow): its track is cut back to its time, so it stays as long as the microphone's.
        tmp = Path(tempfile.mkdtemp(prefix="bm-"))
        try:
            tone_wav(tmp / "mic.wav", 20, 440)
            tone_wav(tmp / "remote.wav", 20, 660)
            state, folder = tmp / "state", tmp / "rec"
            state.write_text("recording\n")
            folder.mkdir()
            env = dict(os.environ, BMEET_RECORD_MIC_FILE=str(tmp / "mic.wav"),
                       BMEET_RECORD_SYSTEM_FILE=str(tmp / "remote.wav"), BMEET_RECORD_SYSTEM_CLOCK="1.25")
            p = subprocess.Popen([str(recorder()), "meeting", str(folder), "--state", str(state),
                                  "--pointer", str(tmp / "recording")], env=env, stderr=subprocess.PIPE, text=True)
            time.sleep(4.0)
            set_state(state, "idle")
            self.assertEqual(p.wait(timeout=10), 0, p.stderr.read())
            mic, remote = (wav_info(folder / f"part-1-{t}.wav")[2] / 32000 for t in ("mic", "remote"))
            self.assertGreater(mic, 3)
            self.assertLess(abs(remote - mic), 0.3, (mic, remote))      # (not cut: 1 s longer)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_the_runtime_folder_of_bmeetmac(self) -> None:
        # Without --state and --pointer: BMeet's runtime folder as bmeetmac sees it
        # ($BMEET_RUNTIME_ROOT/bmeet here), not the XDG_RUNTIME_DIR a shell exports.
        tmp = Path(tempfile.mkdtemp(prefix="bm-"))
        p = None
        try:
            tone_wav(tmp / "mic.wav", 5)
            run = tmp / "run/bmeet"
            run.mkdir(parents=True)
            (run / "meeting_state").write_text("recording\n")
            (tmp / "rec").mkdir()
            env = {k: v for k, v in os.environ.items() if k not in ("BMEET_STATE_FILE", "BMEET_RECORDING_FILE")}
            env.update(BMEET_RUNTIME_ROOT=str(tmp / "run"), XDG_RUNTIME_DIR=str(tmp / "shell"),
                       BMEET_RECORD_MIC_FILE=str(tmp / "mic.wav"))
            p = subprocess.Popen([str(recorder()), "meeting", str(tmp / "rec"), "--mic-only"], env=env,
                                 stderr=subprocess.PIPE, text=True)
            deadline = time.time() + 5
            while not (run / "recording").exists() and time.time() < deadline:
                time.sleep(0.1)
            self.assertEqual((run / "recording").read_text().strip(), str(tmp / "rec"))
            (run / "meeting_state").write_text("idle\n")
            self.assertEqual(p.wait(timeout=10), 0, p.stderr.read())
            self.assertFalse((run / "recording").exists())
            self.assertFalse((tmp / "shell").exists())
        finally:
            if p and p.poll() is None:
                p.kill()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_a_missing_folder_is_not_made(self) -> None:
        # bmeet-meeting makes the folder on its RAM disk: one that is gone (the RAM disk was
        # detached) is not made again, on the Mac's disk
        tmp = Path(tempfile.mkdtemp(prefix="bm-"))
        try:
            tone_wav(tmp / "mic.wav", 5)
            state, pointer, folder = tmp / "state", tmp / "recording", tmp / "ram/rec"
            state.write_text("recording\n")
            env = dict(os.environ, BMEET_RECORD_MIC_FILE=str(tmp / "mic.wav"))
            r = subprocess.run([str(recorder()), "meeting", str(folder), "--mic-only", "--state", str(state),
                                "--pointer", str(pointer)], env=env, capture_output=True, text=True, timeout=20)
            self.assertEqual(r.returncode, 1, r.stderr)
            self.assertIn("is missing", r.stderr)
            self.assertFalse((tmp / "ram").exists())
            self.assertFalse(pointer.exists())
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
import json, os, signal, sys, time
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
    if os.environ.get("FAKE_LIVE_NONE"):   # (nothing was said)
        (folder / "live.done").touch()
        sys.exit(0)
    (folder / "live.md").write_text("# Live\n\n## Meeting Info\n\n- **Speakers:** You\n"
                                    "- **Transcribed:** as people talked (live), fake\n\n"
                                    "## Transcript\n\n### You\n\nhello\n")
    if os.environ.get("FAKE_LIVE_FAIL"):   # (an error: no live.done)
        sys.exit(1)
    (folder / "live.done").touch()
elif "--meeting" in args:
    folder = Path(args[args.index("--meeting") + 1])
    out = Path(args[args.index("--output") + 1])
    assert (folder / "speaker-pass").exists()
    if os.environ.get("FAKE_PASS_KILLED"):   # (ended without a word, as a crash ends it)
        os.kill(os.getpid(), signal.SIGKILL)
    if os.environ.get("FAKE_PASS_FAIL"):     # (an error npu-transcribe reports itself)
        sys.exit(1)
    out.write_text("# Speakers\n\n### You\n\nhello\n\n### Person 1\n\nhi\n")
'''


def mac_runtime_root() -> str | None:
    """The temp folder whose bmeet/ holds the running BMeet.app's sockets (getconf DARWIN_USER_TEMP_DIR)."""
    try:
        return os.confstr(65537).rstrip("/") or None   # (_CS_DARWIN_USER_TEMP_DIR)
    except (ValueError, OSError):
        return None


def sealed(env: dict, notes: Path) -> dict:
    """`env` for what a meeting test starts, kept off this Mac's BMeet: a runtime folder of the
    test's own (checked: not the real one), and notifications into the file `notes`
    (BMEET_NOTIFY_LOG: bmeetmac.notify then neither asks BMeet.app nor runs osascript, which would
    put them on the screen)."""
    root, real = env.get("BMEET_RUNTIME_ROOT", ""), mac_runtime_root()
    if not root or (real and os.path.realpath(root) == os.path.realpath(real)):
        raise RuntimeError(f"refusing to run: BMEET_RUNTIME_ROOT={root!r} is not a test's own")
    return dict(env, BMEET_NOTIFY_LOG=str(notes))


def notes_in(path: Path) -> list[tuple[str, bool]]:
    """(title, ok) of the notifications a test's processes posted (BMEET_NOTIFY_LOG)."""
    try:
        return [(n["title"], n["ok"]) for n in map(json.loads, path.read_text().splitlines())]
    except OSError:
        return []


def no_apps_opened(test: unittest.TestCase) -> None:
    """For the rest of `test`, nothing in this process can run `open` or `osascript` (bmeet-ctl
    opens BMeet.app when its control socket does not answer): it fails the test instead."""
    from unittest import mock
    real = subprocess.Popen

    def popen(args, *more, **kwargs):
        argv = [args] if isinstance(args, (str, bytes)) else list(args)
        if argv and os.path.basename(str(argv[0])) in ("open", "osascript"):
            raise AssertionError(f"a test would have run {argv}")
        return real(args, *more, **kwargs)
    guard = mock.patch.object(subprocess, "Popen", popen)
    guard.start()
    test.addCleanup(guard.stop)


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
        self.env = sealed(self.env, self.tmp / "notes.jsonl")

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
        for k in ("BMEET_RUN_DIR", "FAKE_LIVE_FAIL", "FAKE_LIVE_NONE", "FAKE_RECORD_DIE_AFTER", "FAKE_PASS_KILLED",
                  "FAKE_PASS_FAIL"):
            self.env.pop(k, None)
        self.env = sealed(self.env, self.tmp / "notes.jsonl")

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

    @unittest.skipIf(os.geteuid() == 0, "root writes anywhere")
    def test_no_live_text_and_no_meetings_folder(self) -> None:
        # Only the speaker pass writes the transcript, next to the index: the index names it too,
        # so export finds it (and the window and bmeet-ctl look in that folder on a Mac)
        locked = self.tmp / "locked"
        locked.mkdir()
        locked.chmod(0o500)
        self.env.update(VOXTYPE_MEETING_DIR=str(locked / "Meetings"), FAKE_LIVE_NONE="1")
        self.assertEqual(self.run_cmd("start").returncode, 0)
        folder = self.folder()
        time.sleep(0.5)
        r = self.run_cmd("stop")
        self.assertEqual(r.returncode, 0, r.stderr)
        md = Path(r.stdout.strip())
        self.assertEqual(md.parent, self.tmp)
        self.assertEqual(self.rows(), [("completed", str(md))])
        self.wait_for(lambda: not folder.exists() and md.exists() and "Person 1" in md.read_text())
        [mid] = [m["id"] for m in json.loads(self.run_cmd("list").stdout)]
        r = self.run_cmd("export", "--", mid)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Person 1", r.stdout)

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

    def notes(self) -> list[tuple[str, bool]]:
        return notes_in(self.tmp / "notes.jsonl")

    def test_a_speaker_pass_that_dies_says_so(self) -> None:
        # Ended without a word of its own, as the Core ML crash of 2026-11-27 ended it (status -11;
        # here SIGKILL, which leaves no crash report): the live copy stays, with a line that says
        # the voices were not told apart, a notification says so, and the audio goes
        self.env["FAKE_PASS_KILLED"] = "1"
        self.assertEqual(self.run_cmd("start", "--in-person").returncode, 0)
        folder = self.folder()
        time.sleep(0.5)
        r = self.run_cmd("stop")
        self.assertEqual(r.returncode, 0, r.stderr)
        md = Path(r.stdout.strip())
        self.wait_for(lambda: not folder.exists())
        text = md.read_text()
        line = "- **Speaker pass:** the voices were not told apart: it was ended (SIGKILL)"
        self.assertIn(line, text)
        self.assertLess(text.index(line), text.index("## Transcript"))
        self.assertIn("hello", text)
        self.assertIn(("Speakers not told apart", False), self.notes())

    def test_a_speaker_pass_that_fails_is_marked_once(self) -> None:
        # npu-transcribe reports its own errors (status 1, its own notification): the transcript
        # gets the line, and no second notification comes from here
        self.env["FAKE_PASS_FAIL"] = "1"
        self.assertEqual(self.run_cmd("start").returncode, 0)
        folder = self.folder()
        time.sleep(0.5)
        md = Path(self.run_cmd("stop").stdout.strip())
        self.wait_for(lambda: not folder.exists())
        self.assertIn("- **Speaker pass:** the voices were not told apart: it ended with status 1", md.read_text())
        self.assertNotIn("Speakers not told apart", [title for title, _ in self.notes()])

    def test_a_speaker_pass_that_works_leaves_no_line(self) -> None:
        self.assertEqual(self.run_cmd("start").returncode, 0)
        folder = self.folder()
        time.sleep(0.5)
        md = Path(self.run_cmd("stop").stdout.strip())
        self.wait_for(lambda: not folder.exists())
        self.assertIn("Person 1", md.read_text())
        self.assertNotIn("Speaker pass", md.read_text())
        self.assertNotIn("Speakers not told apart", [title for title, _ in self.notes()])

    def test_a_pass_that_ends_before_its_pid_is_seen_is_not_a_failure(self) -> None:
        # unit_start says False for a unit that ran and ended before it saw the unit's pid (a fast
        # pass on a busy Mac, as on CI): its `after` has run, so nothing is marked or notified.
        # One that never started is: the transcript gets the line and a notification goes out.
        saved = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(saved)))
        os.environ.update(self.env)
        bm = load(MACOS / "bmeet-meeting", "bmeet_meeting_fast_pass_test")
        live = ("# M\n\n## Meeting Info\n\n- **Transcribed:** as people talked (live), fake\n\n"
                "## Transcript\n\n### You\n\nhello\n")
        for ran in (True, False):
            folder, md = bm.AUDIO_ROOT / f"rec-{ran}", self.tmp / f"m-{ran}.md"
            folder.mkdir(parents=True)
            md.write_text(live)

            def unit_start(name, cmd, after=None, nice=0, ran=ran):
                if ran and name.startswith("bmeet-speakers-"):   # (it ran and ended at once, after and all)
                    subprocess.run(after, env=dict(os.environ, BMEET_UNIT_STATUS="0"), check=True)
                return False
            bm.unit_start = unit_start
            bm.speaker_pass(folder.name, folder, md)
            self.assertFalse(folder.exists())
            if ran:
                self.assertNotIn("Speaker pass", md.read_text())
                self.assertEqual(self.notes(), [])
            else:
                self.assertIn("- **Speaker pass:** the voices were not told apart: it could not be started",
                              md.read_text())
                self.assertEqual(self.notes(), [("Speakers not told apart", False)])

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

    def test_notifications_of_tests_stay_off_the_screen(self) -> None:
        # BMEET_NOTIFY_LOG: into the file; a test's runtime folder without BMeet.app: nowhere,
        # osascript is not run; with neither seal, the guard of these tests refuses
        mac = load(MACOS / "bmeetmac.py", "bmeetmac_notify_test")
        tmp = Path(tempfile.mkdtemp(prefix="bm-"))
        saved = dict(os.environ)
        no_apps_opened(self)
        try:
            os.environ.update(BMEET_RUNTIME_ROOT=str(tmp), BMEET_NOTIFY_LOG=str(tmp / "notes.jsonl"))
            mac.notify("Title", "body", ok=False)
            self.assertEqual(notes_in(tmp / "notes.jsonl"), [("Title", False)])
            del os.environ["BMEET_NOTIFY_LOG"]
            mac.notify("Not shown")                   # (no agent.sock there, and osascript would fail the test)
            self.assertEqual(len(notes_in(tmp / "notes.jsonl")), 1)
            with self.assertRaises(RuntimeError):
                sealed({}, tmp / "x")
            if mac_runtime_root():
                with self.assertRaises(RuntimeError):
                    sealed({"BMEET_RUNTIME_ROOT": mac_runtime_root()}, tmp / "x")
            with self.assertRaises(AssertionError):   # (harmless even if the guard failed)
                subprocess.run(["open", "--help"], capture_output=True)
        finally:
            os.environ.clear()
            os.environ.update(saved)
            shutil.rmtree(tmp, ignore_errors=True)


def whisper_server() -> Path | None:
    p = MACOS / ".build/release/bmeet-whisper"
    return p if os.access(p, os.X_OK) else None


@unittest.skipUnless(sys.platform == "darwin" and whisper_server(), "macOS with bmeet-whisper built")
class WhisperServer(unittest.TestCase):
    """The real bmeet-whisper, without models (its status needs none)."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="bm-"))
        # (the setting from a folder of the test's own: XDG_CONFIG_HOME)
        self.env = dict(os.environ, BMEET_RUNTIME_ROOT=str(self.tmp / "run"), XDG_RUNTIME_DIR=str(self.tmp / "shell"),
                        XDG_CONFIG_HOME=str(self.tmp / "config"))
        for k in ("BMEET_UNLOAD_AFTER_SECONDS", "BMEET_WHISPER_TEST"):
            self.env.pop(k, None)
        (self.tmp / "config/bmeet").mkdir(parents=True)
        self.procs: list[subprocess.Popen] = []
        self.run_dir = self.tmp / "run/bmeet"

    def tearDown(self) -> None:
        for p in self.procs:
            if p.poll() is None:
                p.kill()
                p.wait()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def serve(self, *extra: str, **env: str) -> subprocess.Popen:
        """The server (env: more of its environment); what it logs goes to self.tmp/server.log."""
        with open(self.tmp / "server.log", "ab") as log:
            p = subprocess.Popen([str(whisper_server()), "serve", "--models", str(self.tmp / "models"), *extra],
                                 env=dict(self.env, **env), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=log)
        self.procs.append(p)
        return p

    def ask(self, request: dict, samples: int = 0, sock: Path | None = None) -> dict:
        """One request to the server (`samples` of silence after it, for the ops that take audio)."""
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(10)
            s.connect(str(sock or self.run_dir / "whisper.sock"))
            s.sendall(json.dumps(dict(request, samples=samples) if samples else request).encode() + b"\n"
                      + bytes(4 * samples))
            return json.loads(s.makefile("rb").readline())

    def stand_ins(self, extra: tuple[str, ...] = (), **env: str) -> subprocess.Popen:
        """The server with stand-in models (BMEET_WHISPER_TEST: they load at once, without files)."""
        p = self.serve(*extra, BMEET_WHISPER_TEST="1", **env)
        self.wait_for(self.run_dir / "whisper.pid")
        self.assertTrue(wait_until(lambda: self.ask({"op": "status"})["ok"]))
        return p

    def loaded(self) -> dict:
        return self.ask({"op": "status"})["loaded"]

    def logged(self) -> str:
        return (self.tmp / "server.log").read_text()

    def wait_for(self, path: Path) -> None:
        deadline = time.time() + 15
        while not path.exists() and time.time() < deadline:
            time.sleep(0.05)
        self.assertTrue(path.exists(), path)

    def status(self, sock: Path) -> dict:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(5)
            s.connect(str(sock))
            s.sendall(b'{"op": "status"}\n')
            return json.loads(s.makefile("rb").readline())

    def test_its_runtime_folder_and_only_its_own_files_go_when_it_ends(self) -> None:
        run = self.tmp / "run/bmeet"
        main = self.serve()
        self.wait_for(run / "whisper.pid")
        self.assertEqual(self.status(run / "whisper.sock")["pid"], main.pid)
        self.assertFalse((self.tmp / "shell").exists())    # (not the XDG_RUNTIME_DIR a shell exports)
        # another server on another socket: when it ends, the first one's socket and pid file stay
        other = self.serve("--socket", str(self.tmp / "o/x.sock"))
        self.wait_for(self.tmp / "o/whisper.pid")
        self.assertEqual(self.status(self.tmp / "o/x.sock")["pid"], other.pid)   # (it answers: all set up)
        other.terminate()
        self.assertEqual(other.wait(timeout=10), 0)
        self.assertFalse((self.tmp / "o/x.sock").exists())
        self.assertFalse((self.tmp / "o/whisper.pid").exists())
        self.assertEqual((run / "whisper.pid").read_text().strip(), str(main.pid))
        self.assertEqual(self.status(run / "whisper.sock")["pid"], main.pid)
        main.terminate()
        self.assertEqual(main.wait(timeout=10), 0)
        self.assertFalse((run / "whisper.sock").exists())
        self.assertFalse((run / "whisper.pid").exists())

    def test_a_look_or_a_status_keeps_no_model(self) -> None:
        # Clients that only look whether the server is up (bmeetmac.whisper_up) or ask for its
        # status, every 0.1 s: the model still goes when unload-after (1 s here) is over
        self.stand_ins(BMEET_UNLOAD_AFTER_SECONDS="1")
        t0 = time.time()
        self.assertEqual(self.ask({"op": "load", "model": "turbo"})["device"], "TEST")
        [(key, info)] = self.loaded().items()
        self.assertEqual(key, "turbo/ane")
        self.assertLess(abs(info["since"] - t0), 5)
        end = time.monotonic() + 6
        while self.loaded() and time.monotonic() < end:
            with socket.socket(socket.AF_UNIX) as look:   # (a look, closed at once)
                look.connect(str(self.run_dir / "whisper.sock"))
            time.sleep(0.1)
        self.assertEqual(self.loaded(), {})
        self.assertIn("unloaded turbo/ane (unused for", self.logged())

    def test_unload(self) -> None:
        self.stand_ins()
        self.ask({"op": "load", "model": "turbo"})
        self.ask({"op": "load", "model": "small"})
        self.assertEqual(sorted(self.loaded()), ["small/ane", "turbo/ane"])
        self.assertEqual(self.ask({"op": "unload", "model": "small"})["unloaded"], ["small/ane"])
        self.assertEqual(sorted(self.loaded()), ["turbo/ane"])
        self.assertEqual(self.ask({"op": "unload"})["unloaded"], ["turbo/ane"])
        self.assertEqual(self.loaded(), {})
        self.assertEqual(self.ask({"op": "unload"})["unloaded"], [])

    def test_the_setting_is_read_when_it_is_needed(self) -> None:
        # ~/.config/bmeet/unload-after: 60 minutes keeps the model after a transcription; changed to
        # 0 between two, it goes right after the second (no restart)
        setting = self.tmp / "config/bmeet/unload-after"
        setting.write_text("60\n")
        self.stand_ins()
        request = {"op": "transcribe", "model": "turbo", "language": "en"}
        self.assertTrue(self.ask(request, samples=1600)["ok"])
        self.assertEqual(self.ask({"op": "status"})["unload_after"], 3600)
        time.sleep(1.0)
        self.assertEqual(sorted(self.loaded()), ["turbo/ane"])
        setting.write_text("0\n")
        self.assertTrue(self.ask(request, samples=1600)["ok"])
        self.assertTrue(wait_until(lambda: self.loaded() == {}, 3), self.loaded())
        self.assertIn("unloaded turbo/ane (unload-after is 0", self.logged())

    def test_memory_pressure_releases_the_models(self) -> None:
        (self.tmp / "config/bmeet/unload-after").write_text("60\n")
        self.stand_ins()
        self.ask({"op": "load", "model": "turbo"})
        self.assertEqual(self.ask({"op": "pressure", "level": "critical"}), {"ok": True})
        self.assertTrue(wait_until(lambda: self.loaded() == {}, 3), self.loaded())
        self.assertIn("unloaded turbo/ane (memory pressure: critical)", self.logged())
        # (the test op exists only with BMEET_WHISPER_TEST: not in the server BMeet starts)
        self.serve("--socket", str(self.tmp / "o/x.sock"))
        self.wait_for(self.tmp / "o/whisper.pid")
        self.assertEqual(self.ask({"op": "pressure"}, sock=self.tmp / "o/x.sock"),
                         {"ok": False, "error": "unknown op pressure"})

    def test_a_one_off_model_does_not_take_the_install_models_time(self) -> None:
        # turbo (the install's model) keeps the whole of unload-after (10 minutes here) after a
        # transcription with small; small, used last, gets at most 3 minutes
        self.stand_ins(BMEET_UNLOAD_AFTER_SECONDS="600")
        for model in ("turbo", "small"):
            self.assertTrue(self.ask({"op": "transcribe", "model": model, "language": "en"}, samples=1600)["ok"])
        loaded = self.loaded()
        self.assertGreater(loaded["turbo/ane"]["unload_in"], 500)
        self.assertLessEqual(loaded["small/ane"]["unload_in"], 180)

    def test_it_exits_when_idle_though_asked_for_its_status(self) -> None:
        # --idle 1: no model loaded or used for a second, and only status requests: it exits
        p = self.stand_ins(extra=("--idle", "1"))
        end = time.monotonic() + 10
        while p.poll() is None and time.monotonic() < end:
            with contextlib.suppress(OSError, ValueError):
                self.ask({"op": "status"})
            time.sleep(0.2)
        self.assertEqual(p.poll(), 0)
        self.assertFalse((self.run_dir / "whisper.sock").exists())
        self.assertIn("no model loaded or used for 1 s: exiting", self.logged())


INSTALLED_MODELS = Path(os.environ.get("NPU_MODELS_DIR") or Path.home() / ".local/share/npu-models")


def coreml_models() -> bool:
    """The installed Core ML models (read only here), coremltools, `say` and ffmpeg: a Mac with BMeet."""
    return (sys.platform == "darwin" and importlib.util.find_spec("coremltools") is not None
            and bool(shutil.which("say")) and bool(shutil.which("ffmpeg"))
            and all((INSTALLED_MODELS / p).exists() for p in (
                "gigaam-v3-ctc-ru/model.mlmodelc", "gigaam-v3-ctc-ru/tokens.txt", "speaker-diarization/titanet.mlmodelc",
                "speaker-diarization/nemotron-3-diarization/embedder.mlmodelc",
                "speaker-diarization/nemotron-3-diarization/encoder.mlmodelc")))


# GigaAM, then the speaker models, on the Neural Engine, with a pause after each prediction that
# is longer than Core ML takes to let go of a model's inputs on its own thread (~1.2 s after the
# first prediction, measured on macOS 27): the pass of 2026-11-27 crashed in such a pause (status
# -11). Prints what it heard, how many voices, and how many predictions' inputs bmeetmac still held
# right after each prediction and after each pause (information only: it depends on macOS).
COREML_DRIVER = r'''
import importlib.machinery, importlib.util, json, sys, time
from pathlib import Path
repo, wav, pause = Path(sys.argv[1]), Path(sys.argv[2]), float(sys.argv[3])
loader = importlib.machinery.SourceFileLoader("npu_transcribe", str(repo / "transcribe/npu-transcribe"))
spec = importlib.util.spec_from_loader(loader.name, loader)
nt = importlib.util.module_from_spec(spec)
sys.modules[loader.name] = nt
loader.exec_module(nt)
samples = nt.decode_all(wav)
gigaam = nt.GigaAM(["ANE", "CPU"])
words, held = [], []
for _, block in nt.array_blocks(samples):
    words += [w.text for w in gigaam.words(block)]
    held.append(nt.bmeetmac.release_coreml_inputs())
    time.sleep(pause)
    held.append(nt.bmeetmac.release_coreml_inputs())
turns, _, _ = nt.diarize(nt.SpeakerModels(["ANE", "CPU"]), samples)
time.sleep(pause)
held.append(nt.bmeetmac.release_coreml_inputs())
print(json.dumps({"text": "".join(words), "voices": len({t.speaker for t in turns}), "device": gigaam.device,
                  "held": held}, ensure_ascii=False))
'''

SAID = [   # (two people: the second one's voice made lower with ffmpeg)
    "Добрый день, коллеги. Сегодня мы обсуждаем план работ на следующую неделю.",
    "Спасибо. Я подготовил отчет о продажах за прошлый месяц, цифры хорошие.",
    "Отлично. Тогда давайте начнем с отчета, а потом перейдем к бюджету.",
    "Хорошо. Бюджет на рекламу мы предлагаем увеличить на десять процентов.",
]


@unittest.skipUnless(coreml_models(), "a Mac with BMeet's Core ML models installed (not in CI: no models)")
class CoreMLInputs(unittest.TestCase):
    """The installed GigaAM, Nemotron and TitaNet on the Neural Engine, in processes this test
    starts and ends, on speech made with `say` (nothing is recorded or played). Local only."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="bm-"))
        # (a runtime and config folder of its own: no Whisper is asked, and none of yours is touched)
        self.env = sealed(dict(os.environ, BMEET_RUNTIME_ROOT=str(self.tmp / "run"), XDG_RUNTIME_DIR=str(self.tmp / "run"),
                               XDG_CONFIG_HOME=str(self.tmp / "config"),
                               NPU_TRANSCRIBE_HISTORY=str(self.tmp / "history.jsonl")), self.tmp / "notes.jsonl")
        self.wav = self.tmp / "meeting.wav"
        self.speech(self.wav)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def speech(self, out: Path) -> None:
        """SAID, two voices taking turns, 2.5 s of quiet between them, 16 kHz mono WAV."""
        import numpy as np
        rate, pieces = 16000, []
        for i, text in enumerate(SAID):
            aiff, wav = self.tmp / f"{i}.aiff", self.tmp / f"{i}.wav"
            subprocess.run(["say", "-v", "Milena", "-o", str(aiff), text], check=True, timeout=60)
            voice = ["-af", "asetrate=16000*0.8,aresample=16000,atempo=1.25"] if i % 2 else []
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(aiff), "-ar", "16000", "-ac", "1", *voice, str(wav)],
                           check=True, timeout=60)
            with wave.open(str(wav)) as w:
                pieces += [np.frombuffer(w.readframes(w.getnframes()), "<i2"), np.zeros(int(2.5 * rate), "<i2")]
        with wave.open(str(out), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(np.concatenate(pieces).tobytes())

    def test_pauses_after_predictions_do_not_crash(self) -> None:
        r = subprocess.run([PY, "-I", "-c", COREML_DRIVER, str(REPO), str(self.wav), "3"], env=self.env,
                           capture_output=True, text=True, timeout=600)
        self.assertEqual(r.returncode, 0, r.stderr[-3000:])
        out = json.loads(r.stdout.strip().splitlines()[-1])
        print(f"\n  Core ML inputs still held (after each prediction, after each pause): {out['held']}",
              file=sys.stderr)
        self.assertRegex(out["text"], "[а-яА-Я]{3}")
        self.assertGreaterEqual(out["voices"], 1)

    def test_a_speaker_pass(self) -> None:
        # npu-transcribe --meeting on an in-person recording, Russian given and no English terms:
        # GigaAM and the speaker models only (no Whisper), as the pass of 2026-11-27 ran
        folder = self.tmp / "rec"
        folder.mkdir()
        shutil.copy(self.wav, folder / "part-1-mic.wav")
        (folder / "meeting.json").write_text(json.dumps({"title": "Test", "started": int(time.time()), "in_person": True}))
        out = self.tmp / "out.md"
        r = subprocess.run([PY, "-I", str(REPO / "transcribe/npu-transcribe"), "--meeting", str(folder), "--output", str(out),
                            "--language", "ru", "--no-english-terms"], env=self.env, capture_output=True, text=True,
                           timeout=600)
        self.assertEqual(r.returncode, 0, r.stderr[-3000:])
        text = out.read_text()
        self.assertIn("## Transcript", text)
        self.assertRegex(text, "[а-яА-Я]{3}")
        self.assertNotIn("bmeet-whisper", r.stderr)


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
        # bmeet-ctl opens BMeet.app when its control socket does not answer: here that must never
        # reach the BMeet.app of this Mac, nor a notification the screen
        no_apps_opened(self)
        sealed(dict(os.environ), self.tmp / "notes.jsonl")       # (checks BMEET_RUNTIME_ROOT)
        os.environ["BMEET_NOTIFY_LOG"] = str(self.tmp / "notes.jsonl")

    def tearDown(self) -> None:
        self.server.close()
        shutil.rmtree(self.tmp, ignore_errors=True)
        for k in ("BMEET_RUNTIME_ROOT", "XDG_RUNTIME_DIR", "XDG_CONFIG_HOME", "BMEET_NOTIFY_LOG"):
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

    def test_updates_of_this_system(self) -> None:
        # BMeet for macOS's own release line: its macos-vX.Y.Z releases and macos/VERSION, not Linux's
        from unittest import mock
        mine = (MACOS / "VERSION").read_text().strip()
        major, minor, patch = (int(x) for x in mine.split("."))
        newer = f"{major}.{minor}.{patch + 1}"
        (self.tmp / "releases.json").write_text(json.dumps([
            {"tag_name": "v99.0.0", "draft": False, "prerelease": False, "html_url": "https://x/linux"},
            {"tag_name": f"macos-v{newer}", "draft": False, "prerelease": False, "html_url": "https://x/mac"}]))
        env = {"XDG_CACHE_HOME": str(self.tmp / "cache"), "XDG_DATA_HOME": str(self.tmp / "data"),
               "BMEET_RELEASES_URL": (self.tmp / "releases.json").as_uri()}
        os.environ.update(env)
        os.environ.pop("BMEET_TERMINAL", None)
        try:
            ctl = load(REPO / "bmeet/bmeet-ctl", "bmeet_ctl_mac_update_test")
            state = ctl.check_update()
            self.assertEqual(ctl.update_info(state), {"version": newer, "tag": f"macos-v{newer}",
                                                      "url": "https://x/mac", "installed": mine})
            self.assertEqual(ctl.status()["update"]["tag"], f"macos-v{newer}")
            # the Update button: Terminal runs it, from a .command file
            with mock.patch.object(ctl.subprocess, "call", return_value=0) as call:
                self.assertEqual(ctl.main(["update", "--terminal"]), 0)
            script = self.tmp / "cache/bmeet/update.command"
            call.assert_called_once_with(["open", str(script)])
            self.assertTrue(os.access(script, os.X_OK))
            self.assertIn(f"{REPO / 'bmeet/bmeet-ctl'} update;", script.read_text())
        finally:
            for k in env:
                os.environ.pop(k, None)


# curl for fetch-models: serves SOURCE, and cuts off as many calls as CUTS says (half of what is
# left, then exit 56). Every call is logged with the size its .part had.
FAKE_CURL = '''#!{py} -I
import os, sys
from pathlib import Path
here = Path(__file__).resolve().parent
out = Path(sys.argv[sys.argv.index("-o") + 1])
data = (here / "source").read_bytes()
have = out.stat().st_size if out.exists() else 0
with open(here / "calls", "a") as log:
    log.write(f"{{have}}\\n")
cuts = int((here / "cuts").read_text())
with open(out, "ab") as f:
    if cuts > 0:
        (here / "cuts").write_text(str(cuts - 1))
        f.write(data[have:have + (len(data) - have) // 2])
        sys.exit(56)
    f.write(data[have:])
'''


class ComputePlan(unittest.TestCase):
    def test_a_plan_that_cannot_be_made_is_only_reported(self) -> None:
        # (MLComputePlan: macOS 14.4 and later; whatever coremltools raises, the build goes on)
        prep = load(MACOS / "prepare-coreml", "prepare_coreml_test")
        for exc in (RuntimeError("x"), ValueError("no compute plan"), Exception("no proxy")):
            def plan(_compiled, exc=exc):
                raise exc
            prep.plan = plan
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(prep.report_plan(Path("model.mlmodelc")), 0.0)
            self.assertIn("compute plan: not available on this macOS", out.getvalue())


class FetchModels(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="bm-"))
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.data = os.urandom(200_000)
        (self.bin / "source").write_bytes(self.data)
        (self.bin / "curl").write_text(FAKE_CURL.format(py=PY))
        (self.bin / "curl").chmod(0o755)
        self.file = "var1/AudioEncoder.mlmodelc/weights/weight.bin"
        (self.tmp / "m.tsv").write_text(f"revision\tabcdef0123\n{self.file}\t{len(self.data)}\t"
                                        f"{hashlib.sha256(self.data).hexdigest()}\n")
        self.env = dict(os.environ, PATH=f"{self.bin}{os.pathsep}{os.environ.get('PATH', '')}")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def fetch(self, cuts: int) -> subprocess.CompletedProcess:
        (self.bin / "cuts").write_text(str(cuts))
        return subprocess.run([PY, "-I", str(MACOS / "fetch-models"), str(self.tmp / "m.tsv"), str(self.tmp / "dest"),
                               "var1"], env=self.env, capture_output=True, text=True, timeout=60)

    def calls(self) -> list[int]:
        return [int(v) for v in (self.bin / "calls").read_text().split()]

    def test_a_cut_off_download_goes_on(self) -> None:
        r = self.fetch(cuts=1)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((self.tmp / "dest" / self.file).read_bytes(), self.data)
        self.assertEqual(self.calls(), [0, len(self.data) // 2])   # (the second attempt resumed)

    def test_the_next_run_goes_on_too(self) -> None:
        r = self.fetch(cuts=2)
        self.assertNotEqual(r.returncode, 0)
        part = self.tmp / "dest" / (self.file + ".part")
        self.assertTrue(part.is_file())
        kept = part.stat().st_size
        self.assertGreater(kept, 0)
        r = self.fetch(cuts=0)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((self.tmp / "dest" / self.file).read_bytes(), self.data)
        self.assertEqual(self.calls()[-1], kept)
        self.assertFalse(part.exists())


if __name__ == "__main__":
    unittest.main()
