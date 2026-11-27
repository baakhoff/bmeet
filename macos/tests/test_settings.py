#!/usr/bin/env python3
"""Tests of BMeet.app's Settings window, its feature switches and its interface language, through
the check modes of BMeet's own executable (macos/Sources/BMeet/Checks.swift). macOS only.

Run: python3 -I macos/tests/test_settings.py -v
(BMeet comes from macos/.build/release: swift build -c release --package-path macos; without it
the tests that need it skip.)

Nothing is shown and nothing of yours is touched or started: every run of BMeet gets folders of
its own (XDG_CONFIG_HOME, BMEET_RUNTIME_ROOT, BMEET_LOGS, ...), and the control socket's runs get
a stand-in for BMeet's Python, so no dictation or meeting records and no text is pasted. Only
--hotkey-check registers shortcuts with macOS: ⌃⌥⇧⌘ with F13–F20, which nobody uses, for a
moment. Your BMeet keeps its own.

- The Settings window, off screen: every page in English and Russian fits a small screen and cuts
  no text short; every text has its Russian line.
- The menu: turning each feature off removes exactly its items and shortcuts, and turning it on
  brings them back; a meeting that runs can still be stopped; the menu bar icon.
- The Settings window's controls, worked as a click would: the files they write (languages and the
  dictation language, shortcuts as HotKeys.swift reads them back, the interface language,
  remembered voices with their questions).
- Shortcuts registered with macOS again, at once, when a switch or the hotkeys file changes.
- The control socket: the "settings" op opens the right page; dictate and meeting refuse what is
  off and follow the files while BMeet runs; unload-after ends the speech engine.
- The translations in the bundle (macos/app/lproj.sh on a scratch bundle), and the strings
  checker (macos/tools/check-strings.py) failing on a text without its Russian line.
"""
from __future__ import annotations

import json
import os
import plistlib
import re
import shutil
import signal
import socket
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
CHECKER = MACOS / "tools/check-strings.py"
CYRILLIC = re.compile("[а-яА-Я]")
# The menu items and shortcuts of each feature: what turning it off must remove, and only that
FEATURE_ITEMS = {
    "dictation": ({"Dictate", "Dictate to Clipboard", "Dictation Language"}, {"dictate", "dictate-clipboard", "language"}),
    "meetings": ({"Start Meeting"}, {"meeting"}),
    "in-person-meetings": ({"Start In-Person Meeting"}, {"meeting-in-person"}),
    "file-transcription": ({"Transcribe a File…"}, set()),
}
RARE = {"dictate": "ctrl+option+shift+cmd+f13", "dictate-clipboard": "ctrl+option+shift+cmd+f14",
        "language": "ctrl+option+shift+cmd+f15", "meeting": "ctrl+option+shift+cmd+f16",
        "meeting-in-person": "ctrl+option+shift+cmd+f17"}


