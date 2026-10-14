#!/usr/bin/python3 -I
"""One bmeet-dictate dictation inside a private KWin (run by kwin-nested.sh; see test_dictate.py):
a test text field has the focus, bmeet-dictate runs with the fake engine, Meta+H twice. Writes
what happened to WORK/result.json.

Usage (inside kwin-nested.sh): nested-scenario.py WORK CASE
CASE: wayland (a Qt field on Wayland), x11 (a Qt field through Xwayland: no input method),
      leftover (a note of an earlier run that held the slot: given back at start-up), cancel,
      give-back (the same note, put back by `bmeet-dictate give-back` without the service)."""
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
DICTATE = HERE.parent / "bmeet-dictate"
work, case = Path(sys.argv[1]), sys.argv[2]
cfg = Path(os.environ["XDG_CONFIG_HOME"])
(cfg / "bmeet").mkdir(parents=True, exist_ok=True)
(cfg / "bmeet/live-dictation").write_text("on\n")
(work / "voxtype.toml").write_text('engine = "openvino"\n[openvino]\nlanguage = "ru"\n')
run = Path(os.environ["XDG_RUNTIME_DIR"]) / f"bmeet-test-{os.getpid()}"   # (a socket path has <= 107 bytes)
env = dict(os.environ, BMEET_RUNTIME_DIR=str(run), BMEET_STATE_DIR=str(work / "state-bmeet"),
           BMEET_DICTATE_ENGINE=str(HERE / "fake-engine"), BMEET_DICTATE_OUTPUT=f"file:{work / 'out.jsonl'}",
           BMEET_IM_LOG=str(work / "im.log"), VOXTYPE_CONFIG=str(work / "voxtype.toml"),
           FAKE_ENGINE_LOG=str(work / "engine.log"), QT_QPA_PLATFORM="wayland")
slot = work / "state-bmeet/input-method.json"


def kwin(prop):
    return subprocess.run(["qdbus6", "org.kde.KWin", "/VirtualKeyboard", f"org.kde.kwin.VirtualKeyboard.{prop}"],
                          capture_output=True, text=True).stdout.strip()


def kwinrc():
    return subprocess.run(["kreadconfig6", "--file", "kwinrc", "--group", "Wayland", "--key", "InputMethod"],
                          capture_output=True, text=True).stdout.strip()


def ctl(*args):
    out = subprocess.run([str(DICTATE), *args], capture_output=True, text=True, env=env, timeout=10).stdout
    return json.loads(out or "{}")


result = {"case": case, "before": {"input_method": kwinrc(), "enabled": kwin("enabled")}}
if case == "give-back":
    slot.parent.mkdir(parents=True, exist_ok=True)
    slot.write_text(json.dumps({"input_method": result["before"]["input_method"], "enabled": False}))
    subprocess.run(["kwriteconfig6", "--file", "kwinrc", "--group", "Wayland", "--key", "InputMethod",
                    str(run / "bmeet-im.desktop"), "--notify"])
    subprocess.run(["qdbus6", "org.kde.KWin", "/VirtualKeyboard", "org.freedesktop.DBus.Properties.Set",
                    "org.kde.kwin.VirtualKeyboard", "enabled", "true"])
    result["crashed"] = {"input_method": kwinrc(), "enabled": kwin("enabled")}
    gb = subprocess.run([str(DICTATE), "give-back"], env=env, capture_output=True, text=True)
    result["give_back"] = {"rc": gb.returncode, "out": gb.stdout.strip()}
    result["after"] = {"input_method": kwinrc(), "enabled": kwin("enabled"), "slot_file": slot.exists()}
    (work / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=1))
    sys.exit(0)
field_env = dict(os.environ)
if case == "x11":
    field_env.update(QT_QPA_PLATFORM="xcb", DISPLAY=os.environ.get("NESTED_DISPLAY", ""))
else:
    field_env["QT_QPA_PLATFORM"] = "wayland"
field = subprocess.Popen([str(HERE / "qt-field.py"), str(work / "field.log"), "12"], env=field_env)
time.sleep(1.5)
if case == "leftover":
    slot.parent.mkdir(parents=True, exist_ok=True)
    slot.write_text(json.dumps({"input_method": result["before"]["input_method"], "enabled": False}))
    subprocess.run(["kwriteconfig6", "--file", "kwinrc", "--group", "Wayland", "--key", "InputMethod",
                    str(run / "bmeet-im.desktop"), "--notify"])
    result["crashed"] = kwinrc()
service = subprocess.Popen([str(DICTATE), "serve"], env=env, stderr=open(work / "service.log", "w"))
for _ in range(50):
    if (run / "dictate.sock").exists():
        break
    time.sleep(0.1)
time.sleep(0.5)
result["at_start"] = {"input_method": kwinrc(), "slot_file": slot.exists()}
t0 = time.time()
result["start"] = ctl("toggle")
time.sleep(0.3)
result["during_slot"] = {"input_method": kwinrc(), "enabled": kwin("enabled"), "slot_file": slot.exists()}
time.sleep(1.8)
result["recording"] = ctl("status")
result["stop"] = ctl("cancel" if case == "cancel" else "toggle")
for _ in range(50):
    if ctl("status").get("state") == "idle":
        break
    time.sleep(0.1)
result["seconds"] = round(time.time() - t0, 2)
time.sleep(0.5)
result["after"] = {"input_method": kwinrc(), "enabled": kwin("enabled"), "slot_file": slot.exists(),
                   "status": ctl("status")}
ctl("quit")
service.wait(10)
field.wait(20)
read = lambda name: [json.loads(line) for line in (work / name).read_text().splitlines()] \
    if (work / name).exists() else []
result["field"] = read("field.log")
result["out"] = read("out.jsonl")
result["im"] = read("im.log")
result["engine"] = read("engine.log")
shutil.rmtree(run, ignore_errors=True)
(work / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=1))
