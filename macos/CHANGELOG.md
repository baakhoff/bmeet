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
- **A Settings window** in BMeet.app: **Settings… ⌘,** in the menu (in place of the Settings submenu), *About BMeet*, and the control socket's new `{"op": "settings", "page": "general"}`. Five pages behind a toolbar, each control saved at once into its file in `~/.config/bmeet`:
  - **General:** the interface language, start at login, the daily update check, *Free memory after* (`unload-after`: right after use, 1, 5, 15, 30 or 60 minutes), pasting (Accessibility), the settings folder.
  - **Features:** dictation (with the words while you speak), online meetings, in-person meetings, telling speakers apart, remembered voices (with a question that says what is kept; off forgets them), file transcription.
  - **Languages:** every language Whisper knows, with a switch and a search, names in the interface language; the dictation language among the ones that are on. At least one stays on.
  - **Shortcuts:** each shortcut changed by pressing the new keys, or turned off, and registered again at once.
  - **About:** the version, **Check Now** or **Update…**, *What's New*, the models installed and their licences, the log folder.
- **Features can be turned off:** a feature that is off is gone from BMeet.app at once, and back when it is turned on: its shortcuts are not registered, its menu items go, the dictation panel never shows, and the control socket's `dictate` and `meeting` ops refuse it (a meeting that runs can still be stopped). Finder's services stay, as macOS takes them from the signed Info.plist; with file transcription off they say so in a notification and transcribe nothing.
- **BMeet.app in Russian:** its menu, the Settings window and its notifications, with the interface language as a setting (`interface-language`: System, English, Русский), changed while BMeet runs. On a Mac whose language is Russian, Finder's services and the permission questions are in Russian too.
- **The engine in the BMeet window's *Transcribe a File*:** *Automatic* (GigaAM v3 for Russian, Whisper turbo otherwise), *GigaAM v3 (Russian)*, *Whisper large-v3 turbo* or *Whisper small*. Before, the page offered only a Whisper size, so GigaAM could not be chosen or seen, though BMeet already used it for Russian. GigaAM is offered when it is installed and Russian is one of your languages; it sets the language to Russian, and Translate turns it off. The *Options* column says what ran (`GigaAM v3 · ru`).
- **The window follows the features you turn off** (`~/.config/bmeet/dictation`, `meetings`, `in-person-meetings`, `file-transcription`) while it is open: their buttons, menu items and shortcuts (⌘3, ⌘O) go; the Meetings and Transcribed Files pages keep their lists. `bmeet-ctl dictate`, `lang` and `meeting start` then refuse, with status 3.
- **The window and the meeting notifications in Russian,** after `~/.config/bmeet/interface-language`: `system` (the default: the first of the Mac's preferred languages that is English or Russian), `en` or `ru`. After a change, the open window offers **Restart**.
- **`bmeet-ctl settings [PAGE]`** opens BMeet's Settings window on a page (`general`, `features`, `languages`, `shortcuts`, `about`); the window's *Settings…* (⌘,) and *About BMeet* use it. A BMeet.app that does not answer gets the folder `~/.config/bmeet`, as *Settings…* opened before.
- **BMeet around the camera:** on a Mac whose screen has a camera housing, BMeet shows what it is doing around it, the way an iPhone's Dynamic Island shows a call. macOS has no API for this, so BMeet draws its own black shape joined to the housing, above the menu bar, over full-screen apps and on every Space ([docs/macos.md](../docs/macos.md#around-the-camera)).
  - **Closed,** beside the housing: a pulsing red dot and the language while you dictate, a spinner while it transcribes, a red dot and the time of a meeting (a pause sign while it is paused, two people for an in-person one), a spinner and *Speakers* during the speaker pass, a check mark and *Saved* for 5 seconds, a warning sign and *Failed* until you have looked at it. Idle, nothing shows.
  - **The words while you speak** open it downward, underlined while they may change. Where the island shows, it takes the place of the dictation panel at the bottom of the screen; without a housing, with the lid closed or with the island off, the panel shows them as before.
  - **Open,** with a click: the state in full, with **Pause** or **Resume** and **Stop** for a meeting, **Stop** and **Cancel** for a dictation, **Open** for the transcript, **OK** for a failure. A click outside, a second click or Esc closes it. It never takes the keyboard focus or activates BMeet, and BMeet holds Esc only while it is open.
  - *Failed* is a speaker pass that did not tell the voices apart, or a notification of BMeet's that something failed during or right after a meeting (not started, not saved, the recorder stopped). One that comes while the island cannot show (off, the lid closed) is not kept for later.
  - It widens on a spring and grows a little under the mouse; with *Reduce motion* on, it fades.
  - It follows the housing macOS reports, so an older, wider notch gets its own island, and BMeet measures again when the screens change.
  - **Settings → Features → *Show BMeet around the camera*** (`~/.config/bmeet/island`, `on` by default), greyed on a Mac without a housing.

### Changed
- BMeet.app follows the files in `~/.config/bmeet` while it runs (within 2 s), whoever wrote them: the shortcuts in `hotkeys` and a feature's switch no longer need a restart.
- The control socket's `notify` op reads the `ok` that `bmeetmac.notify` sends: a notification that something failed, during or right after a meeting, also shows in the island. BMeet.app reads what `bmeet-meeting stop` prints (the transcript's path) for the island's *Saved*.
- The speech engine of dictation ends after `unload-after` minutes without a dictation (5 by default; it was 15), or right after each one with `0`, and when dictation is turned off.
- **Languages in *Transcribe a File*:** *Detect automatically* and the languages that are on; English is no longer always offered.
- **`bmeet-ctl status`** adds `features` and `interface_language`. Its `live_dictation` is `on` without the file, as BMeet.app reads it (it said `off`), and its `language` is always one of `languages` (the first, when the file names none of them).

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
- `macos/tests/test_settings.py`, through BMeet's own check modes, which show nothing and use a test's own folders: every Settings page in English and Russian fits a 13-inch screen and cuts no text; each feature off removes exactly its menu items and shortcuts; the controls write the files as HotKeys.swift and the rest read them back; shortcuts are registered again with macOS as the switches change (⌃⌥⇧⌘ with F13–F20 only); the control socket refuses what is off; the translations in the bundle.
- `macos/tools/check-strings.py` (CI's lint job): every text of BMeet.app has its Russian line, and no line is left over.
- `macos/tests/test_island.py`, through BMeet's new `--island-check` and a screen and housing given to any check (`--screen`, `--housing`), all off screen: the island centred on this Mac's housing and an older notch, at least as wide and as tall; none without a housing or with the switch off, and the dictation panel instead (a real dictation with the stand-in engine); every state, closed and open, in English and Russian with no text cut short, outside the island or under the camera; the Features page's row; clicks, Esc, hover and the controls, and Esc let go however the open island goes (the state ends, the switch, the housing); the states BMeet derives from `bmeet-ctl status`, its notifications and a stop's output; pictures of every state (`docs/macos-island-*.png`).
- **`bmeet/app/tests/test_bmeet.py`:** every text of the window and of bmeet-meeting's notifications has its Russian line (a test reads the sources); the window in Russian; a notification reaching a stand-in BMeet.app in Russian; the engine choice; the feature switches while the window is open, the Mac menus; `bmeet-ctl settings` with a stand-in control socket in a runtime folder of the test's own.

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
