#!/bin/bash
# voxtype: per-user part of the setup (NO sudo). The top-level install.sh runs it after
# system/install.sh; it is idempotent and can be re-run on its own:
#     bash voxtype/user-setup.sh [--reset-config]
#     bash voxtype/user-setup.sh --uninstall      (meeting transcripts are kept)
# What it touches (all under your home, all undone by --uninstall):
#   ~/.config/voxtype/config.toml            written if missing; a config not written by
#                                            BMeet is kept as config.toml.bak-<time>
#   ~/.config/bmeet/languages       dictation languages for Meta+Shift+H
#   ~/.local/bin/voxtype-meeting-toggle, voxtype-lang, bmeet-meeting-audio
#   ~/.local/share/applications/voxtype-*.desktop  (+ links in ~/.local/share/kglobalaccel/)
#   ~/.local/share/voxtype/models/gtcrn_simple.onnx (meeting echo cancel, sha256-pinned)
#   ~/.cache/voxtype/openvino/                (NPU compile cache, ~1.1 GB for whisper-small)
#   ~/Documents/Meetings/                     (created, never deleted)
#   ~/.config/systemd/user/voxtype.service.d/10-npu-setup.conf  (log level, software OSD)
#   systemd user unit voxtype.service enabled + started
# Env: NPU_MODELS_DIR (default ~/.local/share/npu-models),
#      BMEET_LANGUAGES (e.g. "en de"; written to the languages file).
set -eu

[ "$(id -u)" -ne 0 ] || { echo "Run this as your normal user, not as root."; exit 1; }

HERE="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
CONF_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/voxtype"
LANG_FILE="${XDG_CONFIG_HOME:-$HOME/.config}/bmeet/languages"
DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}"
CACHE="${XDG_CACHE_HOME:-$HOME/.cache}/bmeet/downloads"
BIN_DIR="$HOME/.local/bin"
APP_DIR="$DATA_DIR/applications"
KGA_DIR="$DATA_DIR/kglobalaccel"
VT_MODELS="$DATA_DIR/voxtype/models"
MEETINGS="$HOME/Documents/Meetings"
MODELS_DIR="${NPU_MODELS_DIR:-$DATA_DIR/npu-models}"
MODEL="$MODELS_DIR/whisper-small-int8-ov"
SDK_LIB=/opt/intel/openvino_genai/runtime/lib/intel64/libopenvino_genai_c.so
GTCRN_URL=https://github.com/k2-fsa/sherpa-onnx/releases/download/speech-enhancement-models/gtcrn_simple.onnx
GTCRN_SHA256=e77603ac0c23dac3227dd2d7135b3a585cbee2679048aecfa886657d3ae1b534
NPU_BUSY=/sys/class/accel/accel0/device/npu_busy_time_us
CONFIG_MARKER="# BMeet:"
OLD_CONFIG_MARKER="# npu-voicenotes:"   # before 0.2.0 (BMeet was called npu-voicenotes)
OLD_LANG_FILE="${XDG_CONFIG_HOME:-$HOME/.config}/npu-voicenotes/languages"
DROPIN_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/voxtype.service.d"
DROPIN="$DROPIN_DIR/10-npu-setup.conf"
APPS="voxtype-dictate voxtype-meeting voxtype-meeting-in-person voxtype-lang"
# Whisper's language codes (openai/whisper tokenizer.py LANGUAGES)
WHISPER_LANGS="en zh de es ru ko fr ja pt tr pl ca nl ar sv it id hi fi vi he uk el ms cs ro
da hu ta no th ur hr bg lt la mi ml cy sk te fa lv bn sr az sl kn et mk br eu is hy ne mn bs
kk sq sw gl mr pa si km sn yo so af oc ka be tg sd gu am yi lo uz fo ht ps tk nn mt sa lb my
bo tl mg as tt haw ln ha ba jw su yue"
WHISPER_LANGS="$(echo $WHISPER_LANGS)"   # one line
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
# Started from a script that has no desktop session environment (sudo, ssh), kbuildsycoca6
# would refresh a different cache than the one Plasma (and kglobalaccel) watches: it names
# the cache after XDG_DATA_DIRS and the locale. Plasma exports both to systemd --user.
for var in XDG_DATA_DIRS LANG; do
    if [ -z "${!var:-}" ]; then
        val="$(systemctl --user show-environment 2>/dev/null | sed -n "s/^$var=//p")"
        [ -n "$val" ] && export "$var=$val"
    fi
done

step() { echo; echo "=== $* ==="; }
fail() { echo "ERROR: $*" >&2; exit 1; }
refresh_kde() { command -v kbuildsycoca6 >/dev/null && kbuildsycoca6 >/dev/null 2>&1 || true; }

