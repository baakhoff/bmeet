# npu-transcribe

`npu-transcribe` turns audio and video files into subtitles and readable transcripts
with Whisper on the Intel NPU. It falls back to the CPU when the NPU is missing or fails.

```bash
npu-transcribe meeting.m4a                                  # -> meeting.srt + meeting.md
npu-transcribe --model small --format srt,md,txt --out ~/transcripts *.mp4
npu-transcribe --language de --translate interview.ogg      # German speech -> English text
npu-transcribe --help
```

**What it does:**
- **Decoding:** the `ffmpeg` CLI decodes the audio straight into memory (16 kHz mono float32, no temp files).
- **Blocks:** Whisper gets ~2 minute blocks, each cut at the quietest moment, so progress can be shown and memory stays flat for multi-hour recordings.
- **Outputs**, written next to the input (or into `--out`):
  - `NAME.srt`: subtitles, one Whisper segment per cue.
  - `NAME.md`: readable paragraphs with `[hh:mm:ss]` markers about every 30 s.
  - `NAME.txt` (only with `--format …,txt`): the same paragraphs without markers.
- **Existing files are never overwritten:** the outputs become `NAME.2.*`, `NAME.3.*` and so on, unless you pass `--overwrite`.
- **Read-only input folders:** output goes to `~/Transcripts`.
- **History:** every finished file is appended to `~/.local/share/npu-transcribe/history.jsonl`, which the BMeet app and the widget list.
- **Exit status:** 0 all done, 1 some file failed, 2 usage error, 130 Ctrl+C.

**Other ways to start it:**
- **Dolphin:** right-click audio or video → **Transcribe (NPU)**. It runs in a Konsole window and sends a notification when done.
- **BMeet app:** the **Transcribe a file** page has a queue and progress display.

## Models

| | turbo (`whisper-large-v3-turbo-int8-ov`) | small (`whisper-small-int8-ov`) |
|---|---|---|
| Accuracy | better (WER 0.5–1.4 % on the long test clip) | good (1.6–3.0 %) |
| Speed on the NPU | about the same as small (turbo has only 4 decoder layers) | ~26x realtime |
| Speed on the CPU | ~2.5x realtime | ~8x realtime |
| RAM (max RSS) | 3.9 GB | 2.6 GB |
| First NPU compile (then cached in `~/.cache/npu-ov-cache`) | ~4 min | ~1 min |
| Translation (`--translate`) | not trained for it (output stays in the source language) | yes |

**Default model:** turbo when installed (the installer downloads it unless you pass `--no-turbo`), otherwise small. `--translate` uses small unless `--model` is given.

## Benchmarks

Test setup:
- **Hardware and software:** Core Ultra 7 258V on battery (`platform_profile=quiet`), OpenVINO 2026.4.1, NPU driver 1.38.0.
- **Test audio:** synthetic espeak-ng speech, no real recordings.
  - `en_long.mp4`: 164 s of speech.
  - Short clips: 5–8 s each in en, ru, de and fr.
- **Timing:** time is decode plus transcription, without model load.
- **Shared NPU:** marked rows ran while another program was also using the NPU.

| Device / model | en_long 164 s | short clips | Load (cached) |
|---|---|---|---|
| NPU small | **6.1 s (26.8x)**, WER 1.6 % | 0.5 s | 2.8 s |
| NPU turbo | **6.5 s (25.4x)**, WER 0.5 % | 0.7 s | 4.0 s |
| NPU small, shared NPU | 19.9 s (8.2x), WER 3.0 % | 1.2–2.0 s | 2.3 s |
| NPU turbo, shared NPU | 24.3 s (6.7x), WER 1.4 % | 2.0–3.1 s | 2.5 s |
| CPU small | 20.9 s (7.9x), WER 2.4 % | 2.6–2.8 s | 1.6 s |
| CPU turbo | 63.7 s (2.6x), WER 1.1 % | 8.5–9.2 s | 2.1 s |

**Short clips are dominated by fixed costs:** every Whisper call pads its input to a 30 s window, so CPU turbo needs ~9 s for any clip under 30 s.

**Re-running the benchmark:**
```bash
transcribe/tests/run-tests.sh /tmp/npt-work "NPU CPU"
```
- It writes only under the given directory.
- `tests/check_output.py` validates every `.srt`: numbering, timing syntax, order and empty cues. It also computes the word error rate against the reference text.

## Findings and workarounds

1. **NPU language auto-detection is sticky** (openvino-genai 2026.4.1, both models):
   - **The bug:** with no language set, only the *first* `generate()` of an NPU `WhisperPipeline` detects the language. Later calls silently reuse the previous call's language.
   - **Example:** a Russian file after an English one came out as an English translation.
   - **Not affected:** a forced language works on every call, and the CPU pipeline detects correctly.
   - **Workaround in npu-transcribe:** with `--language auto`, the NPU pipeline is reloaded before each file (~2.5 s from the compile cache). The language detected on the first non-silent block is then pinned for the rest of that file, which is also what the openai-whisper CLI does.
   - Pass `--language` to skip the reload in big batches.
2. **OpenVINO's `AUTO` device runs Whisper on the CPU:** encoder time was 1464 ms versus 67 ms on `NPU`. So `--device AUTO` means "NPU if present, else CPU" and doesn't use the AUTO plugin.
3. **OpenVINO telemetry is on by default:**
   - **What happens:** `import openvino` loads Intel's telemetry module. Without a consent file it counts as opted *in*, sends a Google Analytics event from a helper process and writes `~/intel/openvino_ga_cid`.
   - **What npu-transcribe does:** it makes the module unimportable (`sys.modules["openvino_telemetry"] = None`), and OpenVINO then falls back to its built-in no-op stub. The installer does the same for its one import check.
   - **To opt out for every tool:** `~/.local/share/bmeet/venv/bin/opt_in_out --opt_out`.
4. **Whisper invents text for silence:** for example "Thank you." over trailing digital silence. Blocks and segments whose peak level is below -50 dBFS are skipped or dropped.
5. **Audio that starts after the video:** without a fix, cues came out early by the gap. ffmpeg runs with `aresample=async=1:first_pts=0`, which pads the start (and any gaps) with silence.
6. **File names with a colon**, such as `talk: part 1.ogg`, were read by ffmpeg as a protocol. Inputs are passed as `file:PATH`.
7. **NPU failures fall back to the CPU:**
   - **Load failures** (driver missing, compile error, out of memory) switch to the CPU.
   - **Inference failures** retry the failed block on the CPU and continue there. This was tested by injecting errors into the load step, into every `generate()`, and into one `generate()` in the middle of a file.
   - **openvino.genai #4222** ("invalid unordered_map key") didn't occur with these exports.

## Edge cases checked

| Input | Result |
|---|---|
| video without an audio stream | `ERROR: no audio stream in this file`, other files continue, exit 1 |
| 0-byte `.mp3`, a text file named `.mp3` | `ffmpeg failed: … Invalid data found when processing input`, exit 1 |
| missing file | `ERROR: missing.wav: no such file`, exit 1 |
| 40 s of digital silence | no Whisper call, empty `.srt`, `.md` says "(no speech detected)" |
| 20 s silent intro | first cue at 20.3 s |
| two audio tracks | the first one is used |
| `name with spaces ä & 'quote'.flac` | works (also through Dolphin's Konsole action) |
| Ctrl+C in the middle of a file | exit 130 within 0.2 s, no partial outputs, no temp files, no ffmpeg left |
| HLS playlist pointing at `http://` | refused by ffmpeg (protocol not on the whitelist) |
