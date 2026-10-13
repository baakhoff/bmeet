# npu-transcribe

`npu-transcribe` turns audio and video files into subtitles and readable transcripts on
the Intel NPU: Russian with GigaAM v3, every other language with Whisper. It falls back to
the CPU only when the NPU is missing or fails.

```bash
npu-transcribe meeting.m4a                                  # -> meeting.srt + meeting.md
npu-transcribe --model small --format srt,md,txt --out ~/transcripts *.mp4
npu-transcribe --language de --translate interview.ogg      # German speech -> English text
npu-transcribe --speakers interview.m4a                     # Speaker 1: ... Speaker 2: ...
npu-transcribe --meeting DIR --output MEETING.md            # BMeet's meeting speaker pass
npu-transcribe --meeting DIR --output MEETING.md --voices ~/.local/share/bmeet/voices
npu-transcribe --help
```

**What it does:**
- **Decoding:** the `ffmpeg` CLI decodes the audio straight into memory (16 kHz mono float32, no temp files).
- **Blocks:** Whisper gets blocks of at most 30 s, one Whisper window each, cut at the quietest moment, so memory stays flat for multi-hour recordings. See *Block length* below for why not longer.
- **Stock phrases:** a segment that is only one of the phrases Whisper writes over silence ("Продолжение следует…", "Субтитры сделал DimaTorzok", "Thanks for watching!", …) is dropped.
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

## Russian: GigaAM v3

