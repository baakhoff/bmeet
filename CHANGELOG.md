# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/).

## [0.5.1] - 2026-10-13

Fixes from the first real in-person meeting, tidier text, and nothing slips onto the CPU.

### Fixed
- **Missing transcripts:** the app and the widget showed voxtype's rough live draft ("no Markdown copy") instead of the speaker pass's transcript. That happened when voxtype's start time fell in the next minute after the file's stamp: a meeting stamped 15:32 was started at 15:33:00 by voxtype's clock. A file stamped a minute either side now counts when its title is the meeting's.
- **Whisper loops** ("Я в рейтинге. Я в рейтинге. …" for a minute) are said once. That covers a phrase repeated 3 or more times, a word 5 or more times, and the same line 3 or more times in a row.
- **Never quietly onto the CPU:**
  - A Whisper block now writes at most 400 tokens. Without that cap, a loop overflowed the NPU decoder, and every later block failed on the NPU.
  - A block that still fails is retried once on the NPU and then skipped.
  - Before, the first failure moved the rest of the run to the CPU: 23 minutes of CPU time for 10 minutes of meeting. The CPU is now used only after 3 failed blocks in a row, which means the NPU itself is broken.
- **Odd language guesses:** a language that is not one of yours (`~/.config/bmeet/languages`) is checked on the next 30 s. Whisper had taken synthetic Russian for Polish and Latin.
- **In person:** a few seconds with no voice found are now *You*, not *Person ?*, and a line that was only a punctuation mark is dropped.

### Added
- **Tidier text:** fillers (э-э, а-а, хмм, эмм, um), stutters ("функцио функционал", "мы мы", "и-и-и"), Latin/Cyrillic mix-ups inside a word ("oкей", "Viкl"), and spacing around punctuation are cleaned up by fixed rules. On a real 65-minute meeting that removed 1.1 % of the words. Words said several times on purpose ("нет, нет, нет", "да да") stay. `npu-transcribe --verbatim` keeps everything.
- **CPU time per stage** in npu-transcribe's log (the speaker pass's journal), to find what still runs on the CPU.

### Tried, not shipped
- **Cleaning up the text with a language model on the NPU:** Qwen3-8B and Qwen3-4B-Instruct-2507, int4 as the NPU needs them, on a real Russian meeting. Asked to rewrite paragraphs, they inserted Chinese characters, cut words and left most fillers. Asked only for misheard terms, they translated Russian words, dropped words and invented terms. That would make transcripts worse in ways that are hard to spot (docs/npu-transcribe.md).

## [0.5.0] - 2026-10-13

Words you say while muted in the call are left out, and meetings in the room work too.

### Added
- **Muted in Meet or Slack: left out.** BMeet records your microphone itself, so it heard you even when the call didn't. A small Chrome extension now watches how much of your microphone's sound a Meet or Slack tab sends into the call. It sends none while you're muted, however the app mutes. The speaker pass silences your microphone for those stretches, and the transcript says how long was left out.
  - It reads sound levels only, never the audio, and only on meet.google.com and app.slack.com. Its helper writes the state into the meeting being recorded (in RAM) and nowhere else.
  - Load it once: chrome://extensions → Developer mode → Load unpacked → `~/.local/share/bmeet/src/bmeet/chrome/extension`. Without it, nothing changes.
  - Tested in headless Chrome: a disabled track, a removed track, a silent track and a paused sender were each caught within about a second.
