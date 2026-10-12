# Dictation and meetings: how the voxtype setup works

[voxtype](https://github.com/peteonrails/voxtype) 1.1.0 does the dictation and meeting
recording. BMeet installs it so that Whisper runs on the Intel NPU through
OpenVINO GenAI 2026.4.1. This page explains the choices behind that setup.

- **System part:** [`system/install.sh`](../system/install.sh).
- **Per-user part:** [`voxtype/user-setup.sh`](../voxtype/user-setup.sh).
- **Default config:** [`voxtype/config.toml`](../voxtype/config.toml).

## Downloads (pinned)

| Artifact | Source | Check |
|---|---|---|
| `voxtype_1.1.0-1_amd64.deb` (375 MB) | [voxtype release v1.1.0](https://github.com/peteonrails/voxtype/releases/tag/v1.1.0) | sha256 `36d1bd3c…387b69` + upstream GPG signature (key `9CCF7915B750CAE8B095ED1AA3FC9F33FD209279`, "Voxtype Release Signing"). The installed `voxtype-onnx-avx2` is checked against the signed `SHA256SUMS.txt` |
| `openvino_genai_ubuntu26_2026.4.1.0_x86_64.tar.gz` (134 MiB, 388 MB unpacked) | [storage.openvinotoolkit.org](https://storage.openvinotoolkit.org/repositories/openvino_genai/packages/2026.4.1/linux/) | sha256 `af724fc5…138ba6` (= Intel's `.sha256`) |
| `linux-npu-driver-v1.38.0…-ubuntu2604.tar.gz` (only if no NPU driver is installed) | [intel/linux-npu-driver v1.38.0](https://github.com/intel/linux-npu-driver/releases/tag/v1.38.0) | sha256 of the tarball and both `.deb`s + Intel's GPG signature (key `EA267657A608300C296B8F8AD52C9665A4077678`) |
| `gtcrn_simple.onnx` (meeting echo cancellation) | [sherpa-onnx speech-enhancement-models](https://github.com/k2-fsa/sherpa-onnx/releases/tag/speech-enhancement-models) | sha256 `e77603ac…e1b534` (upstream publishes none) |
| `OpenVINO/whisper-small-int8-ov`, `OpenVINO/whisper-large-v3-turbo-int8-ov` | Hugging Face | pinned commit (`5b831719…`, `b568445d…`) |

**Where checks run:**
- Downloads are fetched as you into `~/.cache/bmeet/downloads`.
- The root script first copies them into root-owned scratch space under `/var/tmp`, then checks and uses only those copies. That way, nothing running as you can swap a file between the check and its use as root.
- The root script reads the downloads *as you*, so a symlink in that folder can't make root copy a file you couldn't read yourself.
- Signatures are checked with `gpgv` against the two public keys in [`system/keys/`](../system/keys). No keyserver is contacted, and no key is imported anywhere.
- A cached download with the wrong hash is deleted, and the installer asks you to run it again.

## What the system part changes

1. **apt packages**, installed with `--no-install-recommends` and `--no-remove`:
   - **Packages:** voxtype, ydotool, `libgtk4-layer-shell0`, plus the libraries the tools need.
   - **What `--no-remove` does:** apt aborts instead of removing anything.
   - **Waiting for apt:** if `unattended-upgrades` is busy, apt waits up to 10 minutes for the lock.
   - **Why `wtype` is skipped:** it is a voxtype Recommends, but it can't work on KWin (no virtual-keyboard protocol).
   - **Install record:** `/var/lib/bmeet/preexisting` notes what was already installed, so `uninstall.sh` removes only what it added.
2. **Binary variant:**
   - **The problem:** the voxtype package installs every build under `/usr/lib/voxtype/`, plus a `/usr/bin/voxtype` wrapper. On a CPU without AVX-512 that wrapper picks `voxtype-avx2` (whisper.cpp only). The OpenVINO engine is compiled only into the ONNX builds.
   - **What the installer does:** `/usr/bin/voxtype` becomes a symlink to `voxtype-onnx-avx2`, the same thing upstream's `voxtype setup variant --to voxtype-onnx-avx2` does. Before that, `dpkg-divert` moves the package's wrapper aside, so a later package upgrade can't silently put it back.
   - **Don't run `voxtype setup gpu --enable` afterwards:** it switches to the Vulkan whisper.cpp build, which has no OpenVINO.
3. **OpenVINO GenAI C/C++ SDK** in `/opt/intel/openvino_genai_ubuntu26_2026.4.1.0_x86_64`, with the symlink `/opt/intel/openvino_genai`:
   - **Why it's needed:** voxtype `dlopen()`s `libopenvino_genai_c.so`, which the pip wheel doesn't contain.
   - **Self-contained:** no `ldconfig` and no `LD_LIBRARY_PATH`. voxtype finds it through `openvino_dir`.
   - **GPU plugin left out on purpose:**
     - voxtype's fallback chain is hard-coded NPU → GPU → CPU (`device_candidates()` in `src/transcribe/openvino_whisper.rs`).
     - Without `libopenvino_intel_gpu_plugin.so`, an NPU start-up failure goes straight to the CPU, so dictation never wakes the iGPU.
     - Tested: with `device = "GPU"`, voxtype "fell back to 'CPU'" and still returned correct text.
4. **`/dev/uinput`:**
   - The udev rule `70-voxtype-uinput.rules` tags it `uaccess`. That is an ACL for whoever sits at the active local seat, so no `input` group is needed.
   - **Trade-off:** any process you run can create a virtual keyboard.
5. **ydotoold:**
   - A drop-in makes it keyboard-only (`--mouse-off`).
   - Ubuntu's package enables it for *every* user session. That is turned off (`systemctl --global disable`), and voxtype's own unit (`Wants=ydotool.service`) starts it for you.
   - Its socket is `$XDG_RUNTIME_DIR/.ydotool_socket`, mode 0600.

## What the per-user part changes

- **Config:**
  - `~/.config/voxtype/config.toml` is rendered from [`config.toml`](../voxtype/config.toml) with your model path and first language.
  - A config not written by BMeet is backed up first. Ours is kept on re-runs (`--reset-config` replaces it).
- **Languages:**
  - `~/.config/bmeet/languages` holds the languages that Meta+Shift+H, the widget and the app cycle through.
  - The default is English plus your desktop language.
- **Commands and shortcuts:**
  - Helper commands go in `~/.local/bin`: `voxtype-meeting-toggle` and `voxtype-lang`.
  - Launchers go in `~/.local/share/applications`. Their `X-KDE-Shortcuts=` lines are linked into `~/.local/share/kglobalaccel/`, the same mechanism Konsole and Spectacle use for default shortcuts.
- **Meeting echo-cancel model:** installed sha256-checked, and `~/Documents/Meetings` is created.
- **One-time NPU compile** of whisper-small (~60 s; cached in `~/.cache/voxtype/openvino/`). It is triggered with a synthetic espeak-ng clip, so no audio is recorded.
- **`voxtype.service`** is enabled with the drop-in `~/.config/systemd/user/voxtype.service.d/10-npu-setup.conf`:
  - `RUST_LOG=voxtype=warn,warn` puts NPU fallback warnings in the journal. It doesn't journal the start of every dictation, as the info level would.
  - `GSK_RENDERER=cairo GDK_DISABLE=gl,vulkan,dmabuf` makes the GTK4 waveform overlay draw in software, without a GPU context. `dmabuf` must stay in that list: the overlay sets `GDK_DISABLE=dmabuf` itself as a crash workaround (#656), but only when the variable is unset.

## Text injection on KDE Plasma 6 Wayland: why "paste" + ydotool

| Option | On KWin |
|---|---|
| wtype | doesn't work (no `zwp_virtual_keyboard_v1`), per the upstream FAQ |
| eitype (libei via the RemoteDesktop portal) | not packaged; uses the portal's keymap, so non-Latin text needs the matching layout (voxtype #180) |
| dotool | not packaged for Ubuntu; types via the XKB layout |
| ydotool `type` | US-ASCII keycodes only; stuck-modifier reports on KDE (#538) |
| **paste** = wl-copy + one Ctrl+V via ydotool | **chosen**: works for every script and layout |
| clipboard only | needs no privileges; you press the paste key yourself (Meta+Alt+H) |

**Terminals:**
- Ctrl+V does nothing in a terminal (Konsole wants Ctrl+Shift+V), so use Meta+Alt+H there.
- `shift+insert` was rejected because in Konsole it pastes the PRIMARY selection, which could be an old command.

## Models and languages

Measured on a Core Ultra 7 258V (voxtype 1.1.0, OpenVINO 2026.4.1, NPU driver 1.38.0):

| | whisper-small int8 | large-v3-turbo int8 |
|---|---|---|
| first NPU compile (once, then cached) | ~60 s, cache 1.1 GB | ~250 s, cache 2.0 GB |
| load from cache (daemon start / language switch) | 2.3–2.7 s | – |
| 5 s utterance on the NPU | ~0.5 s | **fails**: `zeFenceHostSynchronize ZE_RESULT_ERROR_UNKNOWN` |
| 30–40 s utterance on the NPU | 2–6 s | – |
| 4 s utterance on the CPU | 2.3 s | 10.9 s |
| peak RAM while loading | ~2.5 GB | – |

**Dictation uses whisper-small**, multilingual, on the NPU:
- turbo would be more accurate, but inside voxtype it fails on this NPU stack.
- voxtype only falls back to the CPU when the pipeline fails to *start*, not when an inference fails, so every dictation with turbo would error.
- npu-transcribe uses turbo successfully for files (different pipeline settings); see [npu-transcribe.md](npu-transcribe.md).

**The OpenVINO engine takes one fixed language per daemon run:**
- There is no `auto`; a list like `["en","de"]` only exists for the whisper.cpp engine.
- **Switching:** `voxtype-lang` runs `voxtype config set openvino.language XX` and restarts the daemon (~3 s). It refuses while a meeting or dictation is running.
- **Wrong language set:** speaking German with `en` set gives a poor English rendering, not German.

## Meeting mode

- **Starting and stopping:** Meta+Alt+M, the widget (also a middle-click on it), the app, or `voxtype-meeting-toggle [start|stop]`.
- **What is recorded:** the default microphone (labelled **You**) and system audio (labelled **Remote**).
  - System audio is `@DEFAULT_MONITOR@`, the monitor of the *current* default output. voxtype's `auto` can pick an idle HDMI monitor instead.
  - Switching outputs mid-meeting isn't followed.
- **Transcription:** every 30 s chunk is transcribed on the NPU (~2.3 s per chunk).
- **Saving on stop:**
  - The toggle waits until the daemon has processed the last chunk.
  - It then writes `~/Documents/Meetings/YYYY-MM-DD_HHMM-meeting.md` (timestamps, speakers, metadata; it never overwrites) and shows a notification.
  - The full record stays in `~/.local/share/voxtype/meetings/` (SQLite + JSON). Useful commands: `voxtype meeting list|show|export|delete`.
- **Echo cancellation:** `echo_cancel = "auto"` uses the GTCRN model to remove your speakers' sound from the mic track; with headphones it changes little.
- **Limits and storage:** maximum 180 min. Audio is not kept (`retain_audio = false`).

## Is it really on the NPU?

```bash
bash ~/.local/share/bmeet/src/voxtype/verify.sh          # checks + one synthetic transcription
bash ~/.local/share/bmeet/src/voxtype/verify.sh --watch  # then dictate with Meta+H
```

**Watch the busy counter.** `/sys/class/accel/accel0/device/npu_busy_time_us` counts the microseconds the NPU was busy:
- **Dictation:** it jumps by ~0.3–1 s per dictation, and every 30 s during a meeting.
- **CPU fallback:** if it stays flat while you dictate, transcription is running on the CPU.
- **Other NPU users:** the counter is device-wide, so other programs using the NPU raise it too.

**Ignore the configured-device line.** The daemon's "pipeline created … (device=NPU)" prints the *configured* device, not the one in use. To find fallbacks:
```bash
journalctl --user -u voxtype -b | grep "fell back"
```

## Caveats

- **Clipboard:** every dictation goes through the clipboard and lands in Klipper's history. `restore_clipboard` is off because voxtype 1.1.0 adds a trailing newline to the restored content (#747), which is risky before a terminal paste.
- **Holding Meta:** if you still hold Meta when the text arrives (~0.5–1 s after the second Meta+H), apps see Meta+Ctrl+V. voxtype's "wait for modifier release" needs read access to `/dev/input/event*` (the `input` group), which is deliberately not granted.
- **Daemon notifications** are off: only the first one shows on Plasma (#850), and they would copy dictated text into the notification history. The sounds and the overlay show the state instead.
- **Memory:** the daemon keeps the pipeline loaded (~2.5 GB peak at load), and a running meeting loads a second copy. `on_demand_loading = true` frees it between dictations, at ~3 s extra latency each time.
- **voxtype setup commands:**
  - **Don't run:** `voxtype setup npu --enable` (switches to English-only `base.en`), `voxtype setup gpu --enable`, `voxtype setup onnx --enable` or `voxtype setup systemd`. They rewrite the binary, the model or the unit.
  - **Safe:** `voxtype setup npu --status` and `voxtype configure`.
