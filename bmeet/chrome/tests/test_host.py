#!/usr/bin/env python3
"""Offline tests of bmeet-chrome-host (no browser): what it writes where.

Run: python3 -I bmeet/chrome/tests/test_host.py -v
"""
from __future__ import annotations

import json
import os
import shutil
import struct
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

HOST = Path(__file__).resolve().parent.parent / "bmeet-chrome-host"


def framed(*messages) -> bytes:
    out = b""
    for m in messages:
        data = m if isinstance(m, bytes) else json.dumps(m).encode()
        out += struct.pack("=I", len(data)) + data
    return out


class Host(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="bmeet-host-"))
        self.root = self.work / "meeting-audio"
        self.meeting = self.root / "2026-10-13_1200"
        self.meeting.mkdir(parents=True)
        self.pointer = self.work / "recording"
        self.env = dict(os.environ, BMEET_RECORDING_FILE=str(self.pointer), BMEET_AUDIO_DIR=str(self.root))

    def tearDown(self):
        shutil.rmtree(self.work, ignore_errors=True)

    def run_host(self, data: bytes) -> subprocess.CompletedProcess:
        return subprocess.run([str(HOST)], input=data, env=self.env, capture_output=True, timeout=10)

    def log(self) -> list[dict]:
        path = self.meeting / "call-mic.jsonl"
        return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []

    def test_writes_into_the_recording_meeting(self):
        self.pointer.write_text(f"{self.meeting}\n")
        now = time.time()
        r = self.run_host(framed({"t": now, "call": True, "live": True},
                                 {"t": now + 1, "call": True, "live": False},
                                 {"call": "yes", "live": False},          # not ours: skipped
                                 b"not json",
                                 {"t": 5, "call": False, "live": False}))  # far-off clock: replaced
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, b"")  # it never answers
        log = self.log()
        self.assertEqual([(x["call"], x["live"]) for x in log], [(True, True), (True, False), (False, False)])
        self.assertAlmostEqual(log[0]["t"], now, places=2)
        self.assertGreater(log[2]["t"], now - 5)
        self.assertEqual((self.meeting / "call-mic.jsonl").stat().st_mode & 0o777, 0o600)

    def test_nothing_without_a_meeting(self):
        r = self.run_host(framed({"t": time.time(), "call": True, "live": True}))
        self.assertEqual(r.returncode, 0)
        self.assertEqual(self.log(), [])

    def test_only_into_bmeet_meetings_being_recorded(self):
        elsewhere = self.work / "elsewhere"
        elsewhere.mkdir()
        self.pointer.write_text(f"{elsewhere}\n")
        self.run_host(framed({"t": time.time(), "call": True, "live": True}))
        self.assertFalse((elsewhere / "call-mic.jsonl").exists())
        self.pointer.write_text(f"{self.meeting}\n")
        (self.meeting / "speaker-pass").touch()   # recording over: its speaker pass runs
        self.run_host(framed({"t": time.time(), "call": True, "live": True}))
        self.assertEqual(self.log(), [])

    def test_oversized_message_ends_it(self):
        self.pointer.write_text(f"{self.meeting}\n")
        r = self.run_host(struct.pack("=I", 10_000_000) + b"x" * 100)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(self.log(), [])


if __name__ == "__main__":
    unittest.main()
