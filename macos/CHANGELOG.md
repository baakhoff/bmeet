# Changelog: BMeet for macOS

All notable changes to BMeet for macOS are documented here: its releases are the tags
`macos-vX.Y.Z`, and its version is in `macos/VERSION`. BMeet for Linux is released on its
own, with its own versions and changelog: [CHANGELOG.md](../CHANGELOG.md). Changes to the
code both share are in both ([docs/releasing.md](../docs/releasing.md)). The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- **Models are released when nothing uses them,** by one setting: `~/.config/bmeet/unload-after`, in minutes (0–60, default 5; `0`: right after each use). Each process reads it when it decides, so a change applies without a restart. What each process holds and when it lets go: [docs/macos.md](../docs/macos.md#memory).
  - **bmeet-whisper** releases each model `unload-after` after its last use. The install's model (turbo) gets the whole time; small (for translating) and a copy on the CPU get at most 3 minutes. Before, turbo stayed loaded until the server exited, 10 minutes after the last connection.
  - Only requests that use a model count. A client that only looks whether the server is up, or asks for its status, no longer keeps the server and turbo alive. The server exits once it has held no model and had no work for 10 minutes.
  - Its `status` says what is loaded, since when and when it goes. A new `unload` op releases every model, or one. `bmeetmac.whisper_unload()` is its client, and the installer's warm-up uses it to let go of small at once.
  - **The dictation engine** (`npu-transcribe --dictate`) releases GigaAM `unload-after` after the last dictation, without waiting for BMeet.app to end it, and loads it again at the next one. It starts recording first, so nothing said meanwhile is lost.
  - **Memory pressure:** bmeet-whisper releases its models on macOS's memory-pressure warning or critical event, once the request that runs (if one does) is done, and says so in `whisper.log`. The dictation engine reads the same level on its idle tick, every 30 s while it holds a model (one system call, no process).
- **`npu-transcribe --queue`:** runs started with it go one at a time (a lock in the runtime folder, which a crash lets go of too). A file started while another is being transcribed then waits instead of loading the models a second time. Finder's services do not pass it yet.

### Fixed
- **The speaker pass crashed in Core ML (status -11), and the meeting lost its speakers.** Core ML keeps a prediction's inputs after `predict()` returns, and later lets go of them on a thread of its own (`MLE5ExecutionStream`'s reset queue, 1.2 s after GigaAM's first prediction). coremltools 9.0 passes numpy arrays to it without a copy, and drops its reference to them there, without Python's lock. When that was an array's last reference, numpy freed it on a thread Python does not know, and Python crashed (`EXC_BAD_ACCESS` at `0x10`).
  - In the pass of 2026-11-27 that array was GigaAM's `feature_lengths`: the pass ran GigaAM once, then waited for Whisper. Four of the installer's model checks crashed the same way on 2026-11-26.
  - Now every Core ML input is a copy that BMeet keeps until Core ML has let go of it, so Core ML's release never frees anything (`bmeetmac.coreml_predict`, used by npu-transcribe and `macos/prepare-coreml`). The details: [docs/npu-transcribe.md](../docs/npu-transcribe.md#findings-and-workarounds).
- **A speaker pass that crashed left no word of it:** the transcript stayed the live one (*You* / *Remote*, or *Room*), with nothing to say why. Now a pass that ends badly leaves a line under the transcript's Meeting Info: the voices were not told apart, and why (it crashed, was ended, or failed). When npu-transcribe could not report it itself (a crash or a kill), a notification says so too.
- **A translation, or the installer's warm-up, displaced turbo:** the server kept loaded the model of the last transcription, so one use of small made turbo the one released after 3 minutes. Turbo now keeps its time whatever was used last.
- **bmeet-whisper's `status` listed the models it had already released.**

### Tests
- `macos/tests/test_macos.py`, with the real bmeet-whisper and stand-in models (`BMEET_WHISPER_TEST=1`, tests only):
  - a model goes after `unload-after` while clients keep looking and asking for the status;
  - `unload`;
  - the setting is read when it is needed;
  - memory pressure, injected with a test-only op;
  - turbo keeps its time after small;
  - the server exits when idle.
- The speaker pass:
  - one that is killed leaves the line and a notification;
  - one that fails leaves the line without a second notification;
  - one that works leaves neither.
- `CoreMLPredict`: `coreml_predict` through coremltools' own `predict()` code, with a stand-in for Core ML that keeps what it is given: Core ML gets only the copies BMeet watches (a float16 input too), and they are freed once it lets go.
- `CoreMLInputs`, local only (CI has no models): the installed GigaAM, Nemotron and TitaNet on the Neural Engine, on Russian speech made with `say`, with pauses after each prediction longer than Core ML takes to let go of its inputs; and a speaker pass of an in-person recording.
- `transcribe/tests/test_dictation.py`: the dictation engine releases its model when idle, loads it again for the next dictation, reads `unload-after` when it is needed, and lets go on memory pressure.
- **Tests no longer reach the BMeet.app running on the Mac:**
  - `bmeet/dictate/tests/test_dictate.py` ran `bmeet-ctl` on its Mac path, which sent `dictate` to the real control socket. On 2026-11-27 that started dictations from the microphone in the running BMeet.app. Its commands now take bmeet-ctl's Linux path in a runtime folder of their own, and the file refuses to start a command that could reach the Mac's.
  - The meeting tests posted real notifications through `osascript`. A test's notifications now go to a file (`BMEET_NOTIFY_LOG`), and with a test's runtime folder (`BMEET_RUNTIME_ROOT`) `bmeetmac.notify` never runs `osascript`.
  - bmeet-ctl's tests fail instead of running `open` (which would open BMeet.app) or `osascript`.

## [0.9.1] - 2026-10-27

### Added
- **Updates from the menu bar:** when a new release of BMeet for macOS is out, BMeet's menu shows **Update to BMeet X…**, and a notification says so once. Update opens Terminal with the new release's installer, which quits BMeet and opens the new one at the end. *Check for Updates…* asks now. The BMeet window shows the same, with **What's New**. In Terminal: `bmeet-ctl update`, or `bmeet-ctl update --check` to only look.
  - BMeet asks GitHub for its releases once a day, with no data about you or your use of BMeet. *Settings → Check for Updates Automatically* turns that off.
  - An update keeps the options you installed with (`--no-start` and the like). The installer keeps them in `~/.local/share/bmeet/install-options`.
  - It waits while a meeting is recorded or its speaker pass runs.
  - A BMeet window that was open during an update offers **Restart**, so it runs the new version.
  - Coming from 0.9.0, which has no update check: run the install line once.

## [0.9.0] - 2026-10-27

BMeet on Macs with Apple silicon: everything it does on Linux, with the models on the Neural Engine. The first release of BMeet for macOS.

Install (M1 or later, macOS 14.2 or later; no sudo):

```bash
curl -fsSL https://raw.githubusercontent.com/baakhoff/bmeet/main/install.sh | bash
```

### Added
- **macOS (M1 and later, macOS 14.2+):** the same one-line install (`install.sh` hands over to `macos/install.sh`; no sudo). It brings dictation, meeting transcripts with who said what, in-person meetings, file transcription, the BMeet window and the Chrome mute extension. Details, measurements and the differences from Linux are in `docs/macos.md`.
  - **The models on the Neural Engine,** Apple's NPU, with the CPU as the fallback, as with the Intel NPU on Linux:
    - **Whisper large-v3-turbo:** WhisperKit's Core ML models in `bmeet-whisper`, one server that every process shares (the model is loaded once on an 8 GB Mac; dictation goes first). A 23 s talk took 3.0 s on an M1.
    - **GigaAM v3, Nemotron 3 Diarization and TitaNet:** from the same pinned files as on Linux, built into float16 Core ML programs at install time by `macos/prepare-coreml`. GigaAM takes 112 ms per 30 s, the Nemotron encoder 72 ms per step, TitaNet 44 ms per 16 fingerprints; 98–99 % of their ops run on the Neural Engine, and each build is checked against the CPU.
  - **BMeet.app,** a menu bar app (Swift):
    - shortcuts: ⌃⌥H dictate, ⌃⌥⌘H to the clipboard, ⌃⌥M meeting, ⌃⌥⇧M in person, ⌃⌥⇧H language;
    - the menu bar icon and menu (the panel widget's job);
    - a floating panel with the words while you dictate, then a paste with ⌘V;
    - Finder's *Transcribe with BMeet* services, notifications, start at login.
    - Everything that records or pastes runs inside it, so macOS asks for the microphone, the system audio and Accessibility once, for BMeet.
  - **`bmeet-record`:** the microphone and the system audio (a Core Audio tap), in bmeet-meeting-audio's layout and lined up by hardware time.
  - **`bmeet-meeting`:** the meetings voxtype runs on Linux. The meeting audio stays on a RAM disk, never on the disk.
  - **The BMeet window:** the Linux app itself, inside `BMeet.app` (`macos/window/launcher.c` runs its Python in the bundle, so it is *BMeet* in the Dock).
  - **Health check and uninstall:** `macos/verify.sh` checks the install. `uninstall.sh` removes it (`--purge` also deletes the models).
  - **A Mac look:**
    - The window has SF Symbols drawn at install time, a Finder-style sidebar, Apple's system colors, transcripts in macOS's type, and the Mac menus (⌘1–3, ⌘F, ⌘O, ⌘,). Its texts say ⌃⌥ and Finder.
    - The menu bar menu has SF Symbols and native shortcut glyphs.
    - The dictation panel's dot pulses while BMeet listens.
  - **The installer on a Mac:**
    - Nine numbered steps with ✓ and timings, a spinner for the long ones, and the details in `~/Library/Logs/BMeet/install.log`.
    - It installs the Command Line Tools (Apple's own window) and offers Homebrew when they are missing.
    - `BMEET_REF=BRANCH` tries a branch with the same one line.
    - It warns when iCloud syncs your Documents folder, because the meeting transcripts saved there then go to iCloud too.
    - Each Core ML build is compared with the CPU, and a build that does not match stops the install. Interrupted model downloads resume.
  - **When something goes wrong in a meeting:**
    - Quitting BMeet during a meeting asks first, then stops and saves the meeting.
    - If the recorder stops (or the RAM disk is full), BMeet says so, stops the meeting and keeps what was recorded.
    - If BMeet may not write to Documents, the transcript goes to `~/.local/share/bmeet/meetings`, and BMeet says so.
    - A live transcript that did not finish is kept, marked incomplete, instead of being thrown away with the audio.
    - Switching headphones, AirPods or the output device during a meeting keeps both tracks right: the recorder follows the new device and does not drift.
  - **Dictation:** text pasted with ⌘V does not go to your other devices through Universal Clipboard, and clipboard managers skip it.

### Fixed
- **Words were lost from a dictation, or from a live meeting phrase, in clean speech** (on Linux too; in its changelog under *Unreleased*):
  - BMeet tells sound from quiet by the background noise, which it takes from the quietest tenth of the last 30 s. In the first seconds there may be no quiet in that window, and then the "noise" was speech.
  - With clean speech (−13 to −19 dBFS: a close microphone, synthetic speech) the threshold rose to −12.7 dBFS. A pause was heard in the middle of a sentence, the words before it became final too early, and the words after the cut were lost.
  - A "noise" louder than −30 dBFS is speech, so anything louder than −30 dBFS is then sound (`LIVE_SPEECH_DBFS`). A real background keeps the 12 dB margin as before, so a loud room still has its pauses.
  - Found on a Mac, where the slower passes on the Neural Engine made it show. `transcribe/tests/test_dictation.py` has the case, and a loud room too.

### Tests
- **`macos/tests/test_macos.py`** (CI: a new macOS job on Apple silicon) tests, without models or permissions:
  - the Whisper server protocol;
  - bmeet-record, playing files instead of the devices;
  - a whole meeting through bmeet-meeting, with a stand-in transcriber;
  - bmeet-ctl's commands to BMeet.app.
- **The app's tests on a Mac:** they run there too (`BMEET_PLATFORM=linux` runs the Linux window with its fake voxtype), and keep their files out of your own Trash.

[Unreleased]: https://github.com/baakhoff/bmeet/compare/macos-v0.9.1...main
[0.9.1]: https://github.com/baakhoff/bmeet/releases/tag/macos-v0.9.1
[0.9.0]: https://github.com/baakhoff/bmeet/releases/tag/macos-v0.9.0
