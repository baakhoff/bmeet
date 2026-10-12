#!/usr/bin/env bash
# Install npu-transcribe for the current user only (no sudo, nothing system-wide):
#   ~/.local/bin/npu-transcribe                             wrapper: runs the script in this
#                                                           directory with the venv's Python
#   ~/.local/share/kio/servicemenus/npu-transcribe.desktop  Dolphin right-click submenu
#
# Usage: ./install-user.sh [--venv DIR]     install / update (idempotent)
#        ./install-user.sh --uninstall
# The venv (openvino-genai, numpy) is made by the top-level install.sh; default
# ~/.local/share/bmeet/venv.
set -euo pipefail

here=$(cd "$(dirname "$(readlink -f "$0")")" && pwd)
script="$here/npu-transcribe"
venv="${BMEET_VENV:-${XDG_DATA_HOME:-$HOME/.local/share}/bmeet/venv}"
models="${NPU_MODELS_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/npu-models}"
link="$HOME/.local/bin/npu-transcribe"
menu_dir="${XDG_DATA_HOME:-$HOME/.local/share}/kio/servicemenus"
menu="$menu_dir/npu-transcribe.desktop"
marker="# BMeet wrapper"
old_marker="# npu-voicenotes wrapper"   # before 0.2.0

case ${1:-} in
    --uninstall)
        # Only remove what this script wrote (a wrapper with our marker, or an old symlink).
        if [[ -L $link ]] || { [[ -f $link ]] && grep -qF -e "$marker" -e "$old_marker" "$link"; }; then
            rm -v "$link"
        fi
        rm -fv "$menu"
        exit 0 ;;
    --venv) venv=${2:?--venv needs a directory} ;;
    "") ;;
    *) echo "usage: $0 [--venv DIR] | --uninstall" >&2; exit 2 ;;
esac

# --- sanity checks -----------------------------------------------------------
python="$venv/bin/python"
[[ -x $python ]] || { echo "error: no Python venv at $venv (run the top-level install.sh)" >&2; exit 1; }
for tool in ffmpeg ffprobe; do
    command -v "$tool" >/dev/null || { echo "error: $tool not found (sudo apt install ffmpeg)" >&2; exit 1; }
done
if [[ -e $link && ! -L $link ]] && ! grep -qF -e "$marker" -e "$old_marker" "$link"; then
    echo "error: $link exists and was not written by this installer; not touching it" >&2
    exit 1
fi
case "$link$script$python$models" in *[\"\\\$\`]*)
    echo "error: paths containing \" \\ \$ or \` are not supported" >&2; exit 1;; esac

# --- CLI wrapper ---------------------------------------------------------------
# A wrapper rather than a symlink: the script's own shebang is the system python3,
# which has no OpenVINO. -I keeps the user's PYTHONPATH and cwd out of it. The models
# directory is fixed here too, so the Dolphin menu and the app find the same models.
mkdir -p "$(dirname "$link")"
tmp=$(mktemp "$(dirname "$link")/.npu-transcribe.XXXXXX")
trap 'rm -f "$tmp"' EXIT
cat > "$tmp" <<EOF
#!/bin/sh
$marker (rewritten by install.sh, removed by uninstall.sh)
NPU_MODELS_DIR="\${NPU_MODELS_DIR:-$models}" exec "$python" -I "$script" "\$@"
EOF
chmod 755 "$tmp"
mv -f "$tmp" "$link"
trap - EXIT
echo "installed $link -> $script"
"$link" --version   # imports numpy: fails early if the venv is broken

# --- Dolphin service menu ----------------------------------------------------
# Same keys as the system's /usr/share/kio/servicemenus/konsolerun.desktop
# (KF6 / Plasma 6.6). KF6 ignores service menus in user directories unless the
# file is executable. Runs in a Konsole window that stays open (--hold) so the
# progress and any errors stay visible; --notify adds a desktop notification.
# ExcludeServiceTypes (a KFileItemActions key) hides the entry for playlists and
# MIDI, which match audio/* but hold no audio ffmpeg could transcribe.
case $link in *[[:space:]]*)
    echo "note: $link contains a space; the Dolphin menu is skipped (KDE service menus cannot quote it)"
    exit 0 ;;
esac
mkdir -p "$menu_dir"
tmp=$(mktemp "$menu_dir/.npu-transcribe.XXXXXX")
trap 'rm -f "$tmp"' EXIT
cat > "$tmp" <<EOF
[Desktop Entry]
Type=Service
Name=Transcribe (NPU)
X-KDE-ServiceTypes=KonqPopupMenu/Plugin
MimeType=audio/*;video/*;application/ogg;
ExcludeServiceTypes=audio/x-mpegurl;audio/x-scpls;audio/x-ms-asx;audio/midi;
Actions=transcribe;transcribeSpeakers;transcribeSmall;translate;
X-KDE-Submenu=Transcribe (NPU)
X-KDE-Priority=TopLevel
Icon=view-media-lyrics

[Desktop Action transcribe]
Name=Transcribe (best installed model)
Icon=view-media-lyrics
TryExec=konsole
Exec=konsole --hold -e $link --notify %F

[Desktop Action transcribeSpeakers]
Name=Transcribe with speakers (Speaker 1, Speaker 2, ...)
Icon=system-users
TryExec=konsole
Exec=konsole --hold -e $link --speakers --notify %F

[Desktop Action transcribeSmall]
Name=Transcribe with Whisper small (lighter, faster on CPU)
Icon=view-media-lyrics
TryExec=konsole
Exec=konsole --hold -e $link --model small --notify %F

[Desktop Action translate]
Name=Translate speech to English (Whisper small)
Icon=translate
TryExec=konsole
Exec=konsole --hold -e $link --translate --notify %F
EOF
chmod 755 "$tmp"
mv -f "$tmp" "$menu"
trap - EXIT
echo "installed $menu"
# (desktop-file-validate does not know KDE service menus: it flags TryExec in
# actions and MimeType/Actions on Type=Service exactly as for the system's own
# konsolerun.desktop, so it is not run here.)

case ":$PATH:" in
    *":$HOME/.local/bin:"*) ;;
    *) echo "note: ~/.local/bin is not in PATH; log out and in, or use $link" ;;
esac
