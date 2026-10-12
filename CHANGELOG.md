# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/).

## [0.2.0] - 2026-10-12

The project is now called **BMeet**, and meeting transcripts say who said what.

### Added
- **Speakers in meeting transcripts:** *You*, *Person 1*, *Person 2*, … instead of *You* / *Remote*.
  - While a meeting runs, `bmeet-meeting-audio` records the microphone and the system audio into RAM. Nothing is recorded while the meeting is paused.
  - After the stop, `npu-transcribe --meeting` transcribes both tracks again with Whisper large-v3-turbo and word timestamps. It tells the voices in the system audio apart (sherpa-onnx: pyannote segmentation-3.0 + NVIDIA TitaNet-small, on the CPU, in parallel with the NPU), cuts the echo of the other side from your microphone track, and replaces the Markdown copy.
  - The audio is then deleted.
  - voxtype's own transcript is still saved right away, as before.
  - Installer option `--no-speakers`, switch `~/.config/bmeet/speakers`.
- **`npu-transcribe --speakers` / `--num-speakers N`:** files labelled Speaker 1, Speaker 2, … in `.srt`, `.md` and `.txt`. Also in the Dolphin menu: *Transcribe with speakers*.
- **The app shows** "Telling the speakers apart…" for a meeting whose speaker pass runs, and reloads its transcript when the pass is done. `bmeet-ctl status` reports `speakers_pending`.
- **Tests:** `transcribe/tests/meeting_test.py`, an end-to-end meeting with synthetic voices in Russian and English, with and without echo.

### Changed
- **Renamed npu-voicenotes to BMeet:**
  - The repository is `baakhoff/bmeet`, the app and widget are *BMeet* (`io.github.baakhoff.bmeet`), and `voicenotes-ctl` is now `bmeet-ctl`.
  - Paths are `~/.local/share/bmeet`, `~/.config/bmeet` and `/var/lib/bmeet`.
  - The installer migrates an existing install: settings, languages, the download cache, the install record, and the panel widget (replaced in place).
- **`pipewire-bin`** (`pw-record`) is a declared dependency.

## [0.1.0] - 2026-10-11

First public release.

### Added
- One-line installer (`install.sh`) and uninstaller for Ubuntu/Kubuntu 26.04 with KDE Plasma 6.
  - **System part:** pinned, hash- and signature-checked downloads of voxtype 1.1.0, the OpenVINO GenAI 2026.4.1 SDK and Intel's NPU driver 1.38.0.
  - **Python venv** for npu-transcribe.
  - **Whisper models** pinned to tested Hugging Face revisions.
- **Dictation on the NPU** with voxtype: Meta+H types at the cursor, Meta+Alt+H copies to the clipboard. The OpenVINO GPU plugin is left out, so the fallback is NPU → CPU.
- **Meeting transcripts:** microphone + system audio, Meta+Alt+M. The Markdown copy goes to `~/Documents/Meetings`.
- **Language switching** across any number of Whisper languages (`~/.config/npu-voicenotes/languages`, Meta+Shift+H).
- **`npu-transcribe`:** audio and video files to `.srt`/`.md`/`.txt` on the NPU with CPU fallback, plus a Dolphin service menu.
- **Voice Notes app (PyQt6):** meeting history, transcribed files, a transcription queue, search and export.
- **Voice Notes panel widget** (`io.github.baakhoff.voicenotes`), which shows state and has controls for dictation, meetings and language.
- **`voicenotes-ctl`:** a JSON interface for the widget and app.
- **Tools:** the `verify.sh` health check, offscreen app tests and the npu-transcribe benchmark.

[0.2.0]: https://github.com/baakhoff/bmeet/releases/tag/v0.2.0
[0.1.0]: https://github.com/baakhoff/bmeet/releases/tag/v0.1.0
