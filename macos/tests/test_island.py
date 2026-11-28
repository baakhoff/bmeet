#!/usr/bin/env python3
"""Tests of BMeet's island around the camera housing (macos/Sources/BMeet/Island.swift), through
the check modes of BMeet's own executable (Checks.swift: --island-check, and --control-check,
--settings-check and --settings-act with a screen and housing given). macOS only.

Run: python3 -I macos/tests/test_island.py -v
(BMeet comes from macos/.build/release: swift build -c release --package-path macos; without it
the tests that need it skip.)

Nothing is shown and nothing of yours is touched or started: every run of BMeet gets folders of
its own (XDG_CONFIG_HOME, BMEET_RUNTIME_ROOT, BMEET_LOGS, ...), the island is laid out and drawn
off screen, and its screen and housing are the ones a test gives (--screen, --housing), never
this Mac's. The control socket's runs get a stand-in for BMeet's Python, so no dictation or
meeting records and no text is pasted. No shortcut is registered: Esc, which the open island
holds, is only noted.

- Where it is: centred on the housing and at least as wide and as tall, for this Mac's housing
  (1800 x 1182 points, 70 x 47.5) and an older notch (1512 x 982, 185 x 32); none on a screen
  without one.
- Every state, closed and open, in English and Russian: no text cut short, none outside the
  island or under the camera, every text translated.
- The switch (~/.config/bmeet/island): off gives no island, and the dictation panel at the
  bottom shows the words instead; the island takes the panel's place only where it shows, and
  hands the words back to it when it goes.
- Settings -> Features: the row, disabled on a Mac without a housing; the switch writes its file.
- A click opens it and holds Esc; Esc or a click outside closes it (a click outside its shape
  goes through); hover grows it a little; its controls ask BMeet for the right thing.
- What BMeet makes of `bmeet-ctl status` answers: a meeting and its time, paused, in person; the
  speaker pass, then saved or not told apart; a failure that waits until it is looked at.
- The pictures: PNGs at twice the size (a Retina screen's), and a sheet of every state.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
MACOS = HERE.parent
REPO = MACOS.parent
BMEET = MACOS / ".build/release/BMeet"
CYRILLIC = re.compile("[а-яА-Я]")
# This Mac (MacBook Pro M5 Pro): the screen and its housing, X from the screen's left edge
THIS_MAC = ("--screen", "1800,1182", "--housing", "865,70,47.5")
OLD_NOTCH = ("--screen", "1512,982", "--housing", "663.5,185,32")
NO_HOUSING = ("--screen", "1440,900", "--housing", "none")
STATES = ["listening", "words", "transcribing", "meeting", "meeting-paused", "in-person", "in-person-paused",
          "speakers", "saved", "failed"]


class Folders:
    """A HOME-like set of folders for one test: BMeet's settings, runtime, logs, models, cache."""

    def __init__(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="bm-"))   # (short: the socket path has a 104-byte limit)
        self.config = self.root / "cfg/bmeet"
        self.config.mkdir(parents=True)
        self.run_dir = self.root / "run/bmeet"
        self.env = dict(os.environ, BMEET_RUNTIME_ROOT=str(self.root / "run"), XDG_CONFIG_HOME=str(self.root / "cfg"),
                        BMEET_LOGS=str(self.root / "logs"), NPU_MODELS_DIR=str(self.root / "models"),
                        XDG_CACHE_HOME=str(self.root / "cache"), XDG_DATA_HOME=str(self.root / "data"),
                        BMEET_VOICES_DIR=str(self.root / "data/bmeet/voices"), BMEET_SRC=str(REPO), BMEET_NO_NOTIFY="1")
        self.env.pop("BMEET_PYTHON", None)

    def set(self, name: str, value: str) -> None:
        (self.config / name).write_text(value + "\n")

    def get(self, name: str) -> str | None:
        try:
            return (self.config / name).read_text().strip()
        except OSError:
            return None

    def run(self, *args: str, check: bool = True, timeout: float = 60) -> subprocess.CompletedProcess:
        p = subprocess.run([str(BMEET), *args], env=self.env, cwd=self.root, capture_output=True, text=True,
                           timeout=timeout)
        if check and p.returncode != 0:
            raise AssertionError(f"BMeet {' '.join(args)}: exit {p.returncode}\n{p.stdout}\n{p.stderr}")
        return p

    def lines(self, *args: str, check: bool = True) -> list[dict]:
        return [json.loads(line) for line in self.run(*args, check=check).stdout.splitlines()]

    def island(self, state: str, *more: str, geometry: tuple = THIS_MAC, lang: str = "en") -> list[dict]:
        return self.lines("--island-check", state, *geometry, "--lang", lang, *more)

    def status(self, *answers: dict, more: tuple = (), geometry: tuple = THIS_MAC) -> list[dict]:
        """What BMeet makes of these `bmeet-ctl status` answers in turn (one line each, then the acts')."""
        files = []
        for i, answer in enumerate(answers):
            path = self.root / f"status-{i}.json"
            path.write_text(json.dumps(answer))
            files += ["--status", str(path)]
        return self.lines("--island-check", "status", *files, *geometry, "--lang", "en", *more)

    def close(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


@unittest.skipUnless(sys.platform == "darwin" and BMEET.exists(), "macOS, with BMeet built (swift build -c release --package-path macos)")
class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.f = Folders()
        self.addCleanup(self.f.close)

    def assertJoinedToTheHousing(self, line: dict) -> None:
        """Centred on the housing, at least as wide, from the top of the screen down past it."""
        x, y, w, h = line["frame"]
        hx, hy, hw, hh = line["housing"]
        sx, sy, sw, sh = line["screen"]
        self.assertAlmostEqual(x + w / 2, hx + hw / 2, delta=0.5, msg=line["state"])
        self.assertGreaterEqual(w, hw)
        self.assertEqual(y + h, sy + sh)          # (its top is the screen's)
        self.assertLessEqual(y, hy)                # (as tall as the housing at least)
        self.assertGreaterEqual(x, sx)
        self.assertLessEqual(x + w, sx + sw)


class Where(Base):
    def check_geometry(self, geometry: tuple, housing: list[float]) -> None:
        lines = self.f.island("all", geometry=geometry)
        self.assertEqual(len(lines), 2 * len(STATES))
        for line in lines:
            with self.subTest(state=line["state"], open=line["open"]):
                self.assertTrue(line["island"])
                self.assertFalse(line["panel"])
                self.assertEqual(line["housing"], housing)
                self.assertJoinedToTheHousing(line)
                self.assertTrue(line["centred"])
                self.assertTrue(line["ok"], line)
                closed = not line["open"] and line["state"] != "words"
                if closed:   # (compact: exactly as tall as the housing, one shape with it)
                    self.assertEqual(line["frame"][3], housing[3])
                else:        # (open, or with the words: it opens downward)
                    self.assertGreater(line["frame"][3], housing[3] + 30)

    def test_this_macs_housing(self) -> None:
        self.check_geometry(THIS_MAC, [865, 1134.5, 70, 47.5])

    def test_an_older_notch(self) -> None:
        self.check_geometry(OLD_NOTCH, [663.5, 950, 185, 32])

    def test_the_compact_island_widens_to_both_sides_alike(self) -> None:
        # a longer text on the right widens the left side as much: it stays centred on the housing
        short = self.f.island("listening")[0]["frame"]
        long = self.f.island("speakers")[0]["frame"]
        self.assertGreater(long[2], short[2])
        self.assertAlmostEqual(long[0] + long[2] / 2, short[0] + short[2] / 2, delta=0.01)

    def test_no_island_without_a_housing(self) -> None:
        for geometry in (NO_HOUSING, ("--screen", "1440,900"), ()):
            for line in self.f.island("all", geometry=geometry):
                with self.subTest(geometry=geometry, state=line["state"]):
                    self.assertFalse(line["island"])
                    self.assertTrue(line["panel"])   # (the dictation panel at the bottom, as before)
                    self.assertIsNone(line["frame"])
                    self.assertIsNone(line["housing"])

    def test_this_macs_own_screens(self) -> None:
        # (measured for real: a housing here, none on CI's Mac; only that it is one or the other)
        line = self.f.lines("--island-check", "meeting", "--screen", "real")[0]
        if line["island"]:
            self.assertJoinedToTheHousing(line)
        else:
            self.assertIsNone(line["housing"])

    def test_geometry_that_cannot_be(self) -> None:
        for args in (["--housing", "865,70,47.5"], ["--screen", "1800"], ["--screen", "1800,1182", "--housing", "1790,70,47.5"],
                     ["--screen", "1800,1182", "--housing", "865,0,47.5"], ["--screen", "real", "--housing", "none"],
                     ["--screen", "a,b"]):
            with self.subTest(args=args):
                p = self.f.run("--island-check", "meeting", *args, check=False)
                self.assertEqual(p.returncode, 2)
                self.assertEqual(p.stdout, "")


class Texts(Base):
    def check(self, lang: str) -> list[dict]:
        lines = []
        for geometry in (THIS_MAC, OLD_NOTCH):
            p = self.f.run("--island-check", "all", *geometry, "--lang", lang, check=False)
            lines += [json.loads(line) for line in p.stdout.splitlines()]
            self.assertEqual(p.returncode, 0, p.stdout)
        for line in lines:
            with self.subTest(state=line["state"], open=line["open"], lang=lang, housing=line["housing"]):
                self.assertEqual(line["cut"], [], "texts cut short")
                self.assertEqual(line["outside"], [], "outside the island")
                self.assertEqual(line["under_camera"], [], "under the camera")
                self.assertEqual(line["untranslated"], [], "texts without their Russian line")
                self.assertEqual(line["lang"], lang)
        return lines

    def test_every_state_in_english(self) -> None:
        lines = {(line["state"], line["open"]): line for line in self.check("en")}
        self.assertEqual(lines[("meeting", False)]["texts"], ["12:34"])
        self.assertEqual(lines[("listening", False)]["texts"], ["EN"])
        self.assertIn("Online meeting", lines[("meeting", True)]["texts"])
        self.assertEqual(lines[("meeting", True)]["buttons"], ["pause", "stop-meeting"])
        self.assertEqual(lines[("meeting-paused", True)]["buttons"], ["resume", "stop-meeting"])
        self.assertIn("In-person meeting", lines[("in-person", True)]["texts"])
        self.assertEqual(lines[("listening", True)]["buttons"], ["stop", "cancel"])
        self.assertEqual(lines[("transcribing", True)]["buttons"], ["cancel"])
        self.assertEqual(lines[("speakers", True)]["buttons"], ["open"])
        self.assertEqual(lines[("saved", False)]["texts"], ["Saved"])
        self.assertIn("2026-11-28_1405-meeting.md", lines[("saved", True)]["texts"])
        self.assertEqual(lines[("failed", True)]["buttons"], ["open", "dismiss"])
        # the words while you speak, closed and open
        self.assertTrue(any("finish the island and send it out for review" in t for t in lines[("words", False)]["texts"]))
        self.assertTrue(any("finish the island" in t for t in lines[("words", True)]["texts"]))
        for (state, opened), line in lines.items():
            if not opened:
                self.assertEqual(line["buttons"], [], state)   # (controls only when it is open)

    def test_every_state_in_russian(self) -> None:
        for line in self.check("ru"):
            # every text but names, codes, times and a transcript's file name is in Russian
            english = [t for t in line["texts"] if re.search("[a-zA-Z]{4,}", t) and not CYRILLIC.search(t)
                       and not re.fullmatch(r"[\w-]+\.md", t)]
            self.assertEqual(english, [], f"English in {line['state']} in Russian")
        lines = {(line["state"], line["open"]): line for line in self.check("ru")}
        self.assertIn("Онлайн-встреча", lines[("meeting", True)]["texts"])
        self.assertIn("Стоп", lines[("meeting", True)]["texts"])
        self.assertEqual(lines[("saved", False)]["texts"], ["Сохранено"])


class Switch(Base):
    def test_off_gives_no_island_and_the_panel(self) -> None:
        self.f.set("island", "off")
        for line in self.f.island("all"):
            with self.subTest(state=line["state"]):
                self.assertFalse(line["island"])
                self.assertTrue(line["panel"])
        self.f.set("island", "on")
        self.assertTrue(self.f.island("meeting")[0]["island"])


# A stand-in for BMeet's Python (BMEET_PYTHON), as in test_settings.py: as `npu-transcribe
# --dictate` it hears nothing, and ends each dictation with no text, so nothing is pasted.
STAND_IN = """#!{python} -I
import json, sys
log = open({log!r}, "a", buffering=1)
log.write(" ".join(sys.argv[1:]) + "\\n")
if "--dictate" in sys.argv:
    for line in sys.stdin:
        if '"stop"' in line or '"cancel"' in line:
            print(json.dumps({{"done": True, "text": ""}} if '"stop"' in line else {{"cancelled": True}}), flush=True)
    log.write("engine ended\\n")
"""


class DictationPanel(Base):
    """The real dictation, with the stand-in engine: where the words go."""

    def start(self, *geometry: str) -> None:
        log = self.f.root / "ran.log"
        log.touch()
        python = self.f.root / "python"
        python.write_text(STAND_IN.format(python=sys.executable, log=str(log)))
        python.chmod(0o755)
        self.f.env["BMEET_PYTHON"] = str(python)
        self.proc = subprocess.Popen([str(BMEET), "--control-check", *geometry], env=self.f.env, cwd=self.f.root,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(self.stop)
        ready = json.loads(self.proc.stdout.readline() or "{}")
        self.assertTrue(ready.get("ready"), self.proc.stderr.read() if self.proc.poll() is not None else "")
        self.sock = ready["socket"]
        self.assertTrue(self.sock.startswith(str(self.f.root)))   # (never the real one)

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        self.proc.stdout.close()
        self.proc.stderr.close()

    def send(self, req: dict) -> tuple[dict, dict]:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(10)
            s.connect(self.sock)
            s.sendall(json.dumps(req).encode() + b"\n")
            answer = json.loads(s.makefile().readline())
        return answer, json.loads(self.proc.stdout.readline())

    def wait_for(self, check, timeout: float = 8.0) -> bool:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if check():
                return True
            time.sleep(0.1)
        return False

    def test_the_island_takes_the_panels_place_where_it_shows(self) -> None:
        self.start(*THIS_MAC)
        answer, line = self.send({"op": "dictate"})
        self.assertEqual(answer, {"ok": True})
        self.assertEqual(line["dictation"], "recording")
        self.assertEqual(line["island"], "listening")
        self.assertEqual(line["panel_shown"], 0)
        # turned off while it runs: the panel takes the words back, at once
        self.f.set("island", "off")
        self.assertTrue(self.wait_for(lambda: self.send({"op": "status"})[1]["panel_shown"] >= 1))
        self.assertEqual(self.send({"op": "status"})[1]["island"], "idle")
        self.send({"op": "cancel"})

    def test_without_a_housing_the_panel_shows(self) -> None:
        self.start(*NO_HOUSING)
        _, line = self.send({"op": "dictate"})
        self.assertEqual(line["panel_shown"], 1)
        self.assertEqual(line["island"], "idle")
        self.send({"op": "cancel"})

    def test_with_the_island_off_the_panel_shows(self) -> None:
        self.f.set("island", "off")
        self.start(*THIS_MAC)
        _, line = self.send({"op": "dictate"})
        self.assertEqual(line["panel_shown"], 1)
        self.assertEqual(line["island"], "idle")
        self.send({"op": "cancel"})


class FeaturesPage(Base):
    ROW = "Show BMeet around the camera"

    def test_the_row_with_a_housing(self) -> None:
        out = self.f.lines("--settings-act", "page:features", *THIS_MAC)[0]
        self.assertIn(self.ROW, out["texts"])
        self.assertNotIn(self.ROW, out["disabled"])
        self.assertIn("Dictation and meetings show around the camera; click there for their controls.",
                      out["texts"])
        self.assertTrue(any("Around the camera, BMeet shows that it listens." in t for t in out["texts"]))

    def test_the_row_is_disabled_without_a_housing(self) -> None:
        for geometry in (NO_HOUSING, ()):
            out = self.f.lines("--settings-act", "page:features", *geometry)[0]
            with self.subTest(geometry=geometry):
                self.assertIn(self.ROW, out["disabled"])
                self.assertIn("This Mac's screen has no camera housing.", out["texts"])
                # (dictation's caption names the panel at the bottom, where BMeet shows itself here)
                self.assertTrue(any("A small panel at the bottom of the screen" in t for t in out["texts"]))

    def test_the_switch_writes_its_file(self) -> None:
        self.f.lines("--settings-act", "island", *THIS_MAC)
        self.assertEqual(self.f.get("island"), "off")
        out = self.f.lines("--settings-act", "page:features", *THIS_MAC)[0]
        self.assertTrue(any("A small panel at the bottom of the screen" in t for t in out["texts"]))
        self.f.lines("--settings-act", "island", *THIS_MAC)
        self.assertEqual(self.f.get("island"), "on")

    def test_the_page_fits_in_both_languages(self) -> None:
        for geometry in (THIS_MAC, NO_HOUSING):
            for lang in ("en", "ru"):
                line = self.f.lines("--settings-check", "features", "--lang", lang, *geometry)[0]
                with self.subTest(geometry=geometry, lang=lang):
                    self.assertTrue(line["ok"], line)
                    self.assertLessEqual(line["height"], 600)
                    self.assertEqual(line["cut"], [])
                    self.assertEqual(line["untranslated"], [])
        ru = self.f.lines("--settings-act", "page:features", "--lang", "ru", *THIS_MAC)[0]
        self.assertIn("Показывать BMeet вокруг камеры", ru["texts"])


class Clicks(Base):
    def acts(self, state: str, *acts: str) -> list[dict]:
        lines = self.f.island(state, "--act", *acts)
        return lines[1:]   # (the state itself first, then one line per act)

    def test_a_click_opens_it_and_holds_esc_which_closes_it(self) -> None:
        click, esc = self.acts("meeting", "click", "escape")
        self.assertTrue(click["hit"])
        self.assertTrue(click["open"])
        self.assertTrue(click["escape_held"])   # (Esc is BMeet's while it is open, without the keyboard focus)
        self.assertGreater(click["frame"][3], 100)
        self.assertFalse(esc["open"])
        self.assertFalse(esc["escape_held"])    # (and the apps' again)
        self.assertEqual(esc["frame"][3], 47.5)

    def test_a_click_outside_goes_through_and_closes_it(self) -> None:
        click, outside = self.acts("speakers", "click", "outside")
        self.assertTrue(click["open"])
        self.assertFalse(outside["hit"])        # (outside the island's shape: not the island's click)
        self.assertFalse(outside["open"])
        self.assertFalse(outside["escape_held"])
        self.assertEqual(outside["state"], "speakers")

    def test_a_second_click_closes_it(self) -> None:
        first, second = self.acts("meeting", "click", "click")
        self.assertTrue(first["open"])
        self.assertFalse(second["open"])

    def test_hover_grows_it_a_little(self) -> None:
        state = self.f.island("meeting")[0]["frame"]
        hover, leave = self.acts("meeting", "hover", "leave")
        self.assertTrue(hover["hover"])
        self.assertGreater(hover["frame"][2], state[2])
        self.assertLess(hover["frame"][2], state[2] * 1.1)
        self.assertEqual(leave["frame"], state)
        self.assertJoinedToTheHousing(hover)

    def test_the_controls(self) -> None:
        for state, button, actions, still_open in (
                ("meeting", "pause", ["pause"], True), ("meeting-paused", "resume", ["resume"], True),
                ("meeting", "stop-meeting", ["stop-meeting"], False), ("listening", "cancel", ["cancel"], False),
                ("listening", "stop", ["stop"], False),
                ("saved", "open", ["open " + str(Path.home() / "Documents/Meetings/2026-11-28_1405-meeting.md")], False)):
            with self.subTest(state=state, button=button):
                _, pressed = self.acts(state, "click", "button:" + button)
                self.assertEqual(pressed["actions"], actions)
                self.assertEqual(pressed["open"], still_open)
        # a control that is not there
        self.assertEqual(self.f.run("--island-check", "saved", *THIS_MAC, "--act", "click", "button:pause", check=False).returncode, 2)


def meeting(state: str = "recording", started: float | None = None) -> dict:
    return {"meeting": state, "meeting_started": started if started is not None else time.time() - 65,
            "speakers_pending": []}


IDLE = {"meeting": "idle", "speakers_pending": []}


class Status(Base):
    """What BMeet makes of what it knows: the status bmeet-ctl gives, notifications, a stop's output."""

    def transcript(self, failed: bool = False) -> Path:
        path = self.f.root / "Meetings/2026-11-28_1405-meeting.md"
        path.parent.mkdir(exist_ok=True)
        info = "# Meeting\n\n## Meeting Info\n- **Transcribed:** as people talked (live)"
        if failed:   # (the line macos/bmeet-meeting leaves when the pass did not tell the voices apart)
            info += "\n- **Speaker pass:** the voices were not told apart: it crashed (SIGSEGV)"
        path.write_text(info + "\n\n## Transcript\n\n**You:** Hello.\n")
        return path

    def test_a_meeting_and_its_time(self) -> None:
        line = self.f.status(meeting())[0]
        self.assertEqual(line["state"], "meeting")
        self.assertRegex(line["texts"][0], r"^1:0\d$")
        self.assertEqual(self.f.status(meeting("paused"))[0]["state"], "meeting-paused")
        self.assertEqual(self.f.status(meeting(started=time.time() - 3725))[0]["texts"][0][:5], "1:02:")

    def test_an_in_person_meeting(self) -> None:
        folder = self.f.run_dir / "meeting-audio/2026-11-28_1405"
        folder.mkdir(parents=True)
        (folder / "meeting.json").write_text(json.dumps({"title": "In-person meeting", "started": 1, "in_person": True}))
        (self.f.run_dir / "meeting-toggle.started").write_text("2026-11-28_1405\n")
        self.assertEqual(self.f.status(meeting())[0]["state"], "in-person")
        self.assertEqual(self.f.status(meeting("paused"))[0]["state"], "in-person-paused")

    def test_the_speaker_pass_then_saved(self) -> None:
        file = str(self.transcript())
        during, after = self.f.status(meeting() | {"speakers_pending": [file]}, IDLE)
        self.assertEqual(during["state"], "meeting")   # (a meeting that runs comes first)
        pass_, saved = self.f.status(IDLE | {"speakers_pending": [file]}, IDLE)
        self.assertEqual(pass_["state"], "speakers")
        self.assertEqual(saved["state"], "saved")
        self.assertEqual(saved["texts"], ["Saved"])

    def test_the_speaker_pass_then_not_told_apart(self) -> None:
        file = str(self.transcript(failed=True))
        lines = self.f.status(IDLE | {"speakers_pending": [file]}, IDLE, more=("--act", "click"))
        self.assertEqual(lines[1]["state"], "failed")
        self.assertIn("Speakers not told apart", lines[2]["texts"])
        self.assertEqual(lines[2]["buttons"], ["open", "dismiss"])

    def test_a_failure_waits_until_it_is_looked_at(self) -> None:
        crash = {"title": "Meeting recording stopped unexpectedly", "body": "BMeet stops the meeting.", "ok": False}
        lines = self.f.status(meeting() | {"notify": [crash]}, IDLE, IDLE, more=("--act", "hover", "leave", "click", "escape"))
        recording, stopped, later, hover, leave, opened, closed = lines
        self.assertEqual(recording["state"], "meeting")    # (the meeting first, while it runs)
        self.assertEqual(stopped["state"], "failed")
        self.assertEqual(later["state"], "failed")         # (still there)
        self.assertEqual(leave["state"], "failed")         # (the mouse passing by is not looking)
        self.assertIn("Meeting recording stopped unexpectedly", opened["texts"])
        self.assertEqual(closed["state"], "idle")          # (opened and closed: looked at)

    def test_dismiss(self) -> None:
        crash = {"title": "Meeting NOT started", "body": "", "ok": False}
        lines = self.f.status(meeting() | {"notify": [crash]}, IDLE, more=("--act", "click", "button:dismiss"))
        self.assertEqual(lines[-1]["state"], "idle")
        self.assertFalse(lines[-1]["escape_held"])

    def test_notifications_that_are_not_the_islands(self) -> None:
        # one that says nothing failed, and a failure with no meeting anywhere near (a file's)
        fine = {"title": "Meeting transcript saved", "body": "", "ok": True}
        other = {"title": "Transcription failed", "body": "", "ok": False}
        self.assertEqual(self.f.status(meeting() | {"notify": [fine]}, IDLE)[-1]["state"], "idle")
        self.assertEqual(self.f.status(IDLE | {"notify": [other]})[-1]["state"], "idle")

    def test_a_stop_without_a_speaker_pass_is_saved(self) -> None:
        file = str(self.transcript())
        self.assertEqual(self.f.status(meeting(), IDLE | {"written": file})[-1]["state"], "saved")

    def test_a_stop_with_a_speaker_pass_shows_it_at_once(self) -> None:
        file = str(self.transcript())
        folder = self.f.run_dir / "meeting-audio/2026-11-28_1405"
        folder.mkdir(parents=True)
        (folder / "speaker-pass").write_text(file + "\n")   # (what bmeet-meeting marks the recording with)
        line = self.f.status(meeting(), IDLE | {"written": file, "speakers_pending": [file]})[-1]
        self.assertEqual(line["state"], "speakers")

    def test_a_new_meeting_ends_the_last_ones_saved(self) -> None:
        file = str(self.transcript())
        lines = self.f.status(IDLE | {"speakers_pending": [file]}, IDLE, meeting(), IDLE)
        self.assertEqual([line["state"] for line in lines], ["speakers", "saved", "meeting", "idle"])

    def test_with_the_island_off_nothing_shows(self) -> None:
        self.f.set("island", "off")
        line = self.f.status(meeting())[0]
        self.assertFalse(line["island"])
        self.assertEqual(line["state"], "idle")


def png_size(path: Path) -> tuple[int, int]:
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n", path
    return struct.unpack(">II", data[16:24])


class Pictures(Base):
    def test_each_state_and_a_sheet(self) -> None:
        out = self.f.root / "pictures"
        for shade in ("light", "dark"):
            self.f.run("--island-check", "all", *THIS_MAC, "--lang", "en", "--picture", str(out), "--appearance", shade)
        for state in STATES:
            for suffix in ("", "-open"):
                with self.subTest(state=state, suffix=suffix):
                    width, height = png_size(out / f"island-{state}{suffix}-en-light.png")
                    self.assertEqual(width, 1040)   # (520 points, at twice the size)
                    self.assertTrue((out / f"island-{state}{suffix}-en-dark.png").exists())
        sheet_width, sheet_height = png_size(out / "island-en-dark.png")
        self.assertEqual(sheet_width, 2 * (150 + 520))
        self.assertGreater(sheet_height, 2 * 20 * 60)

    def test_no_picture_without_a_housing(self) -> None:
        out = self.f.root / "pictures"
        self.f.run("--island-check", "meeting", *NO_HOUSING, "--picture", str(out))
        self.assertEqual(list(out.iterdir()), [])


class Options(Base):
    def test_unknown_or_misplaced_options_end_with_2(self) -> None:
        for args in (["--island-check", "nope"], ["--island-check"], ["--menu-check", "--open"], ["--open"],
                     ["--island-check", "all", "--act", "click"], ["--island-check", "meeting", "--act", "nope"],
                     ["--island-check", "status"], ["--island-check", "meeting", "--act"]):
            with self.subTest(args=args):
                self.assertEqual(self.f.run(*args, check=False).returncode, 2)

    def test_a_check_needs_folders_of_its_own(self) -> None:
        env = {k: v for k, v in self.f.env.items() if k not in ("XDG_CONFIG_HOME", "BMEET_RUNTIME_ROOT")}
        p = subprocess.run([str(BMEET), "--island-check", "meeting", *THIS_MAC], env=env, capture_output=True, text=True,
                           timeout=30)
        self.assertEqual(p.returncode, 2)
        self.assertEqual(p.stdout, "")

    def test_help(self) -> None:
        self.assertIn("--island-check", self.f.run("--help").stdout)


if __name__ == "__main__":
    unittest.main()
