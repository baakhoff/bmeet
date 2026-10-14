# Text while you speak (live dictation)

voxtype, which BMeet uses for dictation, types the text only once you stop. BMeet's own dictation shows it while you speak instead: in the text field you are in, on the NPU, with GigaAM v3 for Russian and Whisper for the other languages. It is a prototype, **off by default**.

```bash
echo on > ~/.config/bmeet/live-dictation       # the text in the field you are in
echo panel > ~/.config/bmeet/live-dictation    # a panel at the bottom; KWin's settings untouched
rm ~/.config/bmeet/live-dictation              # back to voxtype's dictation
```

Meta+H, Meta+Alt+H, the widget and the app then go to BMeet's dictation (`bmeet-ctl dictate` → `bmeet-dictate`); with the file gone or saying `off` they go to voxtype as before. The language is the dictation language you already have (Meta+Shift+H).

## What you see

- **In a text field of a Wayland program** (tested: Qt, GTK 3, Chrome 155 and Konsole; Firefox and Telegram use the same Wayland text input, not tried): the words still being worked out are shown underlined in the field, and each sentence goes into the field for good once you pause after it. When you stop, the rest follows: ~0.2 s later in Russian, ~0.7 s in English. Nothing goes through the clipboard and no key is pressed, so Klipper's history stays clean, Konsole works (no Ctrl+V needed), and holding Meta when the text arrives doesn't matter.
- **Elsewhere** (X11 programs running through Xwayland, here Bitwarden; or no text field has the focus): a panel at the bottom of the screen shows the text, final words in white and the rest in grey. When you stop, the text is pasted at the cursor once, as voxtype does it (wl-copy, then Ctrl+V through ydotool).
- **Meta+Alt+H** (clipboard only): the panel, and at the end the text goes to the clipboard.
- **With `panel`:** always the panel, and the paste at the end.
- **Cancel** (`bmeet-ctl cancel`): sentences already in the field stay there; the underlined rest goes.

## What it changes on your system

- **`on`, for the length of each dictation:** KWin runs one input method at a time: the one in System Settings → Keyboard → Virtual Keyboard, which here is Plasma's on-screen keyboard, switched off. While you dictate, BMeet's input method (`bmeet-im`) takes that place and KWin's virtual keyboard toggle is switched on. When the text is in, BMeet puts back what was there, switched on or off as it was.
  - **How:** `[Wayland] InputMethod` in `~/.config/kwinrc` (written with `kwriteconfig6 --notify`, which KWin follows), and the `enabled` and `active` properties of KWin's `/VirtualKeyboard` on D-Bus.
  - **Your setting is safe:** it is written to `~/.local/state/bmeet/input-method.json` before anything changes, and that note is only deleted once it is put back. If BMeet crashes or the laptop reboots mid-dictation, the next start of `bmeet-dictate` puts it back.
  - **The cost:** during a dictation the on-screen keyboard can't come up (in tablet mode, tapping a field shows none until you stop).
- **`panel`:** nothing in KWin.
- **Both:** `bmeet-dictate` runs as the user unit `bmeet-dictate`, started by the first Meta+H. It keeps the model for your language loaded: GigaAM ~0.6 GB, Whisper turbo ~3.8 GB. voxtype's daemon keeps running for meetings. The microphone (`pw-record`, named *BMeet dictation*) is only open while you dictate.

## How it works

```
Meta+H ─ bmeet-ctl dictate ─ bmeet-dictate (service, PyQt6) ─┬─ npu-transcribe --dictate (venv; the model on the NPU)
                                                             │     pw-record → draft every 1 s / final sentences → JSON lines
                                                             ├─ KWin: input-method slot taken / given back
                                                             │     bmeet-im (started by KWin) → preedit + commit into the field
                                                             └─ panel (layer-shell, never focused) + wl-copy/ydotool where no field takes it
```

