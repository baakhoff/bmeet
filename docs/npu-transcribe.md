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
- **Worse:** English terms inside Russian speech come out mangled, for example "Superviselening" or "Perfomance". Since 0.5.2, Whisper fixes most of them (see the next section).
- **Through the call codec:** the margin over Whisper turbo shrinks to 7.1 % vs 7.9 %.

**Making it run on the NPU.** `transcribe/prepare-models` does this once, at install time (~7 s, plus a ~1 min NPU compile).
- **The starting point:** sherpa-onnx's export is an onnxruntime "dynamic int8" model. On the NPU it wrote garbage (WER 99 %).
- **1. Run-time quantisation:** it quantises its activations at run time (DynamicQuantizeLinear, then MatMulInteger/ConvInteger), which falls apart in fp16. The int8 weights are turned back into float weights ((w − zero point) × scale), which gives the same model without the run-time rounding. That brought it to WER 34 %.
- **2. An NPU compiler bug in the rotary position step:** `Add(x·cos, rotate(x)·sin)` on (T, 1, heads, 48) tensors comes out wrong (0.76 relative error) although both products are right, and reshaping it does not compile.
  - The step feeds only the query and key projections, so it is folded into them: (x·cos + rotate(x)·sin)·W = [x·cos, x·sin′]·[W; P·W], with P the signed half-swap of rotate_half.
  - The rotary tables come from constant folding at the fixed 30 s input (750 positions).
- **The result:** WER 6.5 %, the same as on the CPU, at ~215x realtime on the NPU. The model is saved with fp16 weights (~480 MB in `~/.local/share/npu-models/gigaam-v3-ctc-ru`).
- **At run time:** the features (64-band log-mel, n_fft 320, hop 160, like `gigaam.preprocess`) are computed in numpy, and the output is decoded greedily. Words get their times from the 40 ms output frames.

## English terms in Russian speech

GigaAM garbles English words or writes them in Cyrillic: "Data Dog", "worниiнг statemont", "Рuби, Пиtн", "жаво", "обзервабилити". Whisper hears most of them, but makes more errors in the Russian around them. So for Russian, `english_terms()` combines the two:

1. **Where:** the sentences in which GigaAM wrote Latin letters (it heard something English) go to Whisper turbo too. Sentences less than 2 s apart are sent together, up to 28 s. Whisper is forced to Russian and gives word timestamps. On the test set that was 12 % of the audio. Also sending sentences with English-looking Cyrillic words ("-инг", "-шн", "-айн"…) needed 64 % of the audio for a few more terms.
2. **What:** a run of Whisper words in Latin letters replaces the GigaAM words said at the same time that sound like it. "Sound like" is checked by transliterating both to Latin and needing a difflib ratio of 0.4, or 0.7 for words of 1–3 letters. Whisper's pieces ("one", "-on", "-one") are joined first.
3. **Russian endings stay:**
   - after a hyphen: "GPU-шки", "Go-разработчиков";
   - attached to a Cyrillic word: "ворнинги" → "warning-и", when the stem sounds more like Whisper's word than the whole word does.