uninstall() {
    if [ -x "$BIN_DIR/voxtype-meeting-toggle" ] \
       && [ "$(head -n1 "$XDG_RUNTIME_DIR/voxtype/meeting_state" 2>/dev/null || echo idle)" != idle ]; then
        echo "  a meeting is running - stopping it so its transcript is saved"
        "$BIN_DIR/voxtype-meeting-toggle" stop || true
    fi
    systemctl --user disable --now voxtype.service 2>/dev/null || true
    systemctl --user stop ydotool.service 2>/dev/null || true
    # speaker passes still running: stop them (their ExecStopPost deletes the audio)
    systemctl --user stop bmeet-meeting-audio.service 'bmeet-speakers-*.service' 2>/dev/null || true
    rm -rf "$XDG_RUNTIME_DIR/bmeet/meeting-audio" "$XDG_RUNTIME_DIR/bmeet/recording"
    rmdir "$XDG_RUNTIME_DIR/bmeet" 2>/dev/null || true
    rm -f "$BIN_DIR/voxtype-meeting-toggle" "$BIN_DIR/voxtype-lang" "$BIN_DIR/bmeet-meeting-audio"
    for f in $APPS; do
        rm -f "$APP_DIR/$f.desktop" "$KGA_DIR/$f.desktop"
    done
    rmdir "$KGA_DIR" 2>/dev/null || true
    refresh_kde
    # Our config: kept if you changed it (renamed, so voxtype starts clean next time).
    if [ -f "$CONF_DIR/config.toml" ] \
       && head -n1 "$CONF_DIR/config.toml" | grep -qF -e "$CONFIG_MARKER" -e "$OLD_CONFIG_MARKER"; then
        mv "$CONF_DIR/config.toml" "$CONF_DIR/config.toml.uninstalled-$(date +%Y%m%d-%H%M%S)"
        echo "  your voxtype config was kept as $CONF_DIR/config.toml.uninstalled-*"
    fi
    rm -f "$LANG_FILE" "$(dirname "$LANG_FILE")/speakers" "$(dirname "$LANG_FILE")/remember-voices"
    rmdir "$(dirname "$LANG_FILE")" 2>/dev/null || true
    rm -f "$DROPIN" "$DROPIN_DIR/10-log-warn.conf"
    rmdir "$DROPIN_DIR" 2>/dev/null || true
    systemctl --user daemon-reload 2>/dev/null || true
    rm -f "$VT_MODELS/gtcrn_simple.onnx"
    rm -rf "$HOME/.cache/voxtype/openvino"      # NPU compile cache (~1 GB+), rebuilt on demand
    rmdir "$VT_MODELS" "$HOME/.cache/voxtype" 2>/dev/null || true
    echo "  voxtype per-user files removed (meeting transcripts and the database kept)"
}

render_config() {  # the template with this user's model dir and first language
    sed -e "s|@MODELS_DIR@|$MODELS_DIR|g" -e "s|@LANGUAGE@|$1|g" "$HERE/config.toml"
}

reset_config=
case "${1:-}" in
    --uninstall) uninstall; exit 0 ;;
    --reset-config) reset_config=1 ;;
    "") ;;
    *) echo "usage: $0 [--reset-config | --uninstall]" >&2; exit 2 ;;
esac
case "$MODELS_DIR" in *[\|\&\\]*) fail "NPU_MODELS_DIR must not contain | & or \\" ;; esac

step "1/8 prerequisites (system part: sudo bash system/install.sh)"
[ "$(readlink -f /usr/bin/voxtype)" = /usr/lib/voxtype/voxtype-onnx-avx2 ] \
    || fail "/usr/bin/voxtype is not the onnx-avx2 variant - run: sudo bash system/install.sh"
[ -e "$SDK_LIB" ] || fail "$SDK_LIB missing - run: sudo bash system/install.sh"
[ -f "$MODEL/openvino_encoder_model.xml" ] && [ -f "$MODEL/preprocessor_config.json" ] \
    || fail "Whisper model incomplete: $MODEL (the top-level install.sh downloads it)"
command -v ydotool >/dev/null || fail "ydotool not installed - run: sudo bash system/install.sh"
if [ -r /dev/accel/accel0 ] && [ -w /dev/accel/accel0 ]; then
    echo "  /dev/accel/accel0 accessible"
else
    echo "  WARNING: no rw access to /dev/accel/accel0 - voxtype will fall back to the CPU"
fi
voxtype --version

step "2/8 dictation languages -> $LANG_FILE"
mkdir -p "$(dirname "$LANG_FILE")"
if [ ! -e "$LANG_FILE" ] && [ -f "$OLD_LANG_FILE" ]; then
    mv "$OLD_LANG_FILE" "$LANG_FILE"
    rmdir "$(dirname "$OLD_LANG_FILE")" 2>/dev/null || true
