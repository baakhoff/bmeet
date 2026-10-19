#!/usr/bin/env bash
# BMeet uninstaller for macOS (the top-level uninstall.sh hands over to it on a Mac).
#
#   bash ~/.local/share/bmeet/src/uninstall.sh [--purge]
#
#   --purge   also delete the models (~/.local/share/npu-models), the download and build
#             cache, and your remembered voices
#
# Never deleted: meeting transcripts (~/Documents/Meetings, the meeting index in
# ~/.local/share/bmeet/meetings), transcripts next to your media files, the transcription
# history and your settings (~/.config/bmeet). macOS's permissions for BMeet (Microphone,
# System Audio Recording, Accessibility) stay listed in System Settings until you remove them.
set -euo pipefail

src="$(cd "$(dirname "$0")/.." && pwd)"
data="${XDG_DATA_HOME:-$HOME/.local/share}"
root="$data/bmeet"
cache="${XDG_CACHE_HOME:-$HOME/.cache}/bmeet"
models="${NPU_MODELS_DIR:-$data/npu-models}"
app="$HOME/Applications/BMeet.app"
purge=
for a in "$@"; do
    case "$a" in
        --purge) purge=1 ;;
        --keep-system) ;;
        -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
        *) echo "unknown option: $a (see --help)" >&2; exit 2 ;;
    esac
done
say() { printf '\n==> %s\n' "$*"; }
run_dir="$(getconf DARWIN_USER_TEMP_DIR)bmeet"
case "$(head -n1 "$run_dir/meeting_state" 2>/dev/null || echo idle)" in
    recording|paused) echo "A meeting is being recorded - stop it first (⌃⌥M)." >&2; exit 1 ;;
esac

say "BMeet.app"
pkill -TERM -f "$app/Contents/MacOS/BMeet" 2>/dev/null && sleep 1   # (it quits cleanly on SIGTERM)
pkill -KILL -f "$app/Contents/MacOS/BMeet" 2>/dev/null || true
pkill -f "$root/bin/bmeet-whisper" 2>/dev/null || true
if [ -d "$app" ]; then
    /System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister \
        -u "$app" >/dev/null 2>&1 || true
    rm -rf "$app"
    echo "  removed $app (and its login item)"
fi
/System/Library/CoreServices/pbs -update >/dev/null 2>&1 || true

say "Chrome extension's helper"
[ -f "$src/bmeet/chrome/install-user.sh" ] && { bash "$src/bmeet/chrome/install-user.sh" --uninstall || true; }

say "Commands, program files, Python environment"
for f in npu-transcribe bmeet-ctl bmeet bmeet-chrome-host; do
    if [ -f "$HOME/.local/bin/$f" ] && grep -q "BMeet" "$HOME/.local/bin/$f" 2>/dev/null; then
        rm -f "$HOME/.local/bin/$f"
    fi
done
# (a RAM disk left by a crash: nothing on it is kept)
if mount | grep -qF "$run_dir/meeting-audio"; then
    diskutil unmount force "$run_dir/meeting-audio" >/dev/null 2>&1 || true
fi
rm -rf "$run_dir"
rm -rf "$HOME/Library/Caches/bmeet-whisper"   # (what macOS compiled for the Neural Engine: ~1 GB)
if [ -n "$purge" ]; then
    rm -rf "$models" "$cache" "$data/bmeet/voices"
    echo "  removed the models, the caches and the remembered voices"
else
    rm -rf "$cache/swift-build"
    echo "  kept the models in $models (--purge deletes them)"
fi
# last: this script lives in $root/src
cd "$HOME"
rm -rf "$root/venv" "$root/bin" "$root/src"
rmdir "$root" 2>/dev/null || true
say "BMeet is uninstalled. Your transcripts and settings are still there."