- **In-person meetings** (Meta+Alt+Shift+M, the app's *In person* button, the widget, `bmeet-ctl meeting start --in-person`). Only the microphone is recorded, and the speaker pass splits it by voice into You, Person 1, Person 2, … Remembered people get their names.
  - **Your own voice** is learned from your microphone in online meetings (with remembered voices on), so in the room the voice that sounds like you is labelled You. A single voice is labelled You too.
  - If you start a normal meeting in the room by habit, nobody speaks through the speakers, and the microphone is split by voice anyway.
  - In the naming dialog of an in-person meeting you can write *You* for yourself. *Remembered voices…* lists your own voice and can forget it.

### Changed
- `bmeet-meeting-audio` writes each part's start time (`part-N.start`), so events can be placed in the audio, and `--mic-only` records the microphone alone.

## [0.4.0] - 2026-10-13

Russian is transcribed with GigaAM v3, and everything runs on the NPU, including telling the speakers apart.

### Added
- **GigaAM v3 for Russian** (Sber, MIT), used automatically for Russian meetings and files (detected or `--language ru`).
  - On Russian conversation (Podlodka podcast, 16 min) it made 6.5 % word errors against Whisper turbo's 8.2 %, at ~215x realtime instead of 14x.
  - The trade-off: English terms inside Russian speech come out mangled.
  - `npu-transcribe --engine auto|whisper|gigaam`, installer `--no-gigaam`.
- **`transcribe/prepare-models`** makes sherpa-onnx's int8 GigaAM export run correctly on the NPU, once, at install time. On the NPU that export wrote garbage. The int8 weights become float again, and the rotary position step, which the NPU compiler gets wrong, is folded into the query and key projections.

### Changed
- **Speaker separation runs on the NPU** (OpenVINO) instead of the CPU (sherpa-onnx). It's a new pipeline:
  - pyannote segmentation-3.0 finds up to 3 voices per 10 s window.
  - TitaNet-small fingerprints them, in batches of 16, with its statistics pooling scaled so the NPU's fp16 does not underflow.
  - Average-linkage clustering, threshold 0.4, then pyannote-style turn reconstruction.
  - The right number of people was found in all four test calls, at ~110x realtime.
- **CPU time:** numpy is kept to one thread, because its BLAS threads busy-waited. A 5-minute meeting now takes 8 s of CPU in total, down from 26 s.
- **Language detection:** the language is detected once per recording (for meetings, on the other side), also when Whisper is chosen.
- **Requirements:** `sherpa-onnx` is no longer needed; `onnx` is (install time only).

## [0.3.1] - 2026-10-12

Better Russian text and far fewer phantom speakers, tuned on a real Russian call.

### Fixed
- **Punctuation and accuracy:** npu-transcribe gives Whisper one 30 s window at a time instead of 2-minute blocks. On a real Russian call the old blocks lost most punctuation and capitals after the first window and misheard more words. English accuracy is unchanged and files transcribe faster.
- **Phantom speakers:** a 1:1 call came out with 24 "people". Voices with under 15 s of speech (and under 5 % of all) now join the voice they sound most like, voices at least 0.65 similar to a bigger one are merged, and short replies with no turn of their own go to the nearest speaker instead of *Remote*.
- **Stock phrases:** Whisper's stock phrases over silence ("Продолжение следует…", "Субтитры сделал DimaTorzok", "Thanks for watching!") are dropped.

## [0.3.0] - 2026-10-12

Meeting speakers get names, and BMeet can recognise them in later meetings.

### Added
- **Name speakers…** in the app turns *Person 1* into a name in a meeting transcript (the headings and the Speakers line). Two labels can get the same name when the pass split one person in two.
- **Remembered voices (opt-in):** with *Recognise them by voice in later meetings* ticked, the speaker pass keeps each meeting's voice fingerprints (`~/.local/share/bmeet/voices`, private). Naming people remembers theirs, at most 10 meetings per person, and later meetings call them by name.
  - Recognition works turn by turn. It splits a turn where a remembered person takes over and pulls a remembered person out of a voice the clustering merged with someone else.
  - *Remembered voices…* forgets one person; unticking forgets all.
- **`npu-transcribe --meeting … --voices DIR`**, the same from the command line.

### Changed
- **Meetings:** the language detected on the system audio is used for your microphone too. Detecting it again on your track alone could pick the wrong language, and Whisper then translated your words.
- **The app** notices when the open transcript is edited in place.

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

[0.5.1]: https://github.com/baakhoff/bmeet/releases/tag/v0.5.1
[0.5.0]: https://github.com/baakhoff/bmeet/releases/tag/v0.5.0
[0.4.0]: https://github.com/baakhoff/bmeet/releases/tag/v0.4.0
[0.3.1]: https://github.com/baakhoff/bmeet/releases/tag/v0.3.1
[0.3.0]: https://github.com/baakhoff/bmeet/releases/tag/v0.3.0
[0.2.0]: https://github.com/baakhoff/bmeet/releases/tag/v0.2.0
[0.1.0]: https://github.com/baakhoff/bmeet/releases/tag/v0.1.0
