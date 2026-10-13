#!/usr/bin/env bash
# Regression + benchmark run for npu-transcribe (results: docs/npu-transcribe.md).
#
# Usage: tests/run-tests.sh WORKDIR [DEVICES]     e.g. run-tests.sh /tmp/npt "NPU CPU"
# Env:   NPU_PYTHON  Python with openvino-genai (default: the installer's venv)
#
# Generates synthetic speech clips (tests/make_speech.py) if WORKDIR/clips is
# missing, transcribes them with every device x model combination (one process
# per combination, so the multi-file / language re-detection path is exercised),
# validates each .srt and prints the word error rate against the reference text.
# Writes only below WORKDIR (plus the OpenVINO compile cache).
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
python=${NPU_PYTHON:-$HOME/.local/share/bmeet/venv/bin/python}
tool() { "$python" -I "$here/../npu-transcribe" "$@"; }
work=${1:?usage: run-tests.sh WORKDIR [DEVICES]}
devices=${2:-NPU CPU}
clips="$work/clips"
# Keep test runs out of the user's transcript history (BMeet "Transcribed files").
export NPU_TRANSCRIBE_HISTORY="$work/history.jsonl"

[ -f "$clips/en_long.mp4" ] || "$here/make_speech.py" "$clips"
inputs=("$clips"/en_short.ogg "$clips"/ru_short.ogg "$clips"/de_short.ogg \
        "$clips"/fr_short.ogg "$clips"/en_long.mp4)

for dev in $devices; do
    for model in small turbo; do
        out="$work/$dev-$model"
        mkdir -p "$out"
        echo "=== $dev / $model"
        start=$(date +%s.%N)
        rc=0
        tool --device "$dev" --model "$model" --format srt,md,txt --overwrite --out "$out" \
            "${inputs[@]}" >/dev/null 2>"$out/run.log" || rc=$?
        printf '  exit %s, total wall %.1f s (incl. model load)\n' "$rc" \
            "$(awk -v s="$start" -v e="$(date +%s.%N)" 'BEGIN { print e - s }')"
        grep -E 'model ready|done:|WARNING|ERROR' "$out/run.log" | sed 's/^npu-transcribe: /  /'
        for srt in "$out"/*.srt; do
            "$here/check_output.py" "$srt" "$clips/$(basename "${srt%.srt}").ref.txt" \
                | sed "s|^$out/|  |"
        done
    done
done

# Translation: Russian/German speech -> English text, both models, first device.
dev=${devices%% *}
for model in small turbo; do
    out="$work/$dev-$model-translate"
    mkdir -p "$out"
    echo "=== $dev / $model / --translate"
    tool --device "$dev" --model "$model" --translate --format txt --overwrite --out "$out" \
        "$clips"/ru_short.ogg "$clips"/de_short.ogg >/dev/null 2>"$out/run.log" || echo "  exit $?"
    grep -E 'done:|WARNING|ERROR' "$out/run.log" | sed 's/^npu-transcribe: /  /'
    for txt in "$out"/*.txt; do
        echo "  $(basename "$txt"): $(tr '\n' ' ' < "$txt")"
    done
done

# Meeting speaker pass (BMeet): synthetic call in both main languages, with and without echo,
# muted in the call for a sentence, and in person (one microphone).
echo "=== meeting speakers (--meeting)"
for args in "--language ru --remember" "--language en --remember" "--language en --headphones" \
            "--language ru --muted" "--language en --muted --headphones" \
            "--language ru --in-person" "--language en --in-person"; do
    echo "  $args"
    NPU_PYTHON="$python" "$here/meeting_test.py" "$work" $args | sed 's/^/  /'
done
