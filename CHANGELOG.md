# Changelog

All notable changes to BMeet for Linux are documented here: its releases are the tags
`vX.Y.Z`. BMeet for macOS is released on its own, with its own versions and changelog:
[macos/CHANGELOG.md](macos/CHANGELOG.md). Changes to the code both share are in both
([docs/releasing.md](docs/releasing.md)). The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- **BMeet for macOS** lives in this repository now (`macos/`), with releases of its own (tags `macos-vX.Y.Z`) and its own changelog, [macos/CHANGELOG.md](macos/CHANGELOG.md). BMeet on Linux works as before.
- **Releases, one platform at a time:** `release/release` sets a platform's version (`bump`), checks that it is ready, and tags it. GitHub Actions then runs all of CI on the tag and publishes the release, with its changelog section as the notes (`release-linux.yml`, `release-macos.yml`). How it works: [docs/releasing.md](docs/releasing.md).

### Fixed
- **Words were lost from a dictation, or from a live meeting phrase, in clean speech:**
  - BMeet tells sound from quiet by the background noise, which it takes from the quietest tenth of the last 30 s. In the first seconds there may be no quiet in that window, and then the "noise" was speech.
  - With clean speech (−13 to −19 dBFS: a close microphone, synthetic speech) the threshold rose to −12.7 dBFS. A pause was heard in the middle of a sentence, the words before it became final too early, and the words after the cut were lost.
  - A "noise" louder than −30 dBFS is speech, so anything louder than −30 dBFS is then sound (`LIVE_SPEECH_DBFS`). A real background keeps the 12 dB margin as before, so a loud room still has its pauses.
  - Found on a Mac, where the slower passes on the Neural Engine made it show. `transcribe/tests/test_dictation.py` has the case, and a loud room too.

### Changed
- **`transcribe/prepare-diarization`:** Nemotron's graph is now one function (`build_graph`) that takes its ops as an argument, so macOS builds the same model with Core ML ops. The OpenVINO model it builds is byte for byte the one 0.8.1 built. Because the script changed, the next update rebuilds it once (about 20 s; nothing is downloaded).
- **npu-transcribe, bmeet-ctl, the app and the Chrome extension's host also run on macOS.** The Mac code lives in `sys.platform == "darwin"` branches and `macos/bmeetmac.py`; on Linux they behave as before. The app's process check works without `/proc` there.
- **CI** also runs on a Mac with Apple silicon (the `macos` job), because the code both platforms share runs there too.

## [0.8.1] - 2026-10-15

### Fixed
- **A meeting started in the same minute as the one before lost its speaker pass and its live transcript:** each meeting's recording was named after its start minute. Stopping a meeting and starting the next at once (on 2026-10-15, a meeting and then an in-person one, both at 11:35) put the second recording into the first one's folder; when the first one's speaker pass ended, it deleted that folder, the second recording with it. The second meeting kept only voxtype's own transcript (everyone under *You* in person), and its audio was lost. Now a minute whose recording is still there gives the next one `-2`, `-3`, … (`voxtype-meeting-toggle`), and the first pass also no longer reads the second meeting's settings.
- **Two meetings of one minute could swap transcripts in the app:** the app paired them with their Markdown copies by modification time, which the speaker pass changes. Now a copy goes to the meeting with its title when no other copy of that minute has that title, and otherwise in the order the copies were written (a later one is named `-meeting-EPOCH.md`); a meeting never gets a copy another meeting can claim by its title, nor a `-meeting-EPOCH.md` copy written long after it ended. On 2026-10-15 both copies got the second meeting's title (the first pass read the second meeting's settings), and they still pair right. The widget's recent list uses the same rule.
- **Two normal meetings started in one minute shared voxtype's own copy:** voxtype keeps a meeting's files in a folder named after its date and title, and the titles only go down to the minute ("Meeting 2026-10-15 11:35"). The second meeting overwrote the first one's transcript there, and deleting one in the app deleted the other's. A title voxtype already has now gets " (2)", " (3)", …
- **"Telling the speakers apart…" on the wrong meeting:** the speaker pass's marker now names the transcript it replaces, so only that meeting shows as busy, not every meeting of its start minute (a meeting without a copy yet: by its start minute, as before).

### Tests
- **`voxtype/tests/test_meeting_toggle.py`** (in CI): the meeting toggle itself, with stand-ins for voxtype, systemd and the clock: one meeting, two in one minute with the first speaker pass ending while the second records, three in one minute (titles), and a minute whose pass is still stopping. The app's tests pair same-minute copies in both the app and bmeet-ctl, including the 2026-10-15 shape.

## [0.8.0] - 2026-10-14

See the text while you dictate: a prototype, off by default.