4. **Left alone:** words that do not sound alike ("там, не знаю" next to "GPU"), and words the two models agree on (GigaAM's capitals stay).

**Measured** on the Podlodka clips with Latin-letter references: 87 unique clips, 33 min, 4,744 words, 98 English terms (`tests/english_terms_test.py`). WER is on lower-case words. A term counts when the output has the Latin word, endings aside.

| | WER | English terms right |
|---|---|---|
| GigaAM (with tidying, as before) | 7.05 % | 22 / 98 |
| Whisper turbo alone | 8.2 % | 57 / 98 |
| **GigaAM + Whisper's English words** | **6.52 %** | **53 / 98** |
| ... with Whisper on all the audio (simulated) | 6.67 % | 62 / 98 |

**Typical swaps:** "Data Dog" → "DataDog", "worниiнг statemont" → "warning statement", "эксepшеon" → "exception", "Рuби, Пиtн" → "Ruby, Python", "Baunderis" → "Boundaries", "P" → "KPI".

**What it doesn't fix:** some swaps are still wrong where Whisper is wrong too ("Wave to VEG" for wav2vec), and terms GigaAM wrote in plain Cyrillic in a sentence with no Latin letters are not looked at.

**Cost:** on the test set Whisper took 29 s on the NPU and 12.3 s of CPU (its decoding loop and word timing run on the host) for 33 min of audio. Whisper on all the audio would cost 2.2 s of CPU per minute; without word timestamps 1.1 s. The small model is not cheaper: its decoder is heavier than turbo's. In meetings the Whisper pipeline is already loaded to detect the language. `--no-english-terms` turns it off.

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
the voices in the system audio become **Person 1**, **Person 2**, … In an in-person meeting
the microphone is split by voice instead ([In person](#in-person)).

**How it works:**
1. **Who speaks when**, on the NPU, with NVIDIA's **Nemotron 3 Diarization** (since 0.6.0; [why](#choosing-the-diarization-model)):
   - **The model** hears the recording in chunks of 27 s, each with 3 s of what comes next and a cache of the moments that best stand for each voice found so far, so a voice keeps its number through hours of audio. It gives each of up to 8 voices a probability every 10 ms, and a voice talks where that is above 0.5. Overlapping speech gives overlapping turns.
   - **Fingerprints:** NVIDIA **TitaNet-small** fingerprints each voice's longest turns (the middle 4 s of each, up to 60 s), for remembered voices.
   - **One person as two voices:** over a long recording the cache now and then opens a second voice for someone. Voices whose fingerprints are at least 0.75 alike become one: in the test recordings such duplicates were 0.89 and 0.92 alike, and different people at most 0.61.
   - **`--num-speakers N`:** Nemotron finds the number itself. If it finds more, the voice with the least speech joins the one it sounds most like, until N are left.
   - **The way of before 0.6.0** (env `NPU_DIARIZATION=pyannote`, or an install without Nemotron): pyannote **segmentation-3.0** finds up to 3 voices in 10 s windows every 5 s; TitaNet fingerprints each voice's clean speech in each window; the fingerprints are clustered (average linkage, threshold 0.4 on cosine distance, env `NPU_SPEAKER_THRESHOLD`, or exactly `--num-speakers` clusters); and turns are rebuilt pyannote's way (activity added up over the overlapping windows, the most active voices kept per frame, turns under 0.3 s dropped, gaps under 0.5 s closed).
2. **Words, not sentences:** GigaAM's words come with their times from its output frames. Whisper runs with word timestamps (a separate NPU compile of ~1 min, cached; each block takes ~8 % longer). Every word goes to the voice that speaks during it, so a sentence that crosses a change of speaker is split there. A change shorter than 0.6 s inside a sentence is treated as noise.
3. **Echo (meetings):** without headphones the microphone also hears the other side, and Whisper writes their words into your track too, often in the same sentence as your own.
   - First, how loud the system audio comes back through the microphone is measured: the median mic/remote ratio while the other side talks.
   - A word on your track is cut when the microphone is less than 2x louder than that predicted echo at that moment.
   - It is also cut when the other side said the same word at the same moment (within 0.25 s). That is their last word, heard just as you start talking: the volume test misses it, because you are loud there already. Repeating their word yourself comes after it, not at the same moment.
   - With headphones the predicted echo is ~0, so nothing is cut. When you talk over the other side, your voice is far louder than the echo and stays.
4. **Output:** Speaker labels in `.srt` cues (`Speaker 1: …`), `.md` and `.txt` paragraphs (which never mix two speakers), and a `speakers` count in the history line. Meetings get voxtype's Markdown layout: a `### Person 1` heading per change of speaker and a `*[mm:ss]*` mark per paragraph.

### Choosing the diarization model

In 0.6.0 telling the voices apart moved from pyannote segmentation + TitaNet clustering (above, "the way of before 0.6.0") to Nemotron 3 Diarization, NVIDIA's model of September 2026 (100M parameters, [OpenMDW-1.1](https://openmdw.ai/license/1-1/): free for commercial use too). Measured with `tests/speakers_test.py` on real recordings, the NPU for both:
- **Russian:** five podcasts with 2-3 people each (4.1 h; [leshinsky/ru-youtube-diarization](https://huggingface.co/datasets/leshinsky/ru-youtube-diarization), hand-corrected labels; three of them came through YouTube's Opus, like a call).
- **AMI:** the 16 test meetings of the AMI corpus recorded by one microphone in the room, like an in-person meeting (9 h, 3-4 people; forced-alignment labels of [nttcslab-sp/diar-forced-alignment](https://github.com/nttcslab-sp/diar-forced-alignment)).
- **Wrong person:** each labelled stretch of speech is given to a voice the way npu-transcribe gives words to speakers; after the best matching of voices to people, the share of speech under someone else. This is what you see in a transcript.

| | Russian podcasts: before | **0.6.0** | AMI meetings: before | **0.6.0** |
|---|---|---|---|---|
| Speech under the wrong person | 8.1 % | **2.1 %** | 22.8 % | **8.4 %** |
| Diarization error rate (DER) | 20.3 % | 21.3 % | 42.3 % | **11.0 %** |
| ... of which confusion (wrong voice) | 7.8 % | **2.9 %** | 12.2 % | **1.1 %** |
| Right number of people | 3 of 5 | **4 of 5** | 4 of 16 | **15 of 16** |
| Time on the NPU | 132 s | **65 s** | 259 s | **146 s** |

- **The Russian DER** barely moves because its labels count the pauses inside a phrase as speech: Nemotron leaves them out (missed speech 3.0 → 8.2 %), which changes nothing in a transcript, while it gives far less speech to the wrong person.
- **One podcast** (nr3hmL9bcPU) stays at 10 % (27.5 % before, where two of its people became one): its labels put two different people under one name (their phrases fall into two clearly different voices).
- **AMI:** NVIDIA reports 11.1 % DER for this setup; the NPU build scores 11.0 %. The AMI test meetings are not in Nemotron's training data (its train and dev sets are).
- **Someone who barely speaks:** a stranger's phrase put into a Russian podcast got a voice of its own in 5 of 8 placements (every phrase of 5 s or more), and never before 0.6.0, which folds voices with under 15 s of speech into others.
- **Real people in a made-up meeting** (`tests/meeting_test.py`, four people of three Russian podcasts, or four LibriSpeech readers): 100 % of the words under the right label in every case, in both languages, online and in person.

**Other candidates:**
- **pyannote community-1** (segmentation-3.0 + WeSpeaker ResNet34 + VBx clustering): pyannote reports 19.9 % DER on AMI's room microphone, against 22.7 % for its 3.1, so a small step from where BMeet was.
- **DiariZen** (WavLM-based, BUT): among the best in published benchmarks, but its weights are CC-BY-NC: not for work meetings.
- **Streaming Sortformer 4spk v2/v2.1:** Nemotron's predecessor, at most 4 voices, 21.4 % DER on AMI's room microphone by NVIDIA's numbers.
- **pyannote precision-2:** a cloud service.

**Nemotron on the NPU.** `transcribe/prepare-diarization` rebuilds it at install time from its Hugging Face weights, read with numpy (no PyTorch):
- **Fixed shapes:** the NPU needs them, so the encoder always takes 684 frames of 80 ms (the cache 264 + recent frames 40 + the chunk 340 + look-ahead 40), padded and masked when shorter; the positions restart at 0 for each chunk, so the rotary tables are constants. The 128-mel features and the speaker cache between chunks are numpy in npu-transcribe.
- **Accuracy:** in fp16 on the NPU it agrees with transformers' fp32 version on 99.98 % of the 10 ms frames. NVIDIA's own code on the CPU gave 5.5 % wrong person on the Russian podcasts, the NPU build 4.4 % before joining duplicate voices. Over a 99-minute podcast tiny numeric differences change which moments the cache keeps, which once split its host into two voices (5.8-10.6 % on that file depending on the run); joining duplicates (0.75 alike) made it 1.2-1.5 % in both runs.
- **Speed:** about 16 s per hour of audio on the NPU, ~1 s of CPU of it for the features. Compiling it takes ~20 s, once.
- **Limit:** 8 voices per track. More people than that end up under those 8.

**Cleaning up the clustering on real calls** (the way of before 0.6.0). Real calls are harder than clean test voices: background noise, the conferencing app's noise suppression, laughter and codec glitches. On one 49-minute Russian 1:1 call, the clustering made 30 "voices": one real person, plus fragments of 1-114 words. Of those fragments, the ones long enough to compare were 0.14-0.87 similar to that person. So after the clustering:
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

`tests/meeting_test.py` builds the same kind of meeting from real people's speech in Russian (podcast speakers, transcribed by GigaAM) and English (LibriSpeech readers) and checks it. `--espeak` uses espeak-ng voices instead (no network); it runs `--engine whisper`, because GigaAM, trained on real speech, writes almost nothing for espeak's robotic Russian, and it does not check that the people are told apart (Nemotron often hears espeak's voices as one person).

**Remembered voices** (`--meeting … --voices DIR`; BMeet passes it when *remember voices* is on):
- **Fingerprints:** each voice's fingerprint is the duration-weighted mean of the TitaNet embeddings of its longest turns, up to 60 s of speech, at unit length. They go to `DIR/meetings/<transcript>.json` (0600, folders 0700). Naming people in the BMeet app copies theirs to `DIR/voices.json`, at most 10 per person.
- **Recognising, turn by turn:** a turn belongs to a remembered person when its embedding is at least 0.55 similar to their voice and 0.1 more similar than to anyone else remembered.
  - The way of before 0.6.0 also checks a 2 s window every 0.5 s inside turns of 4 s or more, and splits them where the recognised person changes for 1.5 s or more, because two similar voices could share one turn in its segmentation. Nemotron hears those changes itself.
  - A voice whose recognised speech is mostly one person's takes their name. Their turns inside another voice (two people the clustering merged) move to them.
  - Everyone else is numbered as before, *Person 1*, *Person 2*, …, skipping the names.
- **Why 0.55:** fingerprints of the same LibriSpeech reader from different recordings (through Opus) were at least 0.79 similar, and those of different readers at most 0.39. One utterance against a fingerprint scored at least 0.63 for the same reader and at most 0.39 for others.
- **Test:** in `tests/meeting_test.py --remember`, a second meeting with new phrases, where the people speak in another order, came out as Anna, Boris and Viktor, with 100 % of the words under the right name in both languages (real voices, 0.6.0).
- **Language:** a meeting's language is detected on the system audio and used for your microphone too. Detecting it again on a track that may hold little speech of your own went wrong in the tests, and Whisper then translated.

**Robustness on the NPU:**
- **Token cap:** a Whisper block writes at most 400 tokens; fast speech needs ~250 per 30 s. Without the cap, a loop over unclear audio ran until the NPU decoder overflowed ("roi_end <= max_dim"). That block and every later one then failed.
- **Retry and skip:** a failed block is retried once with a fresh pipeline and then skipped. Only 3 failed blocks in a row switch to the CPU: once, one bad block had moved a 10-minute meeting onto the CPU, which took 23 minutes of CPU time.
- **Loops:** a phrase repeated 3 or more times in a row is kept once (a single word: 5 or more times), and so is the same line 3 or more times in a row.

**Speed and CPU:** a 5-minute Russian meeting (real podcast speech in both tracks, echo included) took 10 s.
- **CPU time:** 4 s of user and 4 s of system time in total, mostly loading the models.
- **Why so little:** numpy is kept to one thread. Its BLAS would otherwise start a thread per core for the small matrix products of the features, and they busy-wait: that was 26 s of CPU for the same meeting.
- **Per hour of a Russian meeting:** about 1 minute (54 s, 14 s of CPU, with remembered voices; 0.6.0). GigaAM takes ~27 s for both tracks, and the speakers ~18 s. Before 0.6.0 the speakers took ~1.3 min.
- **Other languages:** Whisper turbo is slower, at ~15x realtime per track.

### Muted in the call

BMeet records the microphone itself, so it hears you while the call app has you muted.
BMeet's Chrome extension (`bmeet/chrome/extension`) tells the pass when that was:
- **In the page** (Meet, Slack; the page's own JavaScript world, before its scripts run), it keeps the `RTCPeerConnection`s the page sets up. While one is connected with audio, it reads `getStats()` twice a second: how much the energy (`totalAudioEnergy`) of each audio `media-source` grew, for those an active `outbound-rtp` sends. Muted, it does not grow, however the app mutes. A disabled track, no track, a silent WebAudio track and a paused sender were each caught in headless Chrome (`bmeet/chrome/tests/test_extension.py`). It is not tied to Meet's or Slack's buttons, which change.
- **Timing:** sound found in a poll began after the poll before, and silence began after the last poll with sound, so each change is stamped with that time. In the tests the stamps were within about 1 s of the real change, mostly within 0.5 s. During a call the state is repeated every 5 s.
- **The service worker** combines the tabs and passes `{t, call, live}` to `bmeet-chrome-host` (native messaging). That writes it to `call-mic.jsonl` in the meeting being recorded (`$XDG_RUNTIME_DIR/bmeet/recording`), and drops it when no meeting records.
- **The pass:** `bmeet-meeting-audio` writes each part's start time, which places the log in the recording. Where a call was on and sent no sound, the microphone track is silenced before transcribing. A log line counts for at most 15 s; 0.5 s before each stretch with sound is kept; stretches under 1 s are ignored.
- **Safety:** a log that never once saw sound sent leaves the microphone alone. That covers a call whose microphone the extension cannot see, and the echo filter still measures the echo on the original track. In-person meetings ignore the log.
- **Test:** `tests/meeting_test.py --muted` says one sentence while muted, after the pause. It was left out in both languages; everything else stayed.

### In person

An in-person meeting (`meeting.json`: `"in_person": true`) has only the microphone, so the
microphone is diarized like the system audio, and then:
- **You:** with `--voices`, your own voice (`DIR/you.json`) is one of the remembered voices, under the name *You*. A single voice found is *You* as well.
- **Learning your voice:** in online meetings, what is left of your microphone after the echo and the muted stretches is you. Its fingerprint is the duration-weighted mean of 4 s pieces from the middle of your longest lines (a long line gives several), up to 60 s. It needs at least 15 s, and the last 10 meetings are kept. A meeting whose fingerprint is under 0.5 similar to the ones kept (someone else at your microphone) is skipped once there are two to compare with. In the app you can also name yourself (*You*) in an in-person meeting.
- **Nobody through the speakers:** an online meeting in which the system audio held no speech is split the same way. That covers starting a normal meeting in the room by habit, and a call whose system audio was not captured, where the others leak into your microphone.
- **Test:** `tests/meeting_test.py --in-person`. You are learned in an online meeting, the people are named, and a second meeting in the room comes out as You, Anna, Boris and Viktor: 100 % of the words under the right label in both languages, with real voices (0.6.0). With espeak's voices (`--espeak`; four voices at most 0.41 alike to TitaNet) it was 96 % in English and 87 % in Russian before 0.6.0; Nemotron, trained on real people, often hears espeak's voices as one.

## Live transcript

`--live DIR` writes the transcript of a meeting while BMeet records it, to `DIR/live.md`.
The BMeet app shows it under the running meeting and keeps scrolling with it (unless you
scrolled up to read). When the meeting stops, it becomes the meeting's Markdown copy until
the speaker pass replaces it, instead of voxtype's own transcript (whisper-small in 30 s
chunks, which voxtype keeps to itself until the meeting ends). `voxtype-meeting-toggle`
starts it as the unit `bmeet-live` next to the recorder. It is off when
`~/.config/bmeet/live-transcript` says `off`, and without the recording (`--no-speakers`).

**How it works:**
- **Following the recording:** twice a second it reads what `pw-record` added to the part's WAV files, from behind their header (whose sizes are only written at the end). A new `part-N.start` means the part before it is complete.
- **Phrases:** each track's loudness per 100 ms, against its own background noise (the quietest tenth of its last 30 s, +12 dB), shows where someone talks. A phrase ends after 0.8 s of quiet, or is cut at its quietest moment after 20 s. Under 0.3 s of sound is a click, not a phrase.
- **Text:** a phrase is transcribed once, when it has ended, with 0.3 s of audio around it. With GigaAM (Russian) the phrase still being said is also transcribed after every 1.5 s more of it and shown in italics: ~85 ms on the NPU and ~5 ms of CPU each time. (GigaAM's input is fixed at 30 s on the NPU, so a short phrase costs as much NPU time as a long one.) Other languages wait for the end of the phrase, as Whisper costs more.
- **Language:** `--language`, or detected by Whisper on the first 8 s said on the other side (in person: on the microphone), as the speaker pass does. For Russian, Whisper is let go of then.
- **Your microphone:** what you said while the call could not hear you is left out ([Muted in the call](#muted-in-the-call)), and the other side's echo is cut as in the speaker pass ([Speakers](#speakers), step 3). Your phrases are only what is clearly louder (2x) than the echo the system audio would cause at that moment, so their reply, heard by your microphone, doesn't keep your phrase open. A phrase of yours becomes final once the other side's text for the same moment is there.
- **Labels:** You and Remote; in person, Room. Telling the other voices apart (Person 1, …) stays the speaker pass's work after the meeting.
- **Memory:** only the audio a phrase still needs is kept (in RAM, at most a minute or so), plus the loudness per 100 ms. The process peaks at ~1.6 GB while Whisper is loaded (for the language, or for good with other languages than Russian).
- **The end:** when the recorder no longer names the folder in `$XDG_RUNTIME_DIR/bmeet/recording` (or at SIGTERM, or when the speaker pass begins), it transcribes the last phrases, writes `live.md` once more without italics, and marks it complete with `live.done`. `voxtype-meeting-toggle` waits for that (at most 60 s) before it starts the speaker pass; a live transcript that did not get to the end leaves voxtype's in place.

**Measured** with `tests/meeting_test.py --live`: the made-up meetings of real voices are recorded again in real time while `--live` follows them.

| meeting (~1 min, 10 lines; real voices) | lines shown live | words first seen | line final | wrong side |
|---|---|---|---|---|
| Russian, laptop speakers (echo) | 10/10 | 1.8 s before the line ends | 2.3 s after (max 13.9 s) | none of theirs under You |
| Russian, headphones | 10/10 | 1.8 s before | 2.0 s after (max 13.8 s) | none |
| Russian, one sentence said while muted | 10/10, the muted one not | 1.8 s before | 4.1 s after (max 13.9 s) | none |
| Russian, in person (13 lines) | 13/13, all under Room | 1.9 s before | 4.6 s after (max 13.3 s) | – |
| English | 10/10 | 1.9 s after | 1.9 s after (max 8.7 s) | none |
| English, one sentence said while muted | 10/10, the muted one not | 1.7 s after | 1.7 s after (max 8.1 s) | none |

Medians; the longest waits are the first lines, said before the language is known (8 s of
speech). Lines of different people with under 0.8 s between them become one phrase, so the
earlier one is final only when the later one ends (the words show in italics meanwhile).
**CPU:** ~2-2.7 s to start (Whisper and GigaAM loaded, the language detected), then 0.2-0.3 s
per minute of meeting in Russian (~15 s an hour) and ~1.3 s per minute with Whisper.

## Tidying the text

The outputs (meetings, `.md`, `.txt`, `.srt`) leave out what the speech models write for hesitations and slips, by fixed rules (`tidy()`; `--verbatim` keeps it all):
- **Fillers:** э-э, а-а, м-м, эм, хмм, um, uh, with the commas around them. A lone "м" or "а" between commas counts as well; the conjunction "а" elsewhere is kept.
- **Stutters:** a word said twice in a row ("мы мы"), a cut-off start of the next word ("функцио функционал"), and stretched sounds ("и-и-и"). Words said several times on purpose stay: да, нет, так, ну, вот, очень and a few more, and anything with punctuation between ("нет, нет, нет").
- **Mixed scripts:** a word that mixes Latin and Cyrillic letters is written in the script most of it is in. GigaAM switches mid-word on English terms ("oкей" → "окей", "Viкl" → "Vikl", "реetention" → "reetention"). This makes them readable and searchable, though not the right word.
- **Spacing:** "Подожди,подожди" → "Подожди, подожди", ".." → ".", "....." → "...". A comma inside a number ("5,3") stays.

On a real 65-minute Russian meeting this changed 134 of 195 paragraphs and removed 1.1 % of the words.

**A language model for more was tried and not shipped.** The idea was to fix punctuation and misheard words with an instruction model on the NPU: OpenVINO GenAI `LLMPipeline`, thinking off, greedy decoding. The test was a 5-minute stretch of a real Russian analytics meeting (16 paragraphs, ~900 words):

| model (NPU build) | rewriting paragraphs | listing misheard terms only |
|---|---|---|
| Qwen3-8B, int4 channel-wise (`OpenVINO/Qwen3-8B-int4-cw-ov`) | Left most fillers. Inserted Chinese ("Переходим на рет和其他") and cut words ("визуализацию" → "виз"). 3.5 min for the stretch, compile included. | Echoed the transcript, looped on one pair 30 times, broke endings ("дашборде" → "дашборд"). |
| Qwen3-4B-Instruct-2507, int4 channel-wise with AWQ (third-party build) | – | One right fix ("афелит" → "affiliate"). Also translated Russian ("глобально" → "globally"), dropped words, invented terms ("дажет qвиsiton" → "previous quarter"), and repeated the prompt's examples. |

The NPU runs these models only with int4 channel-wise weights, and their Russian suffers from that. A better candidate is a Russian fine-tune (T-Tech T-lite-it-2.1, Apache-2.0) converted with AWQ and scale estimation. That needs a 16 GB download and a heavy conversion, and it wasn't tried.

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
