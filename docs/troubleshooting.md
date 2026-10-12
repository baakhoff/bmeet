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

When you open an issue, include the output of `verify.sh` and
`journalctl --user -u voxtype -b -n 50`.
