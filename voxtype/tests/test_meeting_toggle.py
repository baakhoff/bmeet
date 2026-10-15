#!/usr/bin/env python3
"""Tests of voxtype-meeting-toggle: the real script, with stand-ins for voxtype, systemd
(systemd-run, systemctl), the clock (date) and the desktop (notify-send), in a throwaway
home and runtime folder. Nothing is recorded or started, nothing outside the temp folder.

Run: python3 -I voxtype/tests/test_meeting_toggle.py -v
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOGGLE = HERE.parent / "bin/voxtype-meeting-toggle"
CTL = HERE.parent.parent / "bmeet/bmeet-ctl"
NOW = 1792056927  # 2026-10-15 11:35:27 in Europe/Berlin (TZ is set below)

# Each stand-in logs its arguments (one JSON list per line) to $STUBS_LOG.
STUBS = {
    # voxtype: the meeting state the toggle follows, its index of meetings, an export it can save
    "voxtype": r'''
st="$XDG_RUNTIME_DIR/voxtype/meeting_state"; mkdir -p "${st%/*}"
case "$1 $2" in
    "meeting start") echo recording > "$st"
        python3 -c 'import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
con.execute("CREATE TABLE IF NOT EXISTS meetings (id TEXT PRIMARY KEY, title TEXT, started_at INTEGER, "
            "ended_at INTEGER, duration_secs INTEGER, status TEXT)")
n = con.execute("SELECT count(*) FROM meetings").fetchone()[0]
con.execute("INSERT INTO meetings VALUES (?, ?, ?, NULL, NULL, ?)", (f"m{n + 1}", sys.argv[2], int(sys.argv[3]), "active"))
con.commit()' "$BMEET_MEETING_DB" "$4" "$FAKE_NOW" ;;
    "meeting stop") echo idle > "$st"
        python3 -c 'import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
con.execute("UPDATE meetings SET ended_at = ?, status = ? WHERE ended_at IS NULL", (int(sys.argv[2]), "completed"))
con.commit()' "$BMEET_MEETING_DB" "$FAKE_NOW" ;;
    "meeting export") for a in "$@"; do [ "$prev" = --output ] && echo "# voxtype's own transcript" > "$a"; prev=$a; done ;;
esac''',
    # systemd-run: a unit "runs" until the test ends it; the recorder leaves a part behind
    "systemd-run": r'''
unit=""; dir=""; for a in "$@"; do case "$a" in --unit=*) unit=${a#--unit=} ;; esac; dir=$a; done
case "$unit" in
    bmeet-meeting-audio) touch "$dir/part-1-mic.wav"; touch "$STUBS_UNITS/$unit.service" ;;
    bmeet-speakers-*) touch "$STUBS_UNITS/$unit.service" ;;
esac''',
    "systemctl": r'''
unit="${@: -1}"
case " $* " in
    *" is-active "*) state=inactive; [ -e "$STUBS_UNITS/$unit" ] && state=active
                     case " $* " in *" --quiet "*) ;; *) echo "$state" ;; esac
                     [ "$state" = active ] ;;
    *" stop "*) case "$unit" in bmeet-speakers-*) ;; *) rm -f "$STUBS_UNITS/$unit" ;; esac ;;
esac''',
    "date": r'''exec /usr/bin/date -d "@$FAKE_NOW" "$@"''',
    "notify-send": "",
    "pw-record": "",
}


class Toggle(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="toggle-"))
        stubs, home = self.tmp / "stubs", self.tmp / "home"
        for d in (stubs, home / ".local/bin", self.tmp / "run", self.tmp / "config/bmeet",
                  self.tmp / "units", self.tmp / "Meetings"):
            d.mkdir(parents=True)
        for name, body in STUBS.items():
            f = stubs / name
            f.write_text('#!/bin/bash\nprintf "%s\\n" "$(python3 -c \'import json,sys; print(json.dumps(sys.argv[1:]))\' '
                         f'{name} "$@")" >> "$STUBS_LOG"\n{body}\n')
            f.chmod(0o755)
        for name in ("bmeet-meeting-audio", "npu-transcribe"):  # (only checked to be there)
            (home / ".local/bin" / name).write_text("#!/bin/sh\nexit 0\n")
            (home / ".local/bin" / name).chmod(0o755)
        (self.tmp / "config/bmeet/speakers").write_text("on\n")
        self.env = {"PATH": f"{stubs}:/usr/bin:/bin", "HOME": str(home), "TZ": "Europe/Berlin",
                    "XDG_RUNTIME_DIR": str(self.tmp / "run"), "XDG_CONFIG_HOME": str(self.tmp / "config"),
                    "VOXTYPE_MEETING_DIR": str(self.tmp / "Meetings"), "STUBS_LOG": str(self.tmp / "calls.log"),
                    "STUBS_UNITS": str(self.tmp / "units"), "FAKE_NOW": str(NOW),
                    "BMEET_MEETING_DB": str(self.tmp / "index.db")}
        self.audio = self.tmp / "run/bmeet/meeting-audio"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def toggle(self, *args: str, at: int = NOW) -> str:
        run = subprocess.run(["bash", str(TOGGLE), *args], env=dict(self.env, FAKE_NOW=str(at)),
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(run.returncode, 0, run.stderr)
        return run.stdout

    def calls(self, name: str) -> list[list[str]]:
        try:
            lines = (self.tmp / "calls.log").read_text().splitlines()
        except OSError:
            return []
        return [c[1:] for c in map(json.loads, lines) if c[0] == name]

    def units(self, prefix: str) -> list[list[str]]:
        return [c for c in self.calls("systemd-run") if f"--unit={prefix}" in " ".join(c)]

    def end_pass(self, unit: str) -> None:
        """The speaker pass `unit` is done: its ExecStopPost runs, then it is gone."""
        call = next(c for c in self.units(unit))
        post = next(c.split("=", 1)[1] for c in call if c.startswith("ExecStopPost="))
        subprocess.run(shlex.split(post), check=True)
        (self.tmp / "units" / f"{unit}.service").unlink()

    def test_one_meeting(self):
        self.toggle("start")
        rec = self.audio / "2026-10-15_1135"
        self.assertTrue((rec / "part-1-mic.wav").exists())
        self.assertEqual(json.loads((rec / "meeting.json").read_text())["in_person"], False)
        self.assertEqual([c[-1] for c in self.units("bmeet-live")], [str(rec)])
        out = self.toggle("stop", at=NOW + 600)
        md = self.tmp / "Meetings/2026-10-15_1135-meeting.md"
        self.assertEqual(out.strip(), str(md))
        self.assertTrue(md.exists())
        (call,) = self.units("bmeet-speakers-2026-10-15_1135")
        self.assertIn(f"ExecStopPost=/usr/bin/rm -rf {rec}", call)
        self.assertEqual(call[-5:], ["--meeting", str(rec), "--output", str(md), "--notify"])
        self.assertEqual((rec / "speaker-pass").read_text(), f"{md}\n")  # (which meeting is busy)
        self.assertFalse((self.tmp / "run/voxtype/meeting-toggle.started").exists())

    def test_two_meetings_in_one_minute(self):
        """2026-10-15: a meeting stopped and an in-person one started at once. Both were
        recorded into the minute's folder, and when the first one's speaker pass ended it
        deleted the second one's recording: no speakers, no live transcript for it."""
        self.toggle("start")
        self.toggle("stop", at=NOW + 11)                          # its pass starts ...
        self.toggle("start", "--in-person", at=NOW + 12)          # ... and the next one right away
        first, second = self.audio / "2026-10-15_1135", self.audio / "2026-10-15_1135-2"
        self.assertTrue((second / "part-1-mic.wav").exists())
        self.assertEqual(json.loads((second / "meeting.json").read_text())["in_person"], True)
        self.assertEqual(json.loads((first / "meeting.json").read_text())["in_person"], False)
        self.assertEqual([c[-1] for c in self.units("bmeet-live")], [str(first), str(second)])
        # what the window shows meanwhile: this meeting's live transcript, the first one's pass
        (second / "live.md").write_text("# live\n")
        st = json.loads(subprocess.run([sys.executable, "-I", str(CTL), "status"], env=self.env,
                                       capture_output=True, text=True, check=True).stdout)
        self.assertEqual(st["meeting_live"], str(second / "live.md"))
        self.assertEqual(st["speakers_pending"], [str(self.tmp / "Meetings/2026-10-15_1135-meeting.md")])
        self.end_pass("bmeet-speakers-2026-10-15_1135")         # the first pass ends
        self.assertFalse(first.exists())
        self.assertTrue((second / "part-1-mic.wav").exists())    # the second recording is still there
        self.toggle("stop", at=NOW + 2500)
        md = self.tmp / f"Meetings/2026-10-15_1135-meeting-{NOW + 2500}.md"
        self.assertTrue(md.exists())
        (call,) = self.units("bmeet-speakers-2026-10-15_1135-2")
        self.assertEqual(call[-5:], ["--meeting", str(second), "--output", str(md), "--notify"])
        self.assertEqual((second / "speaker-pass").read_text(), f"{md}\n")
        self.assertIn(f"ExecStopPost=/usr/bin/rm -rf {second}", call)

    def test_same_title_twice(self):
        """voxtype names a meeting's storage folder after its date and title: two normal
        meetings in one minute would share it (the second overwrote the first one's
        transcript there, and deleting one deleted the other's)."""
        self.toggle("start")                                      # (all within 11:35)
        self.toggle("stop", at=NOW + 10)
        self.toggle("start", at=NOW + 15)
        self.toggle("stop", at=NOW + 20)
        self.toggle("start", "--in-person", at=NOW + 25)
        titles = [c[c.index("--title") + 1] for c in self.calls("voxtype") if c[:2] == ["meeting", "start"]]
        self.assertEqual(titles, ["Meeting 2026-10-15 11:35", "Meeting 2026-10-15 11:35 (2)",
                                  "In-person meeting 2026-10-15 11:35"])
        self.assertEqual(json.loads((self.audio / "2026-10-15_1135-3/meeting.json").read_text())["title"],
                         "In-person meeting 2026-10-15 11:35")

    def test_a_minute_with_a_pass_still_running(self):
        """The folder is gone already, but its unit is still stopping: a name of its own too."""
        self.toggle("start")
        self.toggle("stop", at=NOW + 5)
        shutil.rmtree(self.audio / "2026-10-15_1135")
        self.toggle("start", at=NOW + 6)
        self.assertTrue((self.audio / "2026-10-15_1135-2").is_dir())
        self.assertEqual((self.tmp / "run/voxtype/meeting-toggle.started").read_text().strip(),
                         "2026-10-15_1135-2")


if __name__ == "__main__":
    unittest.main()
