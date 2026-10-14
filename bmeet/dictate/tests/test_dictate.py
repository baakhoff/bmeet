#!/usr/bin/python3 -I
"""Tests of bmeet-dictate (BMeet's own dictation) and bmeet-im, with a fake engine (tests/fake-engine)
instead of the speech model: no NPU, no microphone, nothing typed or copied (BMEET_DICTATE_OUTPUT
writes what would be pasted to a file).

- Offscreen (QT_QPA_PLATFORM=offscreen, "panel" mode): the panel, paste and clipboard, cancel,
  the engine stopping, the commands. Their D-Bus is a dead address, so nothing can reach your
  KWin, and their config, runtime and state folders are temporary.
- In a private KWin (tests/kwin-nested.sh: its own Wayland socket, D-Bus and kwinrc; skipped
  without kwin_wayland): "on" mode, where the text goes into the text field through KWin's
  input-method slot, which must be given back afterwards: a Qt field on Wayland, one through
  Xwayland (no input method there: the panel and paste), a cancel, and the note left by a run
  that ended while it held the slot.

Run: python3 -I bmeet/dictate/tests/test_dictate.py -v
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
DICTATE = HERE.parent / "bmeet-dictate"
CTL = HERE.parents[1] / "bmeet-ctl"
FAKE_VOXTYPE = HERE.parents[1] / "app/tests/fake-voxtype"
FAKE = HERE / "fake-engine"
STAND_IN = "/usr/share/applications/org.kde.plasma.keyboard.desktop"   # (only a name in the private kwinrc)


def load_dictate():
    import importlib.machinery
    import importlib.util
    loader = importlib.machinery.SourceFileLoader("bmeet_dictate", str(DICTATE))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class Helpers(unittest.TestCase):
    def setUp(self):
        self.bd = load_dictate()

    def test_rest_of(self):
        self.assertEqual(self.bd.rest_of("One. Two three.", ""), "One. Two three.")
        self.assertEqual(self.bd.rest_of("One. Two three.", "One."), "Two three.")
        self.assertEqual(self.bd.rest_of("One. Two three.", "One. Two three."), "")
        self.assertEqual(self.bd.rest_of("One. Two.", "Something else"), "")   # (never paste twice)

    def test_shown_text(self):
        self.assertEqual(self.bd.shown_text("Final <b>.", "draft"),
                         "Final &lt;b&gt;. <span style='color:#9aa4b1'>draft</span>")
        long = self.bd.shown_text("word " * 200, "", chars=50)
        self.assertTrue(long.startswith("…") and len(long) <= 52)

    def test_mode_and_language(self):
        tmp = HERE / ".work-helpers"
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir()
        try:
            self.bd.MODE_FILE = tmp / "live-dictation"
            self.assertEqual(self.bd.mode(), "off")
            for text, want in (("on\n", "on"), ("panel", "panel"), ("off", "off"), ("yes", "off"), ("", "off")):
                self.bd.MODE_FILE.write_text(text)
                self.assertEqual(self.bd.mode(), want, text)
            self.bd.VOXTYPE_CONFIG = tmp / "config.toml"
            self.assertEqual(self.bd.dictation_language(), "en")
            self.bd.VOXTYPE_CONFIG.write_text('engine = "openvino"\n[openvino]\nlanguage = "RU"\n')
            self.assertEqual(self.bd.dictation_language(), "ru")
            self.bd.VOXTYPE_CONFIG.write_text('[whisper]\nlanguage = "auto,de"\n')
            self.assertEqual(self.bd.dictation_language(), "en")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class Service(unittest.TestCase):
    """The service offscreen, "panel" mode, the fake engine."""

    def setUp(self):
        self.work = HERE / ".work-dictate"
        shutil.rmtree(self.work, ignore_errors=True)
        (self.work / "config/bmeet").mkdir(parents=True)
        (self.work / "config/bmeet/live-dictation").write_text("panel\n")
        (self.work / "voxtype.toml").write_text('engine = "openvino"\n[openvino]\nlanguage = "ru"\n')
        self.run_dir = Path(os.environ.get("XDG_RUNTIME_DIR") or "/tmp") / f"bmeet-test-{os.getpid()}"
        self.env = dict(os.environ, QT_QPA_PLATFORM="offscreen", XDG_CONFIG_HOME=str(self.work / "config"),
                        BMEET_RUNTIME_DIR=str(self.run_dir), BMEET_STATE_DIR=str(self.work / "state"),
                        BMEET_DICTATE_ENGINE=str(FAKE), BMEET_DICTATE_OUTPUT=f"file:{self.work / 'out.jsonl'}",
                        VOXTYPE_CONFIG=str(self.work / "voxtype.toml"), FAKE_ENGINE_LOG=str(self.work / "engine.log"),
                        DBUS_SESSION_BUS_ADDRESS="unix:path=/nonexistent/bmeet-test-bus")
        self.env.pop("WAYLAND_DISPLAY", None)
        self.service = subprocess.Popen([sys.executable, "-I", str(DICTATE), "serve"], env=self.env,
                                        stderr=subprocess.PIPE, text=True)
        for _ in range(100):
            if (self.run_dir / "dictate.sock").exists():
                break
            time.sleep(0.05)
        self.assertTrue((self.run_dir / "dictate.sock").exists(), "the service did not start")

    def tearDown(self):
        try:
            self.ctl("quit")
            self.service.wait(10)
        except Exception:
            self.service.kill()
        self.service.stderr.close()
        shutil.rmtree(self.work, ignore_errors=True)
        shutil.rmtree(self.run_dir, ignore_errors=True)

    def ctl(self, *args) -> dict:
        out = subprocess.run([sys.executable, "-I", str(DICTATE), *args], env=self.env, capture_output=True,
                             text=True, timeout=10)
        return json.loads(out.stdout or "{}")

    def wait_for(self, state: str, timeout: float = 5.0) -> dict:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if (st := self.ctl("status")).get("state") == state:
                return st
            time.sleep(0.05)
        self.fail(f"not {state}: {st}")

    def out(self) -> list[dict]:
        path = self.work / "out.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_dictation_shows_the_text_and_pastes_it_once(self):
        self.assertEqual(self.ctl("status")["state"], "idle")
        st = self.ctl("toggle")
        self.assertEqual(st["state"], "recording")
        self.assertEqual((self.run_dir / "dictation").read_text().strip(), "recording")
        time.sleep(1.4)
        st = self.ctl("status")
        self.assertTrue(st["panel"])
        self.assertIn("Второй челлендж, он чисто поисковый.", st["panel_text"])
        self.assertIn("color:#9aa4b1", st["panel_text"])     # the draft, in grey
        self.assertFalse(st["input_method"])                  # ("panel" mode: KWin is left alone)
        time.sleep(0.4)
        self.ctl("toggle")
        st = self.wait_for("idle")
        self.assertFalse(st["panel"])
        text = "Второй челлендж, он чисто поисковый. Это, собственно, как делать поиск быстро."
        self.assertEqual(self.out(), [{"text": text, "clipboard": False}])
        self.assertEqual(st["last"]["delivered"], "file")
        self.assertFalse((self.work / "state/input-method.json").exists())
        engine = [json.loads(line) for line in (self.work / "engine.log").read_text().splitlines()]
        self.assertEqual(engine, [{"start": {"language": "ru"}}, {"stop": {}}])

    def test_clipboard_only(self):
        self.ctl("start", "--clipboard")
        st = self.ctl("status")
        self.assertTrue(st["panel"])                          # (right away: no field to wait for)
        time.sleep(1.0)
        self.ctl("stop")
        self.wait_for("idle")
        self.assertEqual(self.out(), [{"text": "Второй челлендж, он чисто поисковый.", "clipboard": True}])

    def test_cancel_and_nothing_said(self):
        self.ctl("toggle")
        time.sleep(0.5)
        self.assertEqual(self.ctl("cancel")["state"], "idle")
        time.sleep(0.3)
        self.ctl("toggle")
        self.ctl("toggle")                                    # (stopped before anything was heard)
        self.wait_for("idle")
        self.assertEqual(self.out(), [])

    def test_engine_gone_mid_dictation(self):
        self.ctl("toggle")
        time.sleep(0.5)
        subprocess.run(["pkill", "-f", f"{FAKE} --dictate"], check=False)
        st = self.wait_for("idle")
        self.assertFalse(st["panel"])
        self.ctl("toggle")                                    # (a new engine is started)
        time.sleep(1.0)
        self.ctl("toggle")
        self.wait_for("idle")
        self.assertEqual(len(self.out()), 1)

    def bmeet_ctl(self, *args) -> subprocess.CompletedProcess:
        """bmeet-ctl as Meta+H runs it, with a fake voxtype first in PATH."""
        bin_dir = self.work / "bin"
        bin_dir.mkdir(exist_ok=True)
        if not (bin_dir / "voxtype").exists():
            (bin_dir / "voxtype").symlink_to(FAKE_VOXTYPE)
        env = dict(self.env, PATH=f"{bin_dir}:{os.environ['PATH']}", FAKE_VOXTYPE_LOG=str(self.work / "voxtype.log"),
                   BMEET_RUN_DIR=str(self.work / "voxtype-run"), BMEET_MEETING_DB=str(self.work / "none.db"))
        return subprocess.run([sys.executable, "-I", str(CTL), *args], env=env, capture_output=True, text=True, timeout=15)

    def test_bmeet_ctl_goes_to_bmeet_dictate_when_it_is_on(self):
        self.assertEqual(self.bmeet_ctl("dictate").returncode, 0)
        status = json.loads(self.bmeet_ctl("status").stdout)
        self.assertEqual((status["dictation"], status["live_dictation"]), ("recording", "panel"))
        self.bmeet_ctl("cancel")
        self.wait_for("idle")
        self.assertFalse((self.work / "voxtype.log").exists())        # voxtype was not asked
        (self.work / "config/bmeet/live-dictation").write_text("off\n")
        self.bmeet_ctl("dictate", "--clipboard")
        self.bmeet_ctl("cancel")
        calls = [json.loads(line) for line in (self.work / "voxtype.log").read_text().splitlines()]
        self.assertEqual(calls, [["record", "toggle", "--clipboard"], ["record", "cancel"]])
        self.assertEqual(self.ctl("status")["state"], "idle")

    def test_bmeet_ctl_starts_bmeet_dictate(self):
        self.ctl("quit")
        self.service.wait(10)
        self.env["BMEET_DICTATE_START"] = f"{sys.executable} -I {DICTATE} serve >/dev/null 2>&1 &"
        self.assertEqual(self.bmeet_ctl("dictate").returncode, 0)
        self.assertEqual(self.ctl("status")["state"], "recording")
        self.bmeet_ctl("cancel")

    def test_unknown_command(self):
        self.assertEqual(subprocess.run([sys.executable, "-I", str(DICTATE), "status-please"], env=self.env,
                                        capture_output=True).returncode, 2)
        import socket
        with socket.socket(socket.AF_UNIX) as s:
            s.connect(str(self.run_dir / "dictate.sock"))
            s.sendall(b"status-please\n")
            self.assertIn("error", json.loads(s.makefile().readline()))


@unittest.skipUnless(shutil.which("kwin_wayland") and shutil.which("dbus-run-session")
                     and shutil.which("kwriteconfig6") and shutil.which("qdbus6"), "needs KWin (kwin_wayland)")
class InPrivateKWin(unittest.TestCase):
    """"on" mode against a private KWin whose input method is set to a stand-in, switched off."""

    def scenario(self, case: str) -> dict:
        work = HERE / f".work-nested-{case}"
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir()
        env = dict(os.environ, NESTED_KWINRC=f"[Wayland]\\nInputMethod={STAND_IN}\\nVirtualKeyboardEnabled=false\\n")
        if case == "x11":
            env["NESTED_X"] = "1"
        subprocess.run([str(HERE / "kwin-nested.sh"), str(work / "kwin"), "--", str(HERE / "nested-scenario.py"),
                        str(work), case], env=env, timeout=120, capture_output=True)
        try:
            return json.loads((work / "result.json").read_text())
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def given_back(self, r: dict) -> None:
        self.assertEqual(r["before"], {"input_method": STAND_IN, "enabled": "false"})
        self.assertTrue(r["during_slot"]["input_method"].endswith("/bmeet-im.desktop"))
        self.assertEqual((r["during_slot"]["enabled"], r["during_slot"]["slot_file"]), ("true", True))
        self.assertEqual(r["after"]["input_method"], STAND_IN)
        self.assertEqual((r["after"]["enabled"], r["after"]["slot_file"]), ("false", False))

    TEXT = "Второй челлендж, он чисто поисковый. Это, собственно, как делать поиск быстро."

    def test_wayland_field_gets_the_text(self):
        r = self.scenario("wayland")
        self.given_back(r)
        self.assertTrue(r["recording"]["field"])
        self.assertFalse(r["recording"]["panel"])
        ims = [e for e in r["field"] if e["ev"] == "im"]
        self.assertTrue(any(e["preedit"] for e in ims))                          # the draft, in the field
        self.assertEqual("".join(e["commit"] for e in ims), self.TEXT)          # committed, nothing twice
        self.assertEqual([e["text"] for e in r["field"] if e["ev"] == "end"], [self.TEXT])
        self.assertEqual(r["out"], [])                                           # nothing pasted
        self.assertEqual(r["after"]["status"]["last"]["delivered"], "into the field")
        self.assertLess(r["seconds"], 4)

    def test_x11_window_gets_the_panel_and_a_paste(self):
        r = self.scenario("x11")
        self.given_back(r)
        self.assertFalse(r["recording"]["field"])
        self.assertTrue(r["recording"]["panel"])
        self.assertEqual([e for e in r["field"] if e["ev"] == "im"], [])
        self.assertEqual(r["out"], [{"text": self.TEXT, "clipboard": False}])

    def test_cancel_gives_the_slot_back(self):
        r = self.scenario("cancel")
        self.given_back(r)
        self.assertEqual(r["out"], [])
        end = [e["text"] for e in r["field"] if e["ev"] == "end"]
        self.assertEqual(end, ["Второй челлендж, он чисто поисковый."])          # final words stay, the draft goes

    def test_give_back_command(self):
        r = self.scenario("give-back")
        self.assertTrue(r["crashed"]["input_method"].endswith("/bmeet-im.desktop"))
        self.assertEqual(r["crashed"]["enabled"], "true")
        self.assertEqual(r["give_back"]["rc"], 0)
        self.assertEqual(r["after"], {"input_method": STAND_IN, "enabled": "false", "slot_file": False})

    def test_leftover_note_is_given_back_at_start(self):
        r = self.scenario("leftover")
        self.assertTrue(r["crashed"].endswith("/bmeet-im.desktop"))
        self.assertEqual(r["at_start"], {"input_method": STAND_IN, "slot_file": False})
        self.given_back(r)


if __name__ == "__main__":
    unittest.main()
