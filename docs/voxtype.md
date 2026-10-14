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
- **In person:** Meta+Alt+Shift+M, *In person* in the app or widget, or `voxtype-meeting-toggle [start|stop] --in-person`. voxtype runs the same meeting (its live transcript still has *You* / *Remote*), but `bmeet-meeting-audio --mic-only` records only the microphone, and `meeting.json` says `"in_person": true`. The speaker pass then splits the microphone by voice.
- **What is recorded:** the default microphone (labelled **You**) and system audio (labelled **Remote**).
  - System audio is `@DEFAULT_MONITOR@`, the monitor of the *current* default output. voxtype's `auto` can pick an idle HDMI monitor instead.
  - Switching outputs mid-meeting isn't followed.
- **Transcription:** voxtype transcribes every 30 s chunk on the NPU (~2.3 s per chunk) but keeps the text to itself until the meeting stops; what the BMeet window shows meanwhile is BMeet's live transcript (below).
- **Saving on stop:**
  - The toggle waits until the daemon has processed the last chunk.
  - It then writes `~/Documents/Meetings/YYYY-MM-DD_HHMM-meeting.md` (timestamps, speakers, metadata; it never overwrites) and shows a notification.
  - The full record stays in `~/.local/share/voxtype/meetings/` (SQLite + JSON). Useful commands: `voxtype meeting list|show|export|delete`.
- **Echo cancellation:** `echo_cancel = "auto"` uses the GTCRN model to remove your speakers' sound from the mic track; with headphones it changes little.
- **Limits and storage:** maximum 180 min. voxtype keeps no audio (`retain_audio = false`).
- **Speakers (BMeet):** voxtype labels the live transcript *You* (microphone) and *Remote* (system audio). Its own `ml` backend can't do better with the OpenVINO engine: that engine returns one segment per 30 s chunk, so each chunk would go to one speaker, and *You* would be lost. So BMeet tells the remote voices apart after the meeting:
  - `voxtype-meeting-toggle` starts `bmeet-meeting-audio` (systemd user unit) right after `voxtype meeting start`. It records the default microphone and the default output's monitor (the same sources voxtype uses) with `pw-record` as 16 kHz mono WAV into `$XDG_RUNTIME_DIR/bmeet/meeting-audio/STAMP/` (RAM, mode 0700). It follows voxtype's meeting state: nothing while paused, a new part after each pause, and it stops when the meeting ends.
  - While it records, `npu-transcribe --live` (unit `bmeet-live`) transcribes it as it grows into `live.md` next to it, which the BMeet app shows under the running meeting (voxtype keeps its own transcript to itself until the meeting stops). After the stop it replaces voxtype's export in the Markdown copy ([npu-transcribe.md](npu-transcribe.md#live-transcript)). `~/.config/bmeet/live-transcript` saying `off` turns it off.
  - After the stop and voxtype's own export, `npu-transcribe --meeting` runs as the unit `bmeet-speakers-STAMP` (see [npu-transcribe.md](npu-transcribe.md#speakers)) and replaces the Markdown copy. The unit's `ExecStopPost` deletes the audio however the pass ends; a meeting start also removes recordings no unit works on any more.
  - `~/.config/bmeet/speakers` (`on`/`off`) switches it; the installer's `--no-speakers` writes `off`.
  - Each part's start time goes to `part-N.start`. While it records, `$XDG_RUNTIME_DIR/bmeet/recording` names the folder, so `bmeet-chrome-host` (the native helper of BMeet's Chrome extension) can write the browser call's microphone state there (`call-mic.jsonl`). The pass leaves out what you said while the call could not hear you ([npu-transcribe.md](npu-transcribe.md#muted-in-the-call)).

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
- **Text while you speak (`[openvino] streaming`): tested, left off** (2026-10-14, voxtype 1.1.0, whisper-small on the NPU; Russian and English clips played into a separate test daemon). BMeet has its own way instead, off by default: [live-dictation.md](live-dictation.md). voxtype's works on the NPU, but:
  - **CPU:** it re-transcribes the whole utterance every 0.8 s, ~0.08 s of CPU each time: 5-6 s of CPU per minute of speech, against ~0.8 s for normal dictation, and ~45 % of the NPU's time.
  - **Long dictation:** past ~25 s of continuous speech a pass takes longer than the interval, and audio is dropped (a 59 s clip came out with 59-74 % word errors). `[streaming] interval_secs = 1.5` avoids that, at ~3 s from word to text.
  - **Repeats:** text is sometimes typed twice, both while you speak and in the end-of-recording pass; `revision_mode` repeats far more.
  - **With BMeet's paste output:** every piece (7-29 per utterance) is a separate `wl-copy` + Ctrl+V, so the clipboard ends up holding only the last piece and Klipper gets one entry per piece. Words not yet agreed on when you stop are not typed at all (pause ~2 s before stopping).
  - Text appears ~2 s after a word (Russian; English ~1.6 s), in bursts of a few words.