- **The engine** (`npu-transcribe --dictate`) records the default microphone with `pw-record` (16 kHz mono, as voxtype records it):
  - **Drafts:** what you said since the last final words is transcribed again every 1 s with sound (Whisper: every 2 s), and once more 0.3 s after you stop talking, so your last words show. GigaAM's NPU input is fixed at 30 s, so a pass costs the same ~85 ms of NPU time and ~5 ms of CPU however long the stretch is.
  - **Final words:** a stretch becomes final when its draft ends a sentence (. ! ? …) and 0.8 s of quiet follow, or, after 20 s without that, up to its quietest 100 ms in the last 8 s. After such a cut the next part loses the capital the model gives every first word. Only the words after the last final ones are redone, so a long dictation costs no more per second than a short one, and nothing has to be kept past 30 s.
  - **At the stop:** only the words since the last final ones are left to transcribe.
  - Sound is told from quiet the way the live meeting transcript does it: 100 ms frames, 12 dB above the quietest tenth.
- **The input method** (`bmeet-im`, stdlib Python, started by KWin ~25-45 ms after the switch) speaks `zwp_input_method_v1` on the Wayland connection KWin gives it.
  - **The focused field:** a field that already has the focus only takes an input method that starts after it once KWin's `forceActivate` is called. That is done only when KWin reports that the focused window supports text input. On an X11 window, forcing it makes KWin type the text as key presses of the current layout, and letters the layout doesn't have are lost: in the test, a US layout kept only the spaces and commas of Russian text.
  - **What it sends:** final words go in with `commit_string`, the draft with `preedit_string`. It tells `bmeet-dictate` whether a field has it, so the text goes by panel and paste where none does.
- **The panel** is a LayerShellQt surface, called through its C++ API with `ctypes` since it has no Python binding: anchored to the bottom, in the top layer, with no keyboard interactivity, so it never takes the focus from the window you dictate into.

## The options compared

The goal: the text while you speak, Russian first, models on the NPU only, little CPU. Four ways were measured; the engine numbers are from `transcribe/tests/dictate_test.py` (below).

| | (a) input method, slot taken per dictation (**chosen**) | (b) panel, paste at the end | (c) paste each finished sentence | voxtype's `[openvino] streaming` |
|---|---|---|---|---|
| where the text shows | in the field (underlined draft) | panel at the bottom; field at the end | field, a sentence at a time; nothing of the sentence being said | field, in bursts |
| word → text (median) | Russian 0.5 s, English 1.2 s | same | a sentence 2.3 s (Russian; 90 %: 7.6 s) / 1.1 s (English; 90 %: 8.7 s) after its last word | ~2 s Russian, ~1.6 s English |
| CPU per minute of speech (today: ~0.4 s for the model) | Russian 0.4 s, English 2.3 s | + ~0.1 s for the panel | same as (a) | 5-6 s |
| word errors Russian / English (today 16.2 % / 2.7 %) | 7.3 % / 1.1 % | same | same | whisper-small's, plus repeats |
| past 30 s of speech | as good (Russian 7.4 %, English 1.4 %) | same | same | audio dropped past ~25 s |
| clipboard / Klipper | untouched in Wayland fields | one entry, as today | one entry per sentence (unless marked secret: needs its own clipboard client, `wl-copy` offers one type) | one entry per fragment, last words lost |
| X11 programs (Xwayland) | panel + paste | works | works | works |
| terminals | Konsole: works (input method) | Ctrl+V doesn't paste in Konsole | same as (b) | same as (b) |
| your system | KWin's input-method slot, per dictation, put back after | nothing | nothing | nothing |

- **(a) with the slot taken for good** (BMeet as *the* input method in System Settings) was the first idea. It works the same way, but loses the on-screen keyboard for good. Taking the slot only while dictating keeps it, and that is what was built.
- **(b)** is the safe fallback and also available on its own (`panel`). Wayland doesn't tell a normal window where the text cursor is, so the panel can't sit next to it.
- **(c)** was left out. It shows only finished sentences, and late: in fluent speech few sentences are followed by the 0.8 s pause that makes them final, so most text would arrive in 20 s pieces (2.5 per minute in the Russian test). A key press goes to whatever has the focus at that moment, so switching windows or typing mid-dictation scatters sentences. It also needs a clipboard client of its own to keep Klipper clean, and still can't paste into Konsole.
- **voxtype's streaming** was measured and rejected earlier ([voxtype.md](voxtype.md#caveats)).

