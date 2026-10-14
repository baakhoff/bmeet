#!/usr/bin/env bash
# Install the BMeet app for the current user only (no sudo, nothing system-wide):
#   ~/.local/share/applications/bmeet.desktop   launcher (Kickoff/KRunner) and
#                                                    "Open with" entry for audio/video files
#   ~/.local/bin/bmeet, ~/.local/bin/bmeet-ctl   symlinks into this tree
#                                                    (the panel widget calls bmeet-ctl)
# The app itself stays in this directory (bmeet-ctl's `open` starts it from here).
#
# Usage: ./install-user.sh              install / update (idempotent)
#        ./install-user.sh --uninstall
set -euo pipefail

here=$(cd "$(dirname "$(readlink -f "$0")")" && pwd)
app="$here/bmeet"
ctl="$(dirname "$here")/bmeet-ctl"
bin_dir="$HOME/.local/bin"
apps_dir="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
desktop="$apps_dir/bmeet.desktop"

refresh() {  # MIME cache for "Open with" + KDE's service cache (best effort)
    update-desktop-database "$apps_dir" 2>/dev/null || true
    kbuildsycoca6 >/dev/null 2>&1 || true
}

unlink_ours() {  # remove a symlink only if it points at a bmeet file
    if [[ -L $1 ]] && [[ $(readlink "$1") == */bmeet* ]]; then rm -v "$1"; fi
}

case ${1:-} in
    --uninstall)
        # BMeet's own dictation: stopping it gives KWin's input method back if it holds it
        systemctl --user stop bmeet-dictate.service 2>/dev/null || true
        /usr/bin/python3 -I "$(dirname "$here")/dictate/bmeet-dictate" give-back 2>/dev/null || true
        rm -fv "$desktop"
        unlink_ours "$bin_dir/bmeet"
        unlink_ours "$bin_dir/bmeet-ctl"
        refresh
        exit 0 ;;
    "") ;;
    *) echo "usage: $0 [--uninstall]" >&2; exit 2 ;;
esac

# --- sanity checks -----------------------------------------------------------
/usr/bin/python3 -I -c 'import PyQt6.QtWidgets, PyQt6.QtNetwork' \
    || { echo "error: the system PyQt6 (python3-pyqt6) is missing" >&2; exit 1; }
case $app in *[[:space:]]*) echo "error: paths with spaces are not supported in Exec=" >&2; exit 1;; esac
dictate="$(dirname "$here")/dictate"
chmod 755 "$app" "$ctl" "$dictate/bmeet-dictate" "$dictate/bmeet-im"
"$app" --help >/dev/null   # parses and imports PyQt6: fails early if something is broken
"$ctl" status >/dev/null
"$dictate/bmeet-dictate" --version >/dev/null
# A running bmeet-dictate (BMeet's own dictation, when ~/.config/bmeet/live-dictation is on) still
# has the old files: stop it, the next Meta+H starts the new one.
systemctl --user stop bmeet-dictate.service 2>/dev/null || true

# --- the same app before 0.2.0 (BMeet was npu-voicenotes, the app "Voice Notes") -----
for name in voicenotes voicenotes-ctl; do
    if [[ -L $bin_dir/$name ]] && [[ $(readlink "$bin_dir/$name") == */npu-voicenotes/* ]]; then
        rm -v "$bin_dir/$name"
    fi
done
if [[ -f $apps_dir/voicenotes.desktop ]] \
   && grep -q '^Exec=.*/npu-voicenotes/src/voicenotes/app/voicenotes' "$apps_dir/voicenotes.desktop"; then
    rm -v "$apps_dir/voicenotes.desktop"
fi
config="${XDG_CONFIG_HOME:-$HOME/.config}"
if [[ -f $config/voicenotes/voicenotes.conf && ! -e $config/bmeet/bmeet.conf ]]; then
    mkdir -p "$config/bmeet"
    mv -v "$config/voicenotes/voicenotes.conf" "$config/bmeet/bmeet.conf"   # window layout etc.
    rmdir "$config/voicenotes" 2>/dev/null || true
fi

# --- commands --------------------------------------------------------------------
mkdir -p "$bin_dir"
for pair in "bmeet:$app" "bmeet-ctl:$ctl"; do
    name=${pair%%:*} target=${pair#*:}
    if [[ -e $bin_dir/$name && ! -L $bin_dir/$name ]]; then
        echo "error: $bin_dir/$name exists and is not a symlink; not touching it" >&2
        exit 1
    fi
    ln -sfn "$target" "$bin_dir/$name"
    echo "linked   $bin_dir/$name -> $target"
done

# --- desktop entry -------------------------------------------------------------
# StartupWMClass/app_id: the window sets QGuiApplication.desktopFileName("bmeet"),
# so Plasma's task manager matches it with this file (icon, name, pinning).
mkdir -p "$apps_dir"
tmpdir=$(mktemp -d)
trap 'rm -rf "$tmpdir"' EXIT
tmp="$tmpdir/bmeet.desktop"   # desktop-file-validate wants the .desktop name
cat > "$tmp" <<EOF
[Desktop Entry]
Type=Application
Name=BMeet
GenericName=Dictation and Transcripts
Comment=Voice typing, meeting recordings and audio/video transcripts (Whisper on the Intel NPU)
Exec=$app %F
TryExec=$app
Icon=audio-input-microphone
Terminal=false
StartupNotify=true
StartupWMClass=bmeet
Categories=AudioVideo;Utility;
MimeType=audio/*;video/*;
Keywords=dictation;voice;speech;transcribe;transcript;meeting;subtitles;whisper;voxtype;
Actions=meetings;files;transcribe;

[Desktop Action meetings]
Name=Meetings
Exec=$app meetings

[Desktop Action files]
Name=Transcribed files
Exec=$app files

[Desktop Action transcribe]
Name=Transcribe a file
Exec=$app transcribe
EOF
if ! out=$(desktop-file-validate "$tmp" 2>&1); then
    printf '%s\n' "$out" >&2
    echo "error: the generated desktop entry is invalid" >&2
    exit 1
fi
# (its only hint, "more than one main category", is intended: AudioVideo + Utility)
printf '%s\n' "$out" | grep -v -e 'more than one main category' -e '^$' || true
chmod 644 "$tmp"
if [[ -f $desktop ]] && cmp -s "$tmp" "$desktop"; then
    echo "unchanged $desktop"
else
    cp -p "$tmp" "$apps_dir/.bmeet.desktop.new"   # same filesystem: atomic rename
    mv -f "$apps_dir/.bmeet.desktop.new" "$desktop"
    echo "installed $desktop"
fi
refresh
echo "done. Start it from the application menu (BMeet), with bmeet-ctl open,"
echo "     or right-click an audio/video file in Dolphin -> Open With -> BMeet."