For Russian, npu-transcribe uses [GigaAM v3](https://github.com/salute-developers/GigaAM) (Sber, MIT; the *e2e CTC* variant, which writes punctuation and capitals). Whisper still detects the language, and it handles every other language and `--translate`. `--engine whisper` keeps Whisper for Russian too.

**Measured** on 40 utterances (16 min, 2,286 words) of the Russian IT podcast [Podlodka](https://huggingface.co/datasets/bond005/podlodka_speech) (its test and validation splits). WER is on lower-case words without punctuation, ё = е.

| model | WER clean | WER through 16 kbit/s Opus | English terms right | speed | runs on |
|---|---|---|---|---|---|
| **GigaAM v3 CTC, prepared for the NPU** | **6.5 %** | 7.1 % | 4 / 40 | **215x** | NPU |
| GigaAM v3 CTC (sherpa-onnx, int8) | 6.4 % | 6.6 % | 4 / 40 | 42x | CPU |
| GigaAM v3 RNNT (sherpa-onnx, int8) | 7.2 % | 7.4 % | | 16-23x | CPU |
| Whisper large-v3 | 7.1 % | 8.5 % | 22 / 40 | 3.9x | NPU |
| Whisper large-v3-turbo (before 0.4) | 8.2 % | 7.9 % | 18 / 40 | 14x | NPU |
| NVIDIA Parakeet-TDT-0.6B v3 | 10.1 % | 11.6 % | | 20x | CPU |
| Whisper small (voxtype's live meeting text) | 14.7 % | 16.6 % | | 10x | NPU |

**Trade-offs:**
- **Better:** GigaAM gets Russian words right that Whisper misses ("тимлидом" where Whisper wrote "тем лидом", "чатик" where it wrote "чат").
- **Worse:** English terms inside Russian speech come out mangled, for example "Superviselening" or "Perfomance".
- **Through the call codec:** the margin over Whisper turbo shrinks to 7.1 % vs 7.9 %.

**Making it run on the NPU.** `transcribe/prepare-models` does this once, at install time (~7 s, plus a ~1 min NPU compile).
- **The starting point:** sherpa-onnx's export is an onnxruntime "dynamic int8" model. On the NPU it wrote garbage (WER 99 %).
- **1. Run-time quantisation:** it quantises its activations at run time (DynamicQuantizeLinear, then MatMulInteger/ConvInteger), which falls apart in fp16. The int8 weights are turned back into float weights ((w − zero point) × scale), which gives the same model without the run-time rounding. That brought it to WER 34 %.
- **2. An NPU compiler bug in the rotary position step:** `Add(x·cos, rotate(x)·sin)` on (T, 1, heads, 48) tensors comes out wrong (0.76 relative error) although both products are right, and reshaping it does not compile.
  - The step feeds only the query and key projections, so it is folded into them: (x·cos + rotate(x)·sin)·W = [x·cos, x·sin′]·[W; P·W], with P the signed half-swap of rotate_half.
  - The rotary tables come from constant folding at the fixed 30 s input (750 positions).
- **The result:** WER 6.5 %, the same as on the CPU, at ~215x realtime on the NPU. The model is saved with fp16 weights (~480 MB in `~/.local/share/npu-models/gigaam-v3-ctc-ru`).
- **At run time:** the features (64-band log-mel, n_fft 320, hop 160, like `gigaam.preprocess`) are computed in numpy, and the output is decoded greedily. Words get their times from the 40 ms output frames.

## Block length

npu-transcribe used to give Whisper 2-minute blocks and let OpenVINO GenAI chain the 30 s windows inside them. On a real Russian call (10 min, the system-audio track, turbo on the NPU) that cost a lot:

| block | punctuation marks per 100 words | segments without any | speed |
|---|---|---|---|
| 120 s | 18.4 | 19 of 74 | 33 s |
| **30 s** | **29.0** | **11 of 84** | 33 s |

- **Same for Whisper small (20.1 vs 29.2):** word timestamps made no difference.
- **What went wrong with 2-minute blocks:** after its first window the text often ran on without commas, full stops or capitals for minutes. It also misheard more: "если учили здесь минут" for "если через 10 минут", "наличку" for "аналитику", "поэмов" for "ПМов".
- **Why not small:** with 30 s blocks small punctuated as well, but it garbled more words than turbo.
- **Why no prompt:** a punctuated Russian `initial_prompt` removed the unpunctuated segments. It also left out filler words ("типа", "короче"), and over a quiet "Угу" it wrote "Да, всё понятно, всё понятно, …". So it isn't used.
- **English:** 30 s blocks kept the long English test clip at 0.5 % WER, and it ran faster (30.7x vs 25.4x).

## Speakers

`--speakers` labels every line with the voice that says it (**Speaker 1**, **Speaker 2**, …,
numbered in the order they first speak); `--num-speakers N` fixes the count when you know it.
`--meeting DIR` is the same for a BMeet meeting recording: the microphone track is **You**,
the voices in the system audio become **Person 1**, **Person 2**, …

**How it works:**
1. **Who speaks when**, on the NPU (OpenVINO; until 0.3 this ran on the CPU through sherpa-onnx):
   - **Speech per window:** pyannote **segmentation-3.0** looks at 10 s windows, every 5 s, and finds up to 3 voices in each (78 ms per window on the NPU).
   - **Fingerprints:** NVIDIA **TitaNet-small** fingerprints each voice's clean speech in each window, 16 per NPU call (13 ms each).
   - **Clustering:** the fingerprints are clustered by average-linkage agglomerative clustering, threshold 0.4 on cosine distance (env `NPU_SPEAKER_THRESHOLD`), or into exactly `--num-speakers` clusters.
   - **Turns:** they are rebuilt pyannote's way. Each voice's activity is added up over the overlapping windows, the number of voices per frame is estimated, and that many of the most active ones are kept. Turns under 0.3 s are dropped, and gaps under 0.5 s are closed.
2. **Words, not sentences:** GigaAM's words come with their times from its output frames. Whisper runs with word timestamps (a separate NPU compile of ~1 min, cached; each block takes ~8 % longer). Every word goes to the voice that speaks during it, so a sentence that crosses a change of speaker is split there. A change shorter than 0.6 s inside a sentence is treated as noise.
3. **Echo (meetings):** without headphones the microphone also hears the other side, and Whisper writes their words into your track too, often in the same sentence as your own.
   - First, how loud the system audio comes back through the microphone is measured: the median mic/remote ratio while the other side talks.
   - A word on your track is cut when the microphone is less than 2x louder than that predicted echo at that moment.
   - With headphones the predicted echo is ~0, so nothing is cut. When you talk over the other side, your voice is far louder than the echo and stays.
4. **Output:** Speaker labels in `.srt` cues (`Speaker 1: …`), `.md` and `.txt` paragraphs (which never mix two speakers), and a `speakers` count in the history line. Meetings get voxtype's Markdown layout: a `### Person 1` heading per change of speaker and a `*[mm:ss]*` mark per paragraph.

**Cleaning up the clustering on real calls.** Real calls are harder than clean test voices: background noise, the conferencing app's noise suppression, laughter and codec glitches. On one 49-minute Russian 1:1 call, the clustering made 30 "voices": one real person, plus fragments of 1-114 words. Of those fragments, the ones long enough to compare were 0.14-0.87 similar to that person. So after the clustering:
- **Fragments:** a voice with under 15 s of speech, and under 5 % of the system audio's speech, joins the voice it sounds most like. If it has no turn long enough to compare, it joins the voice nearest in time.
- **One person split in two:** a voice at least 0.65 similar to a bigger voice joins it. Two different readers were never more than 0.41 similar; the two people on a real call scored 0.41.
- **Unassigned words:** short replies ("Угу", "Да", "Пока") often get no turn of their own. They go to the nearest speaker within 30 s, not *Remote*.

On 10 minutes of a real three-person call, the old pass made You, Remote and Person 1-7. The new one makes You, Person 1 and Person 2, taking turns as in the conversation.

**Memory:** the whole recording is held as one array (~230 MB per hour), because diarization needs all of it at once.

**Choosing the voice model.** All four of sherpa-onnx's general speaker-embedding exports were tested on 36 LibriSpeech utterances by 9 readers (sent through 16 kbit/s Opus, like a VoIP call). The columns show the cosine similarity between two utterances:

| model | same person (mean, lowest) | different people (mean, highest) | equal error rate |
|---|---|---|---|
| **NeMo TitaNet-small** (`nemo_en_titanet_small.onnx`, 40 MB) | 0.76, 0.59 | 0.10, 0.41 | **0 %** |
| 3D-Speaker ERes2Net base (zh-cn) | 0.62, 0.29 | 0.17, 0.49 | 7 % |
| WeSpeaker ResNet34-LM (VoxCeleb) | 0.81, 0.65 | 0.58, 0.84 | 14 % |
| 3D-Speaker CAM++ (VoxCeleb) | 0.50, 0.01 | 0.39, 0.95 | 43 % |

**Threshold.** Four synthetic calls (2-4 readers, 2.5-4 min each, 24-40 turns, through Opus) through the whole NPU pipeline. Each cell shows people found and the share of speech given to the right person:

| threshold | 3 readers | 2 readers | 4 readers | 4 readers |
|---|---|---|---|---|
| 0.3 | 3, 99 % | 2, 100 % | 4, 95 % | 4, 94 % |
| **0.4** | **3, 99 %** | **2, 100 %** | **4, 97 %** | **4, 95 %** |
| 0.5-0.65 | 3, 93 % | 2, 100 % | 4, 97 % | 4, 95 % |
| 0.7 | 2, 79 % | 2, 100 % | 4, 97 % | 4, 95 % |
| 0.85 | 2, 79 % | 2, 100 % | 3, 87 % | 3, 77 % |

This ran at ~110x realtime on the NPU. Under sherpa-onnx's CPU diarization (0.2-0.3, its own threshold scale) the right range was 0.8-0.9.

**Making the speaker models right on the NPU.** Both models load in OpenVINO, but:
- **Segmentation** runs as it is: its speech decisions match the CPU's in 99.8 % of the frames. Batching it does not compile in reasonable time, so it runs one window at a time.
- **TitaNet's** statistics pooling underflows in the NPU's fp16. Attention weights ~1/400 times squared deviations ~1e-3 make products ~1e-6, and fingerprints came out only 0.67 similar to the CPU's. The pooling is fed the features ×64, and its means and standard deviations are scaled back. That's the same maths, now 0.9993 similar.
- **TitaNet's features** (NeMo's: pre-emphasis 0.97, 25 ms Hann window, Slaney mel, per-feature normalisation) are computed in numpy. Fingerprints from the same reader across recordings stay ≥ 0.79 similar, and others ≤ 0.39.

**End to end** (`--meeting` on the NPU): a 2:55 meeting (three readers in the system audio, a fourth on the microphone, the system audio leaking into the microphone at -22 dB, a pause in the middle) took 24 s.
- All 3 people were found.
- 100 % of the text was under the right label.
- 244 echo words were cut, and none of yours.

`tests/meeting_test.py` builds the same kind of meeting from espeak-ng voices in Russian and English and checks it. It uses `--engine whisper`, because GigaAM, trained on real speech, writes almost nothing for espeak's robotic Russian.

**Remembered voices** (`--meeting … --voices DIR`; BMeet passes it when *remember voices* is on):
- **Fingerprints:** each voice's fingerprint is the duration-weighted mean of the TitaNet embeddings of its longest turns, up to 60 s of speech, at unit length. They go to `DIR/meetings/<transcript>.json` (0600, folders 0700). Naming people in the BMeet app copies theirs to `DIR/voices.json`, at most 10 per person.
- **Recognising, turn by turn:** a turn (or, inside a turn of 4 s or more, a 2 s window every 0.5 s) belongs to a remembered person when its embedding is at least 0.55 similar to their voice and 0.1 more similar than to anyone else remembered.
  - Long turns are split where the recognised person changes for 1.5 s or more. Two similar voices can share one turn in the segmentation.
  - A voice whose recognised speech is mostly one person's takes their name. Their turns inside another voice (two people the clustering merged) move to them.
  - Everyone else is numbered as before, *Person 1*, *Person 2*, …, skipping the names.
- **Why 0.55:** fingerprints of the same LibriSpeech reader from different recordings (through Opus) were at least 0.79 similar, and those of different readers at most 0.39. One utterance against a fingerprint scored at least 0.63 for the same reader and at most 0.39 for others.
- **Test:** in `tests/meeting_test.py --remember`, a second meeting with new sentences, where the people speak in another order, came out as Anna, Boris and Viktor. That was 100 % in English and 90 % in Russian, where espeak's synthetic Russian makes Whisper's word timing blur at one change of speaker. Without the window check, two of espeak's similar voices shared one turn and one name.
- **Language:** a meeting's language is detected on the system audio and used for your microphone too. Detecting it again on a track that may hold little speech of your own went wrong in the tests, and Whisper then translated.

**Speed and CPU:** a 5-minute Russian meeting (real podcast speech in both tracks, echo included) took 10 s.
- **CPU time:** 4 s of user and 4 s of system time in total, mostly loading the models.
- **Why so little:** numpy is kept to one thread. Its BLAS would otherwise start a thread per core for the small matrix products of the features, and they busy-wait: that was 26 s of CPU for the same meeting.
- **Per hour of a Russian meeting:** about 2 minutes. GigaAM takes ~30 s for both tracks, and the speakers ~1.5 min.
- **Other languages:** Whisper turbo is slower, at ~15x realtime per track.

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