## Measured

### The engine (`transcribe/tests/dictate_test.py`)

Each dictation is fed to `npu-transcribe --dictate-file --simulate` as if it were spoken: the clock skips the waiting but not the work, so the text comes as late as it would while you speak. Word errors are on lower-case words without punctuation, ё = е.
- **word → text:** from the end of a word (the model's own word times on the whole recording) to the first text that shows it.
- **Today:** whisper-small on the whole recording, as voxtype's OpenVINO engine does it.
- **CPU:** the engine's, during the dictations, per minute of speech. Loading the model is not counted (the service keeps it loaded).

**Russian:** Podlodka podcast clips (`bond005/podlodka_speech`): 20 clips of 8-59 s and two of ~90 s, 22 dictations, 11.2 min. Core Ultra 7 258V, NPU driver 1.38.0, OpenVINO 2026.4.1.

| | word errors | over 30 s | word → text | stop → text | CPU / min |
|---|---|---|---|---|---|
| **live, GigaAM v3, draft every 1 s** | **7.32 %** | 7.37 % | 0.49 s (90 %: 0.96 s) | 0.19 s (max 0.28 s) | 0.40 s |
| live, draft every 1.5 s ¹ | 7.32 % | 7.37 % | 0.76 s (90 %: 1.48 s) | 0.19 s | 0.17 s |
| GigaAM on the whole recording | 7.13 % | 7.12 % | – | – | 0.03 s |
| today: whisper-small, whole recording | 16.21 % | 15.39 % | – | 0.66 s (max 3.29 s) | 0.42 s |

¹ An earlier run inside one process, where drafting every 1 s measured 0.29 s of CPU per minute (0.40 s in the test's own process per dictation).

**English:** LibriSpeech test-clean, six readers: 47 utterances of 4 s and more, and one dictation of ~45-60 s per reader; 53 dictations, 12.6 min. LibriSpeech is clean read speech, so all error rates are low.

| | word errors | over 30 s | word → text | stop → text | CPU / min |
|---|---|---|---|---|---|
| **live, Whisper turbo, draft every 2 s (as built: no timestamps)** | **1.11 %** | 1.37 % | 1.23 s (90 %: 2.04 s) | 0.68 s (max 1.07 s) | 2.25 s |
| live, Whisper turbo, every 2 s, with timestamps | 1.01 % | 1.37 % | 1.28 s (90 %: 2.11 s) | 0.77 s (max 1.14 s) | 2.99 s |
| live, Whisper turbo, every 3 s, with timestamps | 1.06 % | 1.37 % | 1.64 s (90 %: 2.77 s) | 0.73 s (max 1.11 s) | 2.44 s |
| live, Whisper small, every 2 s, with timestamps | 3.42 % | 3.60 % | 1.03 s (90 %: 1.90 s) | 0.38 s | 2.67 s |
| Whisper turbo on the whole recording | 1.46 % | 2.11 % | – | – | – |
| today: whisper-small, whole recording | 2.72 % | 3.60 % | – | 0.24 s (max 1.79 s) | 0.40 s |

- **Live is as accurate as the whole recording.** In Russian the live text has 0.2 points more errors than GigaAM on the whole recording. In English it even had fewer, mostly on the long dictations (1.37 % against 2.11 %), which the whole-recording pass cuts into 30 s windows. Against today's dictation Russian has less than half the errors, mostly because GigaAM is the better Russian model ([npu-transcribe.md](npu-transcribe.md#russian-gigaam-v3)).
- **Past 30 s nothing is lost:** the words since the last final ones are never more than ~20 s, and nothing queues up behind a slow pass.
- **CPU:** Russian costs as much as today's dictation. English costs ~5-6x. A Whisper pass costs CPU in proportion to its audio even when nothing is said (turbo: 17, 41 and 75 ms for 3, 10 and 20 s of near-silence, against ~0.5 ms per word): OpenVINO GenAI computes the audio features on the CPU. Timestamps add a quarter, and dictation doesn't need them, so they are off (2.99 → 2.25 s per minute). Drafting every 3 s instead of 2 saves less than it costs in delay. Whisper small is no cheaper than turbo and makes three times the errors, so English uses turbo.

### Getting the text into the field

Measured in a private KWin 6.6.6: `bmeet/dictate/tests/kwin-nested.sh` runs it with its own Wayland socket, D-Bus and kwinrc, headless and drawn in software. Test fields log every input-method event.

| | |
|---|---|
| input method → field, per update | 0.3-1.5 ms (Qt 0.4 ms, GTK 3 0.4 ms, Chrome 0.3-1.5 ms) |
| taking the slot → the focused field has the input method | ~50 ms (KWin starts `bmeet-im` ~25-45 ms after the kwinrc change) |
| giving it back | ~15 ms; the keyboard that was there comes back switched on or off as it was |
| Qt 6 (Wayland) | draft and final text: yes |
| GTK 3 (Wayland) | yes |
| Chrome 155 (native Wayland, its default here) | yes, **no `--enable-wayland-ime` needed** (tested without, with text-input v1 and with v3) |
| X11 program through Xwayland | no input method (forcing it types key presses: letters outside the layout are lost) → panel + paste |
| panel | ~2 ms of CPU per update (~0.1 s per minute at ~60 updates); the field keeps the focus (KWin: `wantsInput` false) |

Programs here: Chrome, Telegram and the Claude app run on Wayland; Bitwarden is the only X11 one.

## Limits and follow-ups

- **English CPU** (~2.3 s per minute of speech) is the one number well above today's. Possible next steps:
  - Compute Whisper's audio features once per dictation in numpy, and drive the NPU encoder and decoder directly instead of through OpenVINO GenAI's pipeline.
  - Port an English CTC model to the NPU the way GigaAM was (NVIDIA's Parakeet CTC), which would make English as cheap as Russian.
- **English terms in Russian:** the live text doesn't run Whisper for them (`--no-english-terms` behaviour). That would load a second model (~3.8 GB).
- **One language per dictation:** the one set with Meta+Shift+H, as with voxtype.
- **Typing while you dictate:** not tested. Keys go to the field as usual (the input method takes none).
- **Switching windows mid-dictation:** final sentences stay where they went. KWin activates the input method for the newly focused field (or the panel takes over); not tested with real focus changes.
- **Electron programs:** not tested. They can take the input method only when they run on Wayland, and older Chromium versions needed `--enable-wayland-ime` for it; otherwise they get the panel and the paste.
- **Sounds:** none. voxtype plays a start and a stop sound; here the underline in the field (or the panel) shows that it listens.
- **Not yet used in a real desktop session:** everything above ran in the private KWin, on recorded speech. The microphone path was tested with a stand-in for `pw-record`.

## Tests

```bash
python3 -I transcribe/tests/test_dictation.py -v      # the engine, offline (numpy; a fake model, no NPU)
python3 -I bmeet/dictate/tests/test_dictate.py -v      # bmeet-dictate + bmeet-im: offscreen, and in a private KWin
~/.local/share/bmeet/venv/bin/python -I transcribe/tests/dictate_test.py WORKDIR --language ru   # on the NPU
~/.local/share/bmeet/venv/bin/python -I transcribe/tests/dictate_test.py WORKDIR --language en
```

- **Nothing is typed into your windows and your clipboard is never touched.** `BMEET_DICTATE_OUTPUT=file:…` writes what would be pasted to a file.
- **The offscreen tests can't reach your KWin:** their D-Bus address is a dead one.
- **The KWin tests run against their own KWin:** its own D-Bus and kwinrc; services its D-Bus starts are stopped afterwards.
- **`dictate_test.py` downloads its clips once** into WORKDIR (public datasets, never kept in the repository).
