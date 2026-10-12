# BMeet

Voice typing, meeting transcripts and file transcription for **KDE Plasma 6**. Whisper
runs locally on the **Intel NPU** of Core Ultra laptops. It's fast, it stays on your
machine, and the CPU and GPU are left alone.

[![CI](https://github.com/baakhoff/bmeet/actions/workflows/ci.yml/badge.svg)](https://github.com/baakhoff/bmeet/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/baakhoff/bmeet)](https://github.com/baakhoff/bmeet/releases)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

![BMeet app](docs/screenshot.png)

## What you get

- **Dictation anywhere:**
  - Press **Meta+H**, speak, and press it again. The text is typed at the cursor in any app.
  - Speed: about 0.5 s for a 5 s sentence on the NPU.
- **Meeting transcripts:**
  - Press **Meta+Alt+M** to record your microphone (*You*) and the system audio (*Remote*). Each 30 s chunk is transcribed on the NPU while the meeting runs.
  - When you stop, a Markdown transcript is saved to `~/Documents/Meetings`.
  - Audio is not kept.
- **File transcription:**
  - Run `npu-transcribe talk.mp4`, or right-click a file in Dolphin → *Transcribe (NPU)*. You get `.srt` subtitles and a readable `.md` transcript.
  - Whisper large-v3-turbo runs at ~25x realtime on the NPU.
- **BMeet app:**
  - Meeting history, transcribed files and a transcription queue, with search.
  - Export, copy and delete transcripts.
- **Panel widget:** a microphone icon that shows what is happening (ready, listening, transcribing, recording). Click it for dictation, meeting and language controls and your recent transcripts.
- **Several languages:** any language Whisper knows. **Meta+Shift+H** cycles through the ones you pick.

It builds on [voxtype](https://github.com/peteonrails/voxtype) (dictation and meetings) and
[OpenVINO GenAI](https://github.com/openvinotoolkit/openvino.genai) (Whisper on the NPU).
BMeet adds:
- the NPU setup,
- the KDE integration,
- the file transcriber,
- the app and the panel widget,
- a one-line installer.

## Requirements

- **CPU:** an Intel Core Ultra with an NPU (`/dev/accel/accel0`).
  - Tested on Lunar Lake (Core Ultra 7 258V).
  - Meteor Lake and Arrow Lake should work, but are untested.
  - Without an NPU, everything still works on the CPU, more slowly.
- **System:** Ubuntu or Kubuntu **26.04** (x86_64), with **KDE Plasma 6 on Wayland**.
- **Disk and network:** about 6 GB of free disk space, and internet access for the first install (~1.8 GB of downloads).

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/baakhoff/bmeet/main/install.sh | bash
```

The installer asks for your sudo password once. It then:

1. **Installs packages:** voxtype 1.1.0, the OpenVINO GenAI SDK, ydotool, ffmpeg and PyQt6, plus Intel's NPU driver if it's missing.
   - Every download is checked against a pinned sha256.
   - voxtype and the NPU driver are also checked against their upstream GPG signatures.
2. **Sets up the Python side:** creates a Python venv and downloads the Whisper models (small, plus large-v3-turbo) from Hugging Face, pinned to tested revisions.
3. **Compiles for the NPU** once (about 1 minute).
4. **Adds the KDE integration:**
   - the keyboard shortcuts and the `voxtype` user service,
   - the Dolphin menu,
   - the BMeet app,
   - the panel widget, added to the bottom panel.

To pass options:

```bash
curl -fsSL https://raw.githubusercontent.com/baakhoff/bmeet/main/install.sh | bash -s -- --languages "en de"
```

| Option | |
|---|---|
| `--languages "en de"` | dictation languages for Meta+Shift+H (Whisper codes; default: English + your desktop language) |
| `--no-turbo` | skip Whisper large-v3-turbo (~1 GB); file transcription then uses small |
| `--no-panel` | don't add the widget to the panel |
| `--no-npu-driver` | don't install Intel's NPU driver |
| `--skip-system` | skip the sudo part (it must have run before) |
| `--reset-config` | replace your voxtype config with the default |

**From a checkout:** run `git clone https://github.com/baakhoff/bmeet && cd bmeet && ./install.sh`.

**Upgrading:** run the same command again. The installer is idempotent and keeps your settings.

## Use

| | |
|---|---|
| **Meta+H** | start dictation; press again to stop. The text is pasted at the cursor |
| **Meta+Alt+H** | same, but only copies to the clipboard (for terminals: then Ctrl+Shift+V) |
| **Meta+Alt+M** | start or stop a meeting transcript |
| **Meta+Shift+H** | next dictation language (restarts the engine, ~3 s) |
| Panel widget | click for controls; middle-click starts or stops a meeting |
| **BMeet** (app menu) | meetings, transcribed files, *Transcribe a file* |
| Dolphin | right-click audio or video → *Transcribe (NPU)*, or *Open With → BMeet* |

```bash
npu-transcribe interview.m4a                  # interview.srt + interview.md
npu-transcribe --language de --translate a.ogg  # German speech -> English text
bmeet-ctl status                         # JSON state (what the widget shows)
voxtype meeting list                          # all meetings
```

**Shortcuts after a fresh install:** they're registered automatically, but KDE may only pick them up after you log out and in once.

## Configure

- **Dictation languages:** `~/.config/bmeet/languages`, for example `en de fr`.
- **voxtype:** `~/.config/voxtype/config.toml`. Change single values with `voxtype config set KEY VALUE`, then run `systemctl --user restart voxtype`. The defaults and the reasons for them are in [docs/voxtype.md](docs/voxtype.md).
- **Shortcuts:** System Settings → Keyboard → Shortcuts → *Voxtype: …*.

## Privacy

- **Local only:** audio is transcribed on your machine, and nothing is uploaded.
- **Meetings:** the audio isn't stored, only the text.
- **Telemetry:** OpenVINO's telemetry is blocked in npu-transcribe (it's on by default upstream; see [docs/npu-transcribe.md](docs/npu-transcribe.md)). Hugging Face telemetry is turned off for the model download.
- **Clipboard:** dictation goes through the clipboard (that's how text gets into every app on KWin), so it shows up in Klipper's history.

## Uninstall

```bash
bash ~/.local/share/bmeet/src/uninstall.sh
```

- **What it removes:** everything the installer added, including the packages it installed, unless they were there before.
- **What it keeps:** your meeting transcripts, transcripts next to your files, and the Whisper models. Add `--purge` to also delete the models and caches. Intel's NPU driver also stays.

## How it fits together

```
 Meta+H / Meta+Alt+M ─┐
 panel widget (QML) ──┼─> bmeet-ctl ──> voxtype daemon ──> OpenVINO GenAI ──> NPU
 BMeet (PyQt6) ─┘        (JSON)        (systemd --user)    (C SDK, /opt/intel)
         │
         └──────────────> npu-transcribe ──> OpenVINO GenAI (Python venv) ──> NPU, CPU fallback
                          (+ Dolphin menu)
```

| Path | |
|---|---|
| [`install.sh`](install.sh), [`uninstall.sh`](uninstall.sh) | one-line installer / uninstaller |
| [`system/`](system) | the sudo part: packages, NPU driver, voxtype binary, OpenVINO SDK, uinput |
| [`voxtype/`](voxtype) | voxtype config, shortcuts, meeting and language helpers, `verify.sh` |
| [`transcribe/`](transcribe) | `npu-transcribe` and its tests |
| [`bmeet/`](bmeet) | `bmeet-ctl`, the BMeet app and the Plasma widget |
| [`docs/`](docs) | design notes, benchmarks, troubleshooting |

## Development

```bash
bmeet/app/tests/run-tests.sh --quick     # app + bmeet-ctl, offscreen, no NPU needed
bmeet/app/tests/run-tests.sh             # + real Whisper runs on the NPU
bmeet/plasmoid/tests/check_qml.py --strict bmeet/plasmoid/*/contents/ui/*.qml
transcribe/tests/run-tests.sh /tmp/npt "NPU CPU"   # npu-transcribe benchmark / regression
bash voxtype/verify.sh                        # health check of an installed system
```

The tests never record audio. They use synthetic speech from espeak-ng and a scratch
database, and never open your real one.

## Known limitations

- **whisper-large-v3-turbo fails inside voxtype on the NPU,** so dictation uses whisper-small. npu-transcribe runs turbo fine.
- **One language per dictation session:** the OpenVINO engine has no auto-detection. Switch with Meta+Shift+H. File transcription does detect the language.
- **voxtype can't rename meetings yet.**
- **Opening the app from the widget** may only flash it in the task bar (Wayland focus-stealing prevention).
- **Only Ubuntu/Kubuntu 26.04 has been tested;** the pinned packages are built for it.

More in [docs/troubleshooting.md](docs/troubleshooting.md).

## Credits

- [voxtype](https://github.com/peteonrails/voxtype) (peteonrails/voxtype).
- [OpenVINO](https://github.com/openvinotoolkit/openvino) and the [Intel NPU driver](https://github.com/intel/linux-npu-driver) by Intel.
- [Whisper](https://github.com/openai/whisper) by OpenAI, in the int8 OpenVINO exports from [huggingface.co/OpenVINO](https://huggingface.co/OpenVINO).
- [GTCRN](https://github.com/k2-fsa/sherpa-onnx) speech enhancement from sherpa-onnx.

They're downloaded at install time under their own licenses. This repository is [MIT](LICENSE).