def strings(path: Path) -> dict[str, str]:
    """A .strings table read by the checker's own parser."""
    sys.path.insert(0, str(CHECKER.parent))
    import importlib.util
    spec = importlib.util.spec_from_file_location("check_strings", CHECKER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.parse_strings(path.read_text(encoding="utf-8"))


def titles(menu: list[dict]) -> set[str]:
    """The titles of a menu's items, without the separators (not those of submenus)."""
    return {i["title"] for i in menu if i["title"] != "-"}


class Folders:
    """A HOME-like set of folders for one test: BMeet's settings, runtime, logs, models, cache."""

    def __init__(self) -> None:
        # (short: the control socket's path has a 104-byte limit)
        self.root = Path(tempfile.mkdtemp(prefix="bm-"))
        self.config = self.root / "cfg/bmeet"
        self.config.mkdir(parents=True)
        self.voices = self.root / "data/bmeet/voices"
        self.env = dict(os.environ, BMEET_RUNTIME_ROOT=str(self.root / "run"), XDG_CONFIG_HOME=str(self.root / "cfg"),
                        BMEET_LOGS=str(self.root / "logs"), NPU_MODELS_DIR=str(self.root / "models"),
                        XDG_CACHE_HOME=str(self.root / "cache"), XDG_DATA_HOME=str(self.root / "data"),
                        BMEET_VOICES_DIR=str(self.voices), BMEET_SRC=str(REPO), BMEET_NO_NOTIFY="1")
        for k in ("BMEET_PYTHON",):
            self.env.pop(k, None)

    def set(self, name: str, value: str) -> None:
        (self.config / name).write_text(value + "\n")

    def get(self, name: str) -> str | None:
        try:
            return (self.config / name).read_text().strip()
        except OSError:
            return None

    def all_models(self) -> None:
        """Every model the About page lists, as empty folders (the page at its tallest)."""
        for p in ("whisperkit/openai_whisper-large-v3-v20240930_626MB", "whisperkit/openai_whisper-small_216MB",
                  "gigaam-v3-ctc-ru/model.mlmodelc", "speaker-diarization/nemotron-3-diarization/encoder.mlmodelc",
                  "speaker-diarization/titanet.mlmodelc"):
            (self.root / "models" / p).mkdir(parents=True, exist_ok=True)

    def run(self, *args: str, check: bool = True, timeout: float = 60) -> subprocess.CompletedProcess:
        p = subprocess.run([str(BMEET), *args], env=self.env, cwd=self.root, capture_output=True, text=True,
                           timeout=timeout)
        if check and p.returncode != 0:
            raise AssertionError(f"BMeet {' '.join(args)}: exit {p.returncode}\n{p.stdout}\n{p.stderr}")
        return p

    def json(self, *args: str) -> dict:
        return json.loads(self.run(*args).stdout)

    def menu(self, *args: str) -> dict:
        return self.json("--menu-check", *args)

    def act(self, *acts: str) -> dict:
        return self.json("--settings-act", *acts)

    def close(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


@unittest.skipUnless(sys.platform == "darwin" and BMEET.exists(), "macOS, with BMeet built (swift build -c release --package-path macos)")
class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.f = Folders()
        self.addCleanup(self.f.close)


class Pages(Base):
    def check(self, lang: str) -> list[dict]:
        self.f.all_models()
        p = self.f.run("--settings-check", "all", "--lang", lang, check=False)
        lines = [json.loads(line) for line in p.stdout.splitlines()]
        for line in lines:
            with self.subTest(page=line["page"], variant=line["variant"], lang=lang):
                self.assertLessEqual(line["height"], 600, "taller than a 13-inch screen allows")
                self.assertEqual(line["cut"], [], "texts cut short")
                self.assertFalse(line["too_wide"])
                self.assertEqual(line["untranslated"], [], "texts without their Russian line")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual({line["page"] for line in lines}, {"general", "features", "languages", "shortcuts", "about"})
        return lines

    def test_every_page_fits_in_english(self) -> None:
        lines = self.check("en")
        general = next(line for line in lines if line["page"] == "general")
        self.assertEqual(general["toolbar"], ["General", "Features", "Languages", "Shortcuts", "About"])
        self.assertIn("Start BMeet at login", general["texts"])
        about = [line for line in lines if line["page"] == "about"]
        self.assertTrue(any("BMeet 10.10.10 is available." in line["texts"] for line in about))
        self.assertTrue(any("Update…" in line["texts"] for line in about))
        self.assertIn("OpenMDW-1.1", about[0]["texts"])   # (the models installed, with their licences)

    def test_every_page_fits_in_russian(self) -> None:
        lines = self.check("ru")
        general = next(line for line in lines if line["page"] == "general")
        self.assertEqual(general["toolbar"], ["Основные", "Функции", "Языки", "Сочетания клавиш", "О программе"])
        self.assertEqual(general["title"], "Основные")
        for line in lines:
            # every text but names (BMeet, the models, the licences, keys) is in Russian
            english = [t for t in line["texts"] if re.search("[a-zA-Z]{4,}", t) and not CYRILLIC.search(t)
                       and not re.fullmatch(r"(BMeet( for macOS)?|English|github\.com/baakhoff/bmeet|"
                                            r"OpenMDW-1\.1|CC-BY-4\.0|MIT|\S+ \(\w+\)|[⌃⌥⇧⌘A-Z0-9]+)", t)]
            self.assertEqual(english, [], f"English on the {line['page']} page in Russian")

    def test_without_models_the_about_page_lists_only_the_software(self) -> None:
        texts = json.loads(self.f.run("--settings-check", "about", "--lang", "en").stdout.splitlines()[0])["texts"]
        self.assertNotIn("MIT", [t for t in texts if "GigaAM" in t])
        self.assertFalse(any("GigaAM" in t for t in texts))
        self.assertTrue(any("WhisperKit" in t for t in texts))

    def test_a_page_that_does_not_exist(self) -> None:
        self.assertEqual(self.f.run("--settings-check", "nope", check=False).returncode, 2)


class Menu(Base):
    def test_each_feature_off_removes_exactly_its_items_and_shortcuts(self) -> None:
        base = self.f.menu("--lang", "en")
        everything = titles(base["menu"][2:])   # (after the state line and the line under it)
        self.assertEqual(set(base["shortcuts"]), {"dictate", "dictate-clipboard", "language", "meeting", "meeting-in-person"})
        self.assertEqual(base["icon"], "mic")
        self.assertEqual([i["title"] for i in base["menu"][:2]], ["Ready", "EN · on the Neural Engine"])
        for feature, (items, keys) in FEATURE_ITEMS.items():
            with self.subTest(feature=feature):
                self.f.set(feature, "off")
                try:
                    off = self.f.menu("--lang", "en")
                    self.assertEqual(everything - titles(off["menu"][2:]), items)
                    self.assertLessEqual(titles(off["menu"][2:]), everything)
                    self.assertEqual(set(base["shortcuts"]) - set(off["shortcuts"]), keys)
                    self.assertLessEqual(set(off["shortcuts"]), set(base["shortcuts"]))
                    # (the dictation language goes with dictation)
                    self.assertEqual(off["menu"][1]["title"], "On the Neural Engine" if feature == "dictation"
                                     else "EN · on the Neural Engine")
                finally:
                    self.f.set(feature, "on")
                self.assertEqual(self.f.menu("--lang", "en"), base)

    def test_no_double_separators_whatever_is_off(self) -> None:
        for off in ([], ["dictation"], ["meetings", "in-person-meetings"], ["dictation", "meetings", "in-person-meetings"],
                    list(FEATURE_ITEMS)):
            for feature in FEATURE_ITEMS:
                self.f.set(feature, "off" if feature in off else "on")
            menu = [i["title"] for i in self.f.menu("--lang", "en")["menu"]]
            with self.subTest(off=off):
                self.assertFalse(any(a == b == "-" for a, b in zip(menu, menu[1:])), menu)

    def test_everything_off(self) -> None:
        for feature in FEATURE_ITEMS:
            self.f.set(feature, "off")
        menu = self.f.menu("--lang", "en")
        self.assertEqual(menu["menu"][0]["title"], "Every feature is off")
        self.assertEqual(menu["shortcuts"], {})
        self.assertEqual(menu["icon"], "waveform")
        self.assertIn("Settings…", titles(menu["menu"]))
        self.assertIn("Recent Transcripts", titles(menu["menu"]))   # (the history stays)

    def test_the_icon_with_one_feature_on(self) -> None:
        for on, icon in (("meetings", "mic"), ("in-person-meetings", "mic"), ("dictation", "mic"), ("file-transcription", "waveform")):
            for feature in FEATURE_ITEMS:
                self.f.set(feature, "on" if feature == on else "off")
            with self.subTest(on=on):
                self.assertEqual(self.f.menu("--lang", "en")["icon"], icon)

    def test_a_running_meeting_can_be_stopped_with_meetings_off(self) -> None:
        self.f.set("meetings", "off")
        self.f.set("in-person-meetings", "off")
        status = self.f.root / "status.json"
        status.write_text(json.dumps({"meeting": "recording", "meeting_title": "Weekly", "meeting_started": time.time() - 65}))
        menu = self.f.menu("--lang", "en", "--status", str(status))
        self.assertIn("Stop Meeting", titles(menu["menu"]))
        self.assertIn("Pause Meeting", titles(menu["menu"]))
        self.assertEqual(menu["menu"][0]["title"], "Weekly")
        self.assertEqual(menu["icon"], "record.circle.fill")
        self.assertNotIn("meeting", menu["shortcuts"])

    def test_the_settings_item_and_about(self) -> None:
        menu = self.f.menu("--lang", "en")["menu"]
        settings = next(i for i in menu if i["title"] == "Settings…")
        self.assertEqual(settings["key"], "⌘,")
        self.assertIn("About BMeet", titles(menu))


class InterfaceLanguage(Base):
    def test_the_setting_switches_the_menu(self) -> None:
        self.f.set("interface-language", "ru")
        menu = self.f.menu()
        self.assertEqual(menu["lang"], "ru")
        self.assertTrue({"Диктовать", "Начать встречу", "Настройки…", "О программе BMeet", "Выйти из BMeet"} <= titles(menu["menu"]))
        self.assertEqual(menu["untranslated"], [])
        languages = next(i for i in menu["menu"] if i["title"] == "Язык диктовки")
        self.assertEqual(languages["submenu"][0]["title"], "Английский (en)")   # (language names in Russian too)
        self.f.set("interface-language", "en")
        menu = self.f.menu()
        self.assertEqual(menu["lang"], "en")
        self.assertIn("Settings…", titles(menu["menu"]))

    def test_system_is_one_of_the_two(self) -> None:
        # (which one depends on this Mac's languages: only that it is one BMeet has)
        for value in ("system", "de", ""):
            self.f.set("interface-language", value)
            self.assertIn(self.f.menu()["lang"], ("en", "ru"))

    def test_the_general_page_switches_it_and_the_window_follows(self) -> None:
        out = self.f.act("interface:ru")
        self.assertEqual(self.f.get("interface-language"), "ru")
        self.assertEqual(out["lang"], "ru")
        self.assertEqual(out["title"], "Основные")
        self.assertEqual(out["toolbar"][1], "Функции")
        self.assertIn("Открывать BMeet при входе в систему", out["texts"])
        out = self.f.act("interface:system")
        self.assertEqual(self.f.get("interface-language"), "system")
        out = self.f.act("interface:en")
        self.assertEqual(out["title"], "General")


class Controls(Base):
    def test_feature_switches_write_their_files(self) -> None:
        self.f.act("feature:dictation", "feature:file-transcription")
        self.assertEqual(self.f.get("dictation"), "off")
        self.assertEqual(self.f.get("file-transcription"), "off")
        self.assertIsNone(self.f.get("meetings"))   # (a missing file: on)
        self.f.act("feature:dictation")
        self.assertEqual(self.f.get("dictation"), "on")
        self.f.act("live", "speakers")
        self.assertEqual(self.f.get("live-dictation"), "off")
        self.assertEqual(self.f.get("speakers"), "off")

    def test_the_words_while_you_speak_go_with_dictation(self) -> None:
        texts = self.f.act("page:features")["texts"]
        self.assertIn("Show the words while you speak", texts)
        self.f.set("dictation", "off")
        texts = self.f.act("page:features")["texts"]
        self.assertNotIn("Show the words while you speak", texts)
        self.assertNotIn("Pasting:", self.f.act("page:general")["texts"])

    def test_general_page(self) -> None:
        self.f.act("unload:0", "login", "update-check")
        self.assertEqual(self.f.get("unload-after"), "0")
        self.assertEqual(self.f.get("start-at-login"), "off")
        self.assertEqual(self.f.get("update-check"), "off")
        self.f.act("unload:60")
        self.assertEqual(self.f.get("unload-after"), "60")
        self.f.set("unload-after", "10")   # (by hand: shown as it is)
        self.assertEqual(self.f.act("page:general")["unload"], "10 min")
        self.f.set("unload-after", "90")   # (outside 0-60: the nearest end, as the engines read it)
        self.assertEqual(self.f.act("page:general")["unload"], "60 minutes")
        self.f.set("unload-after", "-3")
        self.assertEqual(self.f.act("page:general")["unload"], "Right after use")

    def test_remembered_voices_ask_and_off_forgets_them(self) -> None:
        self.f.act("answer:no", "voices")
        self.assertIsNone(self.f.get("remember-voices"))   # (not turned on without a yes)
        self.f.act("answer:yes", "voices")
        self.assertEqual(self.f.get("remember-voices"), "on")
        self.f.voices.mkdir(parents=True)
        (self.f.voices / "Viktor.json").write_text("{}")
        self.f.act("answer:no", "voices")
        self.assertEqual(self.f.get("remember-voices"), "on")
        self.assertTrue((self.f.voices / "Viktor.json").exists())
        self.f.act("answer:yes", "voices")
        self.assertEqual(self.f.get("remember-voices"), "off")
        self.assertFalse(self.f.voices.exists())


class Languages(Base):
    def test_at_least_one_stays_on(self) -> None:
        self.f.set("languages", "en")
        self.f.set("language", "en")
        self.f.act("language:en")
        self.assertEqual(self.f.get("languages"), "en")
        self.assertEqual(self.f.get("language"), "en")

    def test_the_dictation_language_follows_when_it_is_turned_off(self) -> None:
        self.f.set("languages", "ru en")
        self.f.set("language", "ru")
        self.f.act("language:ru")
        self.assertEqual(self.f.get("languages"), "en")
        self.assertEqual(self.f.get("language"), "en")

    def test_one_turned_on_goes_last_and_can_be_chosen(self) -> None:
        self.f.set("languages", "en ru")
        self.f.set("language", "ru")
        out = self.f.act("search:germ", "language:de", "dictation-language:de")
        self.assertEqual(out["rows"], ["de"])
        self.assertEqual(self.f.get("languages"), "en ru de")
        self.assertEqual(self.f.get("language"), "de")
        self.assertEqual(self.f.act("page:languages")["rows"][:3], ["en", "ru", "de"])   # (the ones on first)

    def test_the_list_is_whispers_with_names_in_the_interface_language(self) -> None:
        self.f.set("languages", "en ru")
        out = self.f.act("page:languages")
        self.assertEqual(out["rows"][:2], ["en", "ru"])
        ru = self.f.json("--settings-act", "page:languages", "--lang", "ru")
        self.assertIn("Английский", ru["texts"])
        self.assertIn("язык диктовки", self.f.json("--settings-act", "page:languages", "--lang", "ru")["texts"])
        self.assertIn("Afrikaans", self.f.act("search:afr")["texts"])
        self.assertEqual(self.f.act("search:haw")["rows"], ["haw"])
        # a language that is not Whisper's cannot be found
        self.assertEqual(self.f.run("--settings-act", "language:xx", check=False).returncode, 2)


class Shortcuts(Base):
    def test_a_recorded_shortcut_is_read_back_the_same(self) -> None:
        out = self.f.act("shortcut:dictate=ctrl+option+j", "shortcut:meeting=cmd+shift+f5")
        self.assertEqual(out["message"], "")
        text = (self.f.config / "hotkeys").read_text()
        self.assertIn("dictate = ctrl+option+j\n", text)
        self.assertIn("meeting = cmd+shift+f5\n", text)
        self.assertNotIn("dictate-clipboard", text)   # (only what differs from the defaults)
        menu = self.f.menu("--lang", "en")
        self.assertEqual(menu["shortcuts"]["dictate"], "⌃⌥J")
        self.assertEqual(menu["shortcuts"]["meeting"], "⇧⌘F5")
        self.assertEqual(next(i for i in menu["menu"] if i["title"] == "Dictate")["key"], "⌃⌥J")
        self.assertEqual(next(i for i in menu["menu"] if i["title"] == "Start Meeting")["key"], "⇧⌘F5")

    def test_delete_turns_one_off_and_esc_keeps_it(self) -> None:
        self.f.act("shortcut:language=esc")
        self.assertFalse((self.f.config / "hotkeys").exists())
        self.f.act("shortcut:language=delete")
        self.assertIn("language = none", (self.f.config / "hotkeys").read_text())
        menu = self.f.menu("--lang", "en")
        self.assertNotIn("language", menu["shortcuts"])
        languages = next(i for i in menu["menu"] if i["title"] == "Dictation Language")
        self.assertEqual(languages["submenu"][-1]["key"], "")   # (Next Language, without its shortcut)

    def test_keys_that_cannot_be_a_shortcut(self) -> None:
        out = self.f.act("shortcut:dictate=h")
        self.assertTrue(out["recording"])            # (still waiting for keys)
        self.assertIn("⌃, ⌥ or ⌘", out["message"])
        self.assertFalse((self.f.config / "hotkeys").exists())
        out = self.f.act("shortcut:dictate=ctrl+option+m")   # (the meeting's)
        self.assertIn("already the shortcut", out["message"])
        self.assertFalse((self.f.config / "hotkeys").exists())
        self.f.act("shortcut:dictate=f6")            # (an F key alone is fine)
        self.assertIn("dictate = f6", (self.f.config / "hotkeys").read_text())

    def test_a_feature_that_is_off_keeps_its_shortcuts_greyed(self) -> None:
        self.f.set("dictation", "off")
        out = self.f.act("shortcut:dictate=ctrl+option+j")
        self.assertFalse((self.f.config / "hotkeys").exists())
        self.assertEqual(out["texts"].count("Off on the Features page"), 3)

    def test_restore_defaults(self) -> None:
        (self.f.config / "hotkeys").write_text("dictate = ctrl+alt+k\n# a note\nmeeting = none\n")
        menu = self.f.menu("--lang", "en")   # (a file written by hand: alt, comments)
        self.assertEqual(menu["shortcuts"]["dictate"], "⌃⌥K")
        self.assertNotIn("meeting", menu["shortcuts"])
        self.f.act("restore-shortcuts")
        self.assertEqual(self.f.menu("--lang", "en")["shortcuts"]["dictate"], "⌃⌥H")
        self.assertFalse(any("=" in line for line in (self.f.config / "hotkeys").read_text().splitlines()
                             if not line.startswith("#")))


class Registration(Base):
    def test_shortcuts_are_registered_again_at_once(self) -> None:
        (self.f.config / "hotkeys").write_text("".join(f"{a} = {c}\n" for a, c in RARE.items()))
        p = self.f.run("--hotkey-check", check=False)
        steps = {s["step"]: s for s in map(json.loads, p.stdout.splitlines())}
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual({k: v["registered"] for k, v in steps.items()},
                         {"start": 5, "dictation off": 2, "meetings off": 1, "dictation on": 4, "meetings on": 5,
                          "dictate changed": 5, "paused": 0, "resumed": 5, "end": 0})
        self.assertEqual(set(steps["dictation off"]["shortcuts"]), {"meeting", "meeting-in-person"})
        self.assertEqual(steps["dictate changed"]["shortcuts"]["dictate"], "⌃⌥⇧⌘F20")
        self.assertEqual(steps["start"]["shortcuts"]["dictate"], "⌃⌥⇧⌘F13")

    def test_it_never_registers_shortcuts_people_use(self) -> None:
        p = self.f.run("--hotkey-check", check=False)   # (the defaults: ⌃⌥H, the running BMeet's)
        self.assertEqual(p.returncode, 2)
        self.assertEqual(p.stdout, "")


class Options(Base):
    def test_unknown_options_end_with_2(self) -> None:
        for args in (["--nope"], ["--lang", "ru"], ["--settings-check"], ["--menu-check", "--lang", "de"],
                     ["--menu-check", "--hotkey-check"], ["-x"]):
            with self.subTest(args=args):
                self.assertEqual(self.f.run(*args, check=False).returncode, 2)

    def test_a_check_needs_folders_of_its_own(self) -> None:
        env = {k: v for k, v in self.f.env.items() if k not in ("XDG_CONFIG_HOME", "BMEET_RUNTIME_ROOT")}
        p = subprocess.run([str(BMEET), "--menu-check"], env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(p.returncode, 2)
        self.assertEqual(p.stdout, "")
        p = self.f.run("--control-check", check=False, timeout=30)   # (without a stand-in for Python)
        self.assertEqual(p.returncode, 2)

    def test_help(self) -> None:
        self.assertIn("--settings-check", self.f.run("--help").stdout)


# A stand-in for BMeet's Python (BMEET_PYTHON): it writes down what BMeet ran. As
# `npu-transcribe --dictate` it is a speech engine that hears nothing: each stop ends the
# dictation with no text, so nothing is pasted, and it says when its stdin closes (BMeet ended it).
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


class ControlSocket(Base):
    def setUp(self) -> None:
        super().setUp()
        self.log = self.f.root / "ran.log"
        self.log.touch()
        python = self.f.root / "python"
        python.write_text(STAND_IN.format(python=sys.executable, log=str(self.log)))
        python.chmod(0o755)
        self.f.env["BMEET_PYTHON"] = str(python)
        self.proc = subprocess.Popen([str(BMEET), "--control-check"], env=self.f.env, cwd=self.f.root,
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
        """The answer, and the line BMeet printed about it."""
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(10)
            s.connect(self.sock)
            s.sendall(json.dumps(req).encode() + b"\n")
            answer = json.loads(s.makefile().readline())
        return answer, json.loads(self.proc.stdout.readline())

    def ran(self) -> str:
        return self.log.read_text()

    def wait_for(self, check, timeout: float = 8.0) -> bool:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if check():
                return True
            time.sleep(0.1)
        return False

    def test_settings_opens_the_page(self) -> None:
        for page, shown in (("languages", "languages"), ("about", "about"), ("nope", "general"), (None, "general"),
                            ("shortcuts", "shortcuts")):
            answer, line = self.send({"op": "settings"} | ({"page": page} if page else {}))
            with self.subTest(page=page):
                self.assertEqual(answer, {"ok": True})
                self.assertEqual(line["page"], shown)
                self.assertFalse(line["shown"])   # (a check shows nothing)

    def test_dictate_refuses_while_dictation_is_off_and_follows_the_file(self) -> None:
        self.f.set("dictation", "off")
        self.assertTrue(self.wait_for(lambda: self.send({"op": "dictate"})[0].get("ok") is False))
        answer, line = self.send({"op": "dictate"})
        self.assertIn("dictation is off", answer["error"])
        self.assertEqual(line["panel_shown"], 0)
        self.assertNotIn("--dictate", self.ran())
        self.f.set("dictation", "on")   # (back on, without a restart)
        answer, line = self.send({"op": "dictate"})
        self.assertEqual(answer, {"ok": True})
        self.assertTrue(self.wait_for(lambda: "--dictate" in self.ran()))
        self.assertEqual(line["dictation"], "recording")
        self.assertEqual(line["panel_shown"], 1)   # (counted, never shown)
        self.send({"op": "cancel"})

    def test_turning_dictation_off_ends_one_that_runs(self) -> None:
        self.assertEqual(self.send({"op": "dictate"})[0], {"ok": True})
        self.assertTrue(self.wait_for(lambda: "--dictate" in self.ran()))
        self.f.set("dictation", "off")
        self.assertTrue(self.wait_for(lambda: "engine ended" in self.ran()), self.ran())
        self.assertEqual(self.send({"op": "status"})[0]["dictation"], "idle")

    def test_unload_after_ends_the_engine(self) -> None:
        self.f.set("unload-after", "5")
        self.send({"op": "dictate"})
        self.assertTrue(self.wait_for(lambda: "--dictate" in self.ran()))
        self.send({"op": "dictate"})   # (stop: the engine hears nothing)
        self.assertTrue(self.wait_for(lambda: self.send({"op": "status"})[0]["dictation"] == "idle"))
        time.sleep(1.5)
        self.assertNotIn("engine ended", self.ran())   # (kept for 5 minutes)
        self.f.set("unload-after", "0")                 # (now: right after use)
        self.assertTrue(self.wait_for(lambda: "engine ended" in self.ran()), self.ran())
        self.send({"op": "dictate"})
        self.assertTrue(self.wait_for(lambda: self.ran().count("--dictate") == 2))
        self.send({"op": "dictate"})
        self.assertTrue(self.wait_for(lambda: self.ran().count("engine ended") == 2), self.ran())

    def test_meetings_that_are_off_are_not_started_but_stopped(self) -> None:
        self.f.set("meetings", "off")
        self.f.set("in-person-meetings", "off")
        self.assertTrue(self.wait_for(lambda: self.send({"op": "meeting", "action": "start"})[0].get("ok") is False))
        for req in ({"op": "meeting", "action": "start"}, {"op": "meeting", "action": "toggle"},
                    {"op": "meeting", "action": "start", "in_person": True}):
            answer, _ = self.send(req)
            with self.subTest(req=req):
                self.assertFalse(answer["ok"])
                self.assertIn("off", answer["error"])
        time.sleep(0.5)
        self.assertNotIn("bmeet-meeting", self.ran())
        run = self.f.root / "run/bmeet"
        (run / "meeting_state").write_text("recording\n")
        self.assertEqual(self.send({"op": "meeting", "action": "stop"})[0], {"ok": True})
        self.assertEqual(self.send({"op": "meeting", "action": "toggle"})[0], {"ok": True})
        self.assertTrue(self.wait_for(lambda: "bmeet-meeting stop" in self.ran() and "bmeet-meeting toggle" in self.ran()))
        (run / "meeting_state").write_text("idle\n")
        self.f.set("meetings", "on")
        self.assertTrue(self.wait_for(lambda: self.send({"op": "meeting", "action": "start"})[0] == {"ok": True}))
        self.assertTrue(self.wait_for(lambda: "bmeet-meeting start" in self.ran()))
        self.assertNotIn("--in-person", self.ran())
        self.assertFalse(self.send({"op": "meeting", "action": "start", "in_person": True})[0]["ok"])

    def test_unknown_op(self) -> None:
        self.assertEqual(self.send({"op": "nope"})[0], {"ok": False, "error": "unknown op"})


@unittest.skipUnless(sys.platform == "darwin", "macOS only (plutil)")
class Translations(unittest.TestCase):
    def test_the_bundle_gets_every_table(self) -> None:
        with tempfile.TemporaryDirectory(prefix="bm-") as tmp:
            resources = Path(tmp) / "BMeet.app/Contents/Resources"
            subprocess.run(["bash", str(MACOS / "app/lproj.sh"), str(resources)], check=True)
            for name in ("Localizable", "ServicesMenu", "InfoPlist"):
                built = plistlib.loads((resources / f"ru.lproj/{name}.strings").read_bytes())   # (binary: plutil read it)
                self.assertEqual(built, strings(MACOS / f"app/ru.lproj/{name}.strings"))
            self.assertEqual(plistlib.loads((resources / "en.lproj/Localizable.strings").read_bytes()), {})
        info = plistlib.loads((MACOS / "app/Info.plist").read_bytes())
        self.assertEqual(info["CFBundleLocalizations"], ["en", "ru"])
        self.assertEqual(info["CFBundleDevelopmentRegion"], "en")

    def test_the_installer_puts_them_in_before_signing(self) -> None:
        text = (MACOS / "install.sh").read_text()
        self.assertLess(text.index('macos/app/lproj.sh" "$stage_app/Contents/Resources"'),
                        text.index('codesign --force --sign "$identity" --identifier io.github.baakhoff.bmeet'))


class Checker(unittest.TestCase):
    def run_checker(self, root: Path) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-I", str(root / "macos/tools/check-strings.py")],
                              capture_output=True, text=True, timeout=60)

    def copy(self) -> Path:
        tmp = Path(tempfile.mkdtemp(prefix="bm-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        for part in ("macos/tools", "macos/app", "macos/Sources/BMeet"):
            shutil.copytree(REPO / part, tmp / part)
        return tmp

    def test_the_tables_match_the_source(self) -> None:
        p = self.run_checker(REPO)
        self.assertEqual(p.returncode, 0, p.stdout)

    def test_a_text_without_its_russian_line_fails(self) -> None:
        tmp = self.copy()
        table = tmp / "macos/app/ru.lproj/Localizable.strings"
        table.write_text(table.read_text().replace('"Dictate" = "Диктовать";\n', ""))
        p = self.run_checker(tmp)
        self.assertEqual(p.returncode, 1)
        self.assertIn("no translation for the text at", p.stdout)
        self.assertIn(": Dictate", p.stdout)

    def test_a_line_without_its_text_fails(self) -> None:
        tmp = self.copy()
        table = tmp / "macos/app/ru.lproj/Localizable.strings"
        table.write_text(table.read_text() + '"Gone" = "Нет";\n"Ёлка" = "ёлка";\n')
        p = self.run_checker(tmp)
        self.assertEqual(p.returncode, 1)
        self.assertIn("not in the source: Gone", p.stdout)
        self.assertIn("«ё»", p.stdout)

    def test_placeholders_and_services(self) -> None:
        tmp = self.copy()
        table = tmp / "macos/app/ru.lproj/Localizable.strings"
        table.write_text(table.read_text().replace('"Version %@" = "Версия %@";', '"Version %@" = "Версия";'))
        services = tmp / "macos/app/ru.lproj/ServicesMenu.strings"
        services.write_text(services.read_text().replace('"Open in BMeet" = "Открыть в BMeet";\n', ""))
        p = self.run_checker(tmp)
        self.assertEqual(p.returncode, 1)
        self.assertIn("other placeholders than its text: Version %@", p.stdout)
        self.assertIn("no title for the service Open in BMeet", p.stdout)


if __name__ == "__main__":
    unittest.main()