fi
if [ -n "${BMEET_LANGUAGES:-}" ]; then
    langs="$(printf '%s\n' "$BMEET_LANGUAGES" | tr 'A-Z,' 'a-z ' | xargs -n1 \
             | grep -xE '[a-z]{2,3}' | awk '!seen[$0]++' | xargs || true)"
    [ -n "$langs" ] || fail "--languages: expected Whisper language codes like \"en de\""
    echo "$langs" > "$LANG_FILE"
elif [ ! -s "$LANG_FILE" ]; then
    # English plus the desktop language, if Whisper knows it (LANG=de_DE.UTF-8 -> "en de").
    loc="$(printf '%s' "${LANG:-en}" | sed -nE 's/^([A-Za-z]{2,3})([_.@].*)?$/\1/p' | tr 'A-Z' 'a-z')"
    case "$loc" in nb|nn) loc=no ;; fil) loc=tl ;; esac   # locale code -> Whisper code
    if [ -n "$loc" ] && [ "$loc" != en ] && [[ " $WHISPER_LANGS " == *" $loc "* ]]; then
        echo "en $loc"
    else
        echo "en"
    fi > "$LANG_FILE"
fi
first_lang="$(tr 'A-Z,' 'a-z ' < "$LANG_FILE" | xargs -n1 | grep -m1 -xE '[a-z]{2,3}' || echo en)"
echo "  languages: $(cat "$LANG_FILE")  (Meta+Shift+H cycles through them)"

step "3/8 config -> $CONF_DIR/config.toml"
install -d -m 0755 "$CONF_DIR"
new="$(mktemp)"
trap 'rm -f "$new"' EXIT
render_config "${first_lang:-en}" > "$new"
if [ -f "$CONF_DIR/config.toml" ] && [ -z "$reset_config" ] \
   && head -n1 "$CONF_DIR/config.toml" | grep -qF -e "$CONFIG_MARKER" -e "$OLD_CONFIG_MARKER"; then
    sed -i "1s|^$OLD_CONFIG_MARKER|$CONFIG_MARKER|" "$CONF_DIR/config.toml"
    echo "  keeping your BMeet config (--reset-config to replace it)"
    # ... but make sure it points at a model that exists (e.g. after NPU_MODELS_DIR moved).
    if [ ! -f "$(voxtype config get openvino.model 2>/dev/null)/openvino_encoder_model.xml" ]; then
        voxtype config set openvino.model "$MODEL" >/dev/null
        echo "  openvino.model -> $MODEL"
    fi
else
    if [ -f "$CONF_DIR/config.toml" ] && ! cmp -s "$new" "$CONF_DIR/config.toml"; then
        bak="$CONF_DIR/config.toml.bak-$(date +%Y%m%d-%H%M%S)"
        cp -p "$CONF_DIR/config.toml" "$bak"
        echo "  existing config saved as $bak"
    fi
    install -m 0644 "$new" "$CONF_DIR/config.toml"
fi
rm -f "$new"; trap - EXIT
echo "  engine=$(voxtype config get engine) model=$(basename "$(voxtype config get openvino.model)")" \
     "device=$(voxtype config get openvino.device) language=$(voxtype config get openvino.language)"

step "4/8 helper commands -> $BIN_DIR"
install -d -m 0755 "$BIN_DIR"
install -m 0755 "$HERE/bin/voxtype-meeting-toggle" "$HERE/bin/voxtype-lang" \
    "$HERE/bin/bmeet-meeting-audio" "$BIN_DIR/"
ls -l "$BIN_DIR/voxtype-meeting-toggle" "$BIN_DIR/voxtype-lang" "$BIN_DIR/bmeet-meeting-audio"

step "5/8 launchers + KDE global shortcuts (Meta+H, Meta+Alt+H, Meta+Alt+M, Meta+Alt+Shift+M, Meta+Shift+H)"
install -d -m 0755 "$APP_DIR" "$KGA_DIR"
for f in $APPS; do
    sed "s|@HOME@|$HOME|g" "$HERE/applications/$f.desktop" > "$APP_DIR/$f.desktop"
    chmod 0644 "$APP_DIR/$f.desktop"
    # kglobalaccel reads X-KDE-Shortcuts from desktop files found in */kglobalaccel/.
    ln -sfn "../applications/$f.desktop" "$KGA_DIR/$f.desktop"
done
refresh_kde   # menu entries (and shortcuts) show up without a re-login
echo "  installed: $(cd "$APP_DIR" && ls voxtype-*.desktop | tr '\n' ' ')"