### Added
- **Text while you speak** (`echo on > ~/.config/bmeet/live-dictation`): Meta+H uses BMeet's own dictation instead of voxtype's, and the words appear in the text field you are in as you speak. They are underlined until they are final, and each sentence goes in for good when you pause after it. Details and measurements in `docs/live-dictation.md`.
  - **Into the field without the clipboard:** for the length of each dictation BMeet becomes KWin's input method (the slot of System Settings → *Virtual Keyboard*), and afterwards puts back what was there, switched on or off as it was. What was there is noted in `~/.local/state/bmeet/input-method.json` first, so a crash can't lose it. Tested with Qt, GTK 3, Chrome 155 and Konsole; X11 programs get a panel at the bottom and a paste at the end. `panel` instead of `on` always does that and leaves KWin alone.
  - **Russian (GigaAM v3):** half the word errors of today's dictation on Podlodka clips (7.3 % against 16.2 %), as good as GigaAM on the whole recording, also past 30 s. The text is ~0.5 s behind your words, and ~0.2 s after the stop it is complete. CPU about as today's: ~0.4 s per minute of speech.
  - **English (Whisper turbo):** 1.1 % word errors on LibriSpeech (today 2.7 %), ~1.2 s behind your words. It costs more CPU than Russian: ~2.3 s per minute of speech, against ~0.4 s for today's dictation. Whisper computes its audio features on the CPU for every pass.
  - `npu-transcribe --dictate` is the engine (commands on stdin, the text as it changes on stdout), `bmeet-dictate` the service that `bmeet-ctl dictate` starts, and `bmeet-im` the input method. `npu-transcribe --dictate-file FILE` dictates a file, for tests.

### Changed
- The Meta+H and Meta+Alt+H launchers run `bmeet-ctl dictate`, which goes to voxtype as before unless live dictation is on.

### Tests
- `transcribe/tests/test_dictation.py` (offline) and `bmeet/dictate/tests/test_dictate.py` (offscreen, and in a private headless KWin with its own D-Bus and kwinrc: text into a Wayland field, X11 fallback, cancel, the slot always given back) run in CI; the KWin part skips there. `transcribe/tests/dictate_test.py` measures the engine on real Russian and English speech on the NPU.

## [0.7.1] - 2026-10-14

### Fixed
- **Export gave the transcript from before the speaker pass:** *Export…* in the app asked voxtype for its copy of the meeting, which is the one from during the meeting: You / Remote only (in person, everything under You), without the people told apart, their names, the tidying, or the echo and muted words left out. It now exports the meeting's Markdown copy, which has all of that: as it is, or as plain text or JSON in voxtype's layouts. A running meeting exports its live transcript; voxtype's copy is used only for a meeting without a Markdown copy.

## [0.7.0] - 2026-10-14

See what has been said while the meeting is still on.

### Added
- **Live meeting transcript:** the BMeet window shows the running meeting's transcript as people talk, **You** and **Remote** (in person: **Room**), and keeps scrolling with it unless you scroll up to read. A meeting that starts while the window is open is selected. Until now nothing showed before the stop: voxtype keeps its own transcript to itself until the meeting ends.
  - `npu-transcribe --live DIR` follows BMeet's meeting recording as it grows (unit `bmeet-live`, started by `voxtype-meeting-toggle` next to the recorder). Each track is cut into phrases where its sound stops, and a phrase is transcribed once it has ended, on the NPU.
  - **Russian (GigaAM):** the sentence still being said shows in italics, redone every 1.5 s of it. In the tests the words showed up about 2 s *before* the end of a line, and the line was final 2-5 s after it. That costs ~15 s of CPU per hour of meeting (plus ~2 s to load the models).
  - **Other languages (Whisper turbo):** a phrase at a time, ~2 s after it ends. Whisper takes more CPU: ~1.3 s per minute of meeting.
  - **Your microphone:** what you said while muted in the call is left out, and so is the other side's echo, as in the speaker pass. The echo doesn't keep your phrase open either: only what is clearly louder than it counts as you.
  - **When you stop:** the live transcript, finished, becomes the meeting's Markdown copy instead of voxtype's (whisper-small, without the echo and muting filters), until the speaker pass replaces it with Person 1, Person 2, …
  - Off with `off` in `~/.config/bmeet/live-transcript`. It needs the meeting audio, so not with `--no-speakers`.

### Not changed
- **Dictation still types the text when you stop.** voxtype's own way of typing while you speak (`[openvino] streaming`) was tested on the NPU and left off. It took 5-6x the CPU of normal dictation, dropped audio past ~25 s of speech, sometimes typed text twice, and with BMeet's paste output it would fill the clipboard history with fragments and lose the last words (details in `docs/voxtype.md`).

### Tests
- **`transcribe/tests/meeting_test.py --live`:** records the made-up meeting of real voices again in real time while `--live` follows it, and reports how soon each line showed up. All lines showed in Russian and English, with echo, with headphones, muted and in person; none of the other side's words were under *You*, and nothing said while muted.

## [0.6.0] - 2026-10-13

Telling the speakers apart with NVIDIA's Nemotron 3 Diarization, on the NPU: far less speech under the wrong person, in Russian first of all.

