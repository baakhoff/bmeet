#!/usr/bin/env bash
# BMeet uninstaller. Removes the per-user parts, then (with sudo) the system part.
#
#   bash ~/.local/share/bmeet/src/uninstall.sh [--keep-system] [--purge]
#
#   --keep-system   leave voxtype, the OpenVINO GenAI SDK etc. installed (no sudo)
#   --purge         also delete the Whisper and speaker models (~/.local/share/npu-models),
#                   the NPU compile cache and the download cache
#
# Never deleted: meeting transcripts (~/Documents/Meetings, voxtype's meetings database),
# transcripts written next to your media files, and the transcription history.
set -euo pipefail

src="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
data="${XDG_DATA_HOME:-$HOME/.local/share}"
root="$data/bmeet"
cache="${XDG_CACHE_HOME:-$HOME/.cache}"
models="${NPU_MODELS_DIR:-$data/npu-models}"
system=1 purge=
for a in "$@"; do
    case "$a" in
        --keep-system) system= ;;
        --purge) purge=1 ;;
        -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
        *) echo "unknown option: $a (see --help)" >&2; exit 2 ;;
    esac
done
[ "$(id -u)" -ne 0 ] || { echo "Run this as your normal user (it asks for sudo itself)." >&2; exit 1; }

say() { printf '\n==> %s\n' "$*"; }

say "BMeet panel widget"
bash "$src/bmeet/plasmoid/install-user.sh" --uninstall || true
say "BMeet app"
bash "$src/bmeet/app/install-user.sh" --uninstall || true
say "npu-transcribe"
bash "$src/transcribe/install-user.sh" --uninstall || true
say "voxtype (per-user)"
bash "$src/voxtype/user-setup.sh" --uninstall || true

if [ -n "$purge" ]; then
    say "Models and caches"
    rm -rf "$models/whisper-small-int8-ov" "$models/whisper-large-v3-turbo-int8-ov" \
           "$models/speaker-diarization" \
           "$cache/npu-ov-cache" "$cache/bmeet"
    rmdir "$models" 2>/dev/null || true
fi

if [ -n "$system" ]; then
    say "System part (sudo)"
    # Copy the script out of the tree that is deleted below before running it.
    tmp="$(mktemp)"
    cp "$src/system/uninstall.sh" "$tmp"
    if ! sudo bash "$tmp"; then
        rm -f "$tmp"
        echo "  The system part failed or was cancelled. Program files are kept so you can retry:"
        echo "    bash $src/uninstall.sh   (or: sudo bash $src/system/uninstall.sh)"
        exit 1
    fi
    rm -f "$tmp"
fi

say "Program files"
if [ "$src" = "$root/src" ]; then
    rm -rf "$root"
    echo "  removed $root"
else
    rm -rf "$root/venv" "$root/src" "$root/voices"   # (voices: remembered voice fingerprints)
    rmdir "$root" 2>/dev/null || true
    echo "  removed $root/{src,venv} (this checkout in $src stays)"
fi

echo
echo "Uninstalled. Kept: your meeting transcripts and transcription history"
[ -n "$purge" ] || echo "and the Whisper models in $models (--purge removes them)."