step "6/8 meeting mode: output folder + echo-cancel model"
install -d -m 0755 "$MEETINGS" "$VT_MODELS"
if ! echo "$GTCRN_SHA256  $VT_MODELS/gtcrn_simple.onnx" | sha256sum -c --status 2>/dev/null; then
    mkdir -p "$CACHE"
    if ! echo "$GTCRN_SHA256  $CACHE/gtcrn_simple.onnx" | sha256sum -c --status 2>/dev/null; then
        curl -fL --retry 3 -o "$CACHE/gtcrn_simple.onnx.part" "$GTCRN_URL"
        mv "$CACHE/gtcrn_simple.onnx.part" "$CACHE/gtcrn_simple.onnx"
    fi
    echo "$GTCRN_SHA256  $CACHE/gtcrn_simple.onnx" | sha256sum -c -
    install -m 0644 "$CACHE/gtcrn_simple.onnx" "$VT_MODELS/gtcrn_simple.onnx"
fi
echo "  transcripts (Markdown): $MEETINGS"

step "7/8 compile whisper-small for the NPU once (cold: ~60 s, then cached)"
# The daemon is restarted below; that would cut a running meeting short without saving it.
case "$(head -n1 "$XDG_RUNTIME_DIR/voxtype/meeting_state" 2>/dev/null || echo idle)" in
    recording|paused) fail "a meeting is being recorded - stop it (Meta+Alt+M) and run this again" ;;
esac
# Stop a running daemon first so two processes do not compile the same model at once.
systemctl --user stop voxtype.service 2>/dev/null || true
clip="$XDG_RUNTIME_DIR/voxtype-selftest-en.wav"
# The clip is only a means to trigger the compile: if it cannot be made (no
# libespeak-ng or ffmpeg), skip the warm-up instead of aborting; the daemon then
# compiles on its first start (~60 s before Meta+H works).
if python3 -I "$HERE/tools/tts_clip.py" en "Voice typing test. The quick brown fox jumps over the lazy dog." "$clip"; then
    before=$(cat "$NPU_BUSY" 2>/dev/null || echo 0)
    start=$(date +%s)
    voxtype transcribe "$clip" || { rm -f "$clip"; fail "test transcription failed (see the log lines above); service not started"; }
    after=$(cat "$NPU_BUSY" 2>/dev/null || echo 0)
    rm -f "$clip"
    echo "  took $(( $(date +%s) - start )) s; NPU busy time grew by $(( (after - before) / 1000 )) ms" \
         "(~0 ms = it ran on the CPU - look for 'fell back' above; other NPU users inflate it)"
else
    rm -f "$clip"
    echo "  WARNING: could not make the synthetic test clip - warm-up skipped;" \
         "the daemon compiles for the NPU on its first start instead (~60 s)"
fi

step "8/8 start the voxtype user service"
install -d -m 0755 "$DROPIN_DIR"
rm -f "$DROPIN_DIR/10-log-warn.conf"   # older name of this drop-in
# RUST_LOG: the packaged unit runs `voxtype -q daemon` (errors only). "warn" adds the
#   NPU->CPU fallback warnings to the journal, but not the INFO lines that quote the
#   start of every dictation.
# GSK_RENDERER/GDK_DISABLE: inherited by the GTK4 recording overlay (a child of the
#   daemon). They make it draw in software (cairo, shared memory) with no GL/Vulkan
#   context, so the overlay never touches the GPU. "dmabuf" must stay in the list: the
#   overlay sets GDK_DISABLE=dmabuf itself (crash workaround for voxtype #656) but only
#   when GDK_DISABLE is unset, so ours replaces it. To let it use the GPU, change the
#   line to GDK_DISABLE=dmabuf and run
#   `systemctl --user daemon-reload && systemctl --user restart voxtype`.
cat > "$DROPIN" <<'EOF'
# Installed by BMeet (voxtype/user-setup.sh), removed by its uninstall.sh
[Service]
Environment=RUST_LOG=voxtype=warn,warn
Environment=GSK_RENDERER=cairo GDK_DISABLE=gl,vulkan,dmabuf
EOF
systemctl --user daemon-reload
systemctl --user enable voxtype.service
systemctl --user restart voxtype.service
# The daemon loads the cached NPU blob (~3 s) before it reports ready; up to ~60 s if
# the warm-up above was skipped and it has to compile first.
for _ in $(seq 1 90); do
    [ "$(voxtype status 2>/dev/null)" = idle ] && break
    sleep 1
done
echo "  voxtype.service: $(systemctl --user is-active voxtype.service), state: $(voxtype status 2>/dev/null || echo '?')"
echo "  ydotool.service: $(systemctl --user is-active ydotool.service)" \
     "(socket: $(ls "$XDG_RUNTIME_DIR/.ydotool_socket" 2>/dev/null || echo missing))"
journalctl --user -u voxtype.service -n 8 --no-pager -o cat 2>/dev/null || true
