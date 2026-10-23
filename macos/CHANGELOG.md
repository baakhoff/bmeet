# Changelog: BMeet for macOS

All notable changes to BMeet for macOS are documented here: its releases are the tags
`macos-vX.Y.Z`, and its version is in `macos/VERSION`. BMeet for Linux is released on its
own, with its own versions and changelog: [CHANGELOG.md](../CHANGELOG.md). Changes to the
code both share are in both ([docs/releasing.md](../docs/releasing.md)). The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/).

## [0.9.0] - 2026-10-20

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

[0.9.0]: https://github.com/baakhoff/bmeet/releases/tag/macos-v0.9.0