### Changed
- **Who speaks when:** NVIDIA's Nemotron 3 Diarization (September 2026, OpenMDW-1.1) replaces pyannote segmentation + clustered TitaNet fingerprints, still on the NPU. Measured on real recordings (`transcribe/tests/speakers_test.py`):
  - **Russian podcasts** (4.1 h, 2-3 people): speech under the wrong person 8.1 % → 2.1 %.
  - **AMI meetings** recorded by one microphone in the room (9 h, 3-4 people): 22.8 % → 8.4 %; diarization error rate 42.3 % → 11.0 %; the right number of people in 15 of 16 meetings instead of 4.
  - **Someone who says little:** a phrase of about 5 s or more usually gets a voice of its own. Before, voices with under 15 s of speech were folded into others.
  - **Faster:** ~16 s per hour of audio on the NPU instead of ~30 s.
  - **Remembered voices** still use TitaNet fingerprints, so the voices you named and your own voice carry over.
  - **Install:** the installer downloads the model (~400 MB) and `transcribe/prepare-diarization` rebuilds it for the NPU without PyTorch (~20 s, once). On the NPU it agrees with NVIDIA's reference code on 99.98 % of the 10 ms frames. `NPU_DIARIZATION=pyannote` uses the way of before.
  - Up to 8 voices per track.
- **One person as two voices:** over a long recording Nemotron now and then opens a second voice for someone; voices whose fingerprints are at least 0.75 alike become one. That halved the speech under the wrong person on the Russian podcasts, to 2.1 %.

### Fixed
- **Their last word under *You*:** a word the other side says just as you start talking, which your microphone picks up from the speakers, was kept in your line: the echo filter compares loudness, and you are loud there already. The same word at the same moment on both tracks is now theirs.
- **Learning your own voice from long lines:** only the middle 4 s of each of your lines counted, so meetings where Whisper joined your sentences into a few long lines could teach nothing (15 s needed). A long line now gives several 4 s pieces.

### Tests
- **`transcribe/tests/meeting_test.py` uses real people:** four speakers of Russian podcasts (transcribed by GigaAM, as in a meeting) or four LibriSpeech readers, downloaded into the work folder. Every case comes out with 100 % of the words under the right label, in both languages. Nemotron often hears espeak's synthetic voices as one person, so `--espeak` now checks only your label, the echo and muting.
- **`transcribe/tests/speakers_test.py`:** who speaks when on the Russian podcasts and (`--ami`) the AMI meetings.

## [0.5.2] - 2026-10-13

English terms in Russian speech come out right far more often.

### Added
- **English terms in Russian transcripts:** GigaAM, used for Russian, garbles English words ("Data Dog", "worниiнг statemont", "Рuби, Пиtн") or writes them in Cyrillic, while Whisper hears most of them. Now the sentences in which GigaAM wrote Latin letters also go to Whisper, and each English word Whisper heard replaces the GigaAM word at the same moment that sounds like it.
  - Russian endings stay ("warning-и", "GPU-шки", "Go-разработчиков"), words that don't sound alike are never touched, and words the two models agree on keep GigaAM's spelling.
  - Measured on 33 min of a Russian IT podcast with 98 English terms (Podlodka): word errors went from 7.05 % to 6.52 %, and the English terms written right from 22 % to 54 %. Whisper turbo alone gets 58 % of the terms but 8.2 % word errors.
  - **Cost:** Whisper ran on 12 % of the audio, which took 29 s on the NPU and 12 s of CPU for the 33 minutes. Expect a minute or two more per hour of a term-heavy meeting.
  - `npu-transcribe --no-english-terms` turns it off. `transcribe/tests/english_terms_test.py` reruns the measurement.

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

[Unreleased]: https://github.com/baakhoff/bmeet/compare/v0.8.1...main
[0.8.1]: https://github.com/baakhoff/bmeet/releases/tag/v0.8.1
[0.8.0]: https://github.com/baakhoff/bmeet/releases/tag/v0.8.0
[0.7.1]: https://github.com/baakhoff/bmeet/releases/tag/v0.7.1
[0.7.0]: https://github.com/baakhoff/bmeet/releases/tag/v0.7.0
[0.6.0]: https://github.com/baakhoff/bmeet/releases/tag/v0.6.0
[0.5.2]: https://github.com/baakhoff/bmeet/releases/tag/v0.5.2
[0.5.1]: https://github.com/baakhoff/bmeet/releases/tag/v0.5.1
[0.5.0]: https://github.com/baakhoff/bmeet/releases/tag/v0.5.0
[0.4.0]: https://github.com/baakhoff/bmeet/releases/tag/v0.4.0
[0.3.1]: https://github.com/baakhoff/bmeet/releases/tag/v0.3.1
[0.3.0]: https://github.com/baakhoff/bmeet/releases/tag/v0.3.0
[0.2.0]: https://github.com/baakhoff/bmeet/releases/tag/v0.2.0
[0.1.0]: https://github.com/baakhoff/bmeet/releases/tag/v0.1.0
