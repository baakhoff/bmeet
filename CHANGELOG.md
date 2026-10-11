# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/).

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

[0.1.0]: https://github.com/baakhoff/npu-voicenotes/releases/tag/v0.1.0
