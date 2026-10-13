# Troubleshooting

Start with the health check. It changes nothing except a temporary WAV file:

```bash
bash ~/.local/share/bmeet/src/voxtype/verify.sh
```

| Symptom | What to check |
|---|---|
| Meta+H does nothing | **Daemon:** `voxtype status`; if it says "stopped", run `systemctl --user status voxtype`. **Shortcuts:** they are registered from launchers, so log out and in once, or set them in System Settings → Keyboard → Shortcuts → Add New → Application → "Voxtype: Dictate". |
| Text is in the clipboard but not pasted | `systemctl --user status ydotool`; `ls -l $XDG_RUNTIME_DIR/.ydotool_socket`; `getfacl /dev/uinput` (your user needs `rw`) |
| Nothing is pasted in a terminal | Expected: Ctrl+V isn't paste there. Use **Meta+Alt+H** (clipboard only), then Ctrl+Shift+V. |
| "openvino-whisper feature not compiled" | `/usr/bin/voxtype` is the package wrapper again. Re-run the installer, or `sudo ln -sfn /usr/lib/voxtype/voxtype-onnx-avx2 /usr/bin/voxtype`. |
| "Failed to load OpenVINO GenAI library" | `ls /opt/intel/openvino_genai/runtime/lib/intel64/libopenvino_genai_c.so`. If it's missing, re-run the installer. |
| Slow, CPU busy while transcribing | It fell back to the CPU: `journalctl --user -u voxtype -b \| grep "fell back"`; `ls -l /dev/accel/accel0`; `dpkg -l intel-level-zero-npu` |
| No `/dev/accel/accel0` | No NPU, or the `intel_vpu` kernel module isn't loaded (`lsmod \| grep intel_vpu`). Core Ultra (Meteor Lake, Lunar Lake, Arrow Lake) CPUs have one. |
| Permission denied on `/dev/accel/accel0` | `sudo usermod -aG render $USER`, then log out and in |
| First dictation after install or upgrade takes ~60 s | One-time NPU compile. The installer normally does it ahead of time. |
| Dictation comes out in the wrong language | Meta+Shift+H, the widget's language chip, or `voxtype-lang de`. The engine has no auto-detection. |
| I want other languages on Meta+Shift+H | Edit `~/.config/bmeet/languages` (e.g. `en de fr`), or re-run the installer with `--languages "en de fr"`. |
| Meeting transcript missing after stopping | Stopping waits for the last chunk (up to a few minutes on a slow CPU). Afterwards: `voxtype meeting list`, then `voxtype meeting export latest -o FILE.md`. |
| Widget shows "Backend error" | `~/.local/bin/bmeet-ctl status` must print JSON. Re-run the installer if it doesn't. |
| Widget not in the panel | Right-click the panel → Add or Manage Widgets → "BMeet". After an upgrade: `systemctl --user restart plasma-plasmashell`. |
| BMeet window only flashes in the task bar | Wayland focus-stealing prevention when it's opened from another program. Click the task bar entry. |
| npu-transcribe: "model not found … turbo" | It was installed with `--no-turbo`. Re-run the installer without it, or use `--model small`. |

## Meeting speakers

The speaker pass (You / Person 1 / Person 2 …) runs as the systemd user unit
`bmeet-speakers-YYYY-MM-DD_HHMM` after each meeting; the BMeet app shows "Telling the
speakers apart…" until it is done.

| Symptom | What to check |
|---|---|
| The transcript keeps *You* / *Remote* | `head -1 ~/.config/bmeet/speakers` must say `on` (off with `--no-speakers`). Its log: `journalctl --user -u 'bmeet-speakers-*' -u bmeet-meeting-audio -b`. A desktop notification "Meeting speakers failed" names the reason. |
| Two people are one *Person* | Their voices were too alike (or one barely spoke). Lower the threshold for the pass, e.g. `systemctl --user set-environment NPU_SPEAKER_THRESHOLD=0.3` (the default is 0.4), and it applies from the next meeting. Undo: `systemctl --user unset-environment NPU_SPEAKER_THRESHOLD`. |
| One person is split in two | Rare since 0.3.1, which folds fragments and near-identical voices together (docs/npu-transcribe.md). Otherwise raise the threshold, e.g. `NPU_SPEAKER_THRESHOLD=0.55`, or name both labels the same in *Name speakers…*. Files: `npu-transcribe --num-speakers N` fixes the count. |
| Someone who said only a few words is under another person | A voice with under 15 s of speech (and under 5 % of all) counts as a fragment and joins the voice it sounds most like. |
| Your own words are missing | The echo filter cuts words where your microphone is less than 2x louder than the echo of the system audio. It can happen if you talk very quietly while the other side is loud. Headphones avoid the echo altogether. |
| Their words appear under *You* | Usually loud speakers next to a sensitive microphone. Headphones help. |
| Russian has English terms mangled ("Perfomance") | GigaAM, used for Russian, only knows Russian. For a talk full of English terms: `npu-transcribe --engine whisper FILE`. Meetings always use GigaAM for Russian; reinstall with `--no-gigaam` to keep Whisper. |
| A "-speakers.md" file appeared next to the transcript | You edited the transcript while the pass ran, so your edit was kept and the speaker version was saved next to it. |
| Someone got the wrong name | Rename them in that meeting with *Name speakers…*. Their fingerprint from that meeting then moves to the right name. Or forget the name in *Remembered voices…*. |
| A remembered person is not recognised | Their voice sounded different, for example through another microphone or connection. Name them again in that meeting: up to 10 meetings per person are kept, and their mean is compared. |
| You were muted in Meet or Slack, and your words are still there | Load BMeet's Chrome extension in the profile you take calls in (README, *Chrome extension*); `verify.sh` shows where it is loaded. After an upgrade, press its reload button in `chrome://extensions`. Calls in desktop apps (Zoom, Teams) are not seen. If the meeting ran without it, there is nothing to correct afterwards. |
| Your words are missing although you were not muted | The transcript's *Microphone off* line says how much was left out. Look at `call-mic.jsonl` while a meeting records (`$(cat $XDG_RUNTIME_DIR/bmeet/recording)/call-mic.jsonl`): `"live": false` while you talk means the extension did not see your sound. Please open an issue with that file's lines. Removing the extension restores the old behaviour. |
| In person, everyone is *Person 1, 2…* and nobody is *You* | Your voice is learned from online meetings with remembered voices on (`verify.sh` shows how many). Until then, write *You* for yourself in *Name speakers…*: later in-person meetings recognise you. |
| In person, everything is under *You* | Telling speakers apart is off, or only one voice was found. |
| *Name speakers…* is greyed out | The meeting is still recording, its speaker pass is running, or the transcript has no speakers (voxtype's *You* / *Remote* version). |

When you open an issue, include the output of `verify.sh` and
`journalctl --user -u voxtype -b -n 50`.
