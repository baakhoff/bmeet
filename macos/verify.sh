#!/usr/bin/env bash
# BMeet health check on macOS: is every part installed and answering?
#   bash ~/.local/share/bmeet/src/macos/verify.sh
set -uo pipefail
data="${XDG_DATA_HOME:-$HOME/.local/share}"
root="$data/bmeet"
models="${NPU_MODELS_DIR:-$data/npu-models}"
config="${XDG_CONFIG_HOME:-$HOME/.config}/bmeet"
py="$root/venv/bin/python"
src="$root/src"
fails=0
ok() { printf '  \033[32mok\033[0m   %s\n' "$*"; }
bad() { printf '  \033[31mFAIL\033[0m %s\n' "$*"; fails=$((fails + 1)); }
note() { printf '  --   %s\n' "$*"; }
check() { if eval "$2" >/dev/null 2>&1; then ok "$1"; else bad "$1${3:+ - $3}"; fi; }

echo "BMeet $(cat "$src/VERSION" 2>/dev/null || echo '?') on macOS $(sw_vers -productVersion), $(uname -m)"
check "Python environment" "'$py' -I -c 'import numpy, coremltools, PyQt6.QtWidgets'" "run the installer again"
check "ffmpeg" "command -v ffmpeg || test -x /opt/homebrew/bin/ffmpeg" "brew install ffmpeg"
check "BMeet.app" "test -x '$HOME/Applications/BMeet.app/Contents/MacOS/BMeet'"
check "the BMeet window" "test -x '$HOME/Applications/BMeet.app/Contents/Helpers/BMeet Window.app/Contents/MacOS/BMeet'"
check "bmeet-whisper, bmeet-record" "test -x '$root/bin/bmeet-whisper' && test -x '$root/bin/bmeet-record'"
# (turbo, GigaAM and the speaker models can be left out on purpose: --no-turbo, --no-gigaam, --no-speakers)
turbo="$models/whisperkit/openai_whisper-large-v3-v20240930_626MB/AudioEncoder.mlmodelc"
if [ -d "$turbo" ]; then ok "Whisper turbo (WhisperKit)"
else note "Whisper turbo not installed (--no-turbo): Whisper small everywhere"; fi
check "Whisper small (translation)" "test -d '$models/whisperkit/openai_whisper-small_216MB/AudioEncoder.mlmodelc'" "run the installer again"
if [ -d "$models/gigaam-v3-ctc-ru/model.mlmodelc" ]; then ok "GigaAM v3 (Russian)"
else note "GigaAM v3 not installed (--no-gigaam): Russian is transcribed with Whisper"; fi
if [ "$(head -n1 "$config/speakers" 2>/dev/null)" != off ]; then
    check "speaker models" "test -d '$models/speaker-diarization/nemotron-3-diarization/encoder.mlmodelc' && test -d '$models/speaker-diarization/titanet.mlmodelc'" "run the installer again"
else
    note "telling speakers apart is off: meetings are labelled You / Remote (~/.config/bmeet/speakers)"
fi
check "commands in ~/.local/bin" "test -x '$HOME/.local/bin/npu-transcribe' && test -x '$HOME/.local/bin/bmeet-ctl'"
status="$("$HOME/.local/bin/bmeet-ctl" status 2>/dev/null)"
if echo "$status" | grep -q '"daemon": "active"'; then ok "BMeet.app is running"; else bad "BMeet.app is not running - open it from ~/Applications"; fi
answer="$(printf '{"op": "status"}\n' | nc -U "$(getconf DARWIN_USER_TEMP_DIR)bmeet/agent.sock" 2>/dev/null)"
case "$answer" in
    *'"accessibility":true'*) ok "pasting allowed (Accessibility)" ;;
    *'"accessibility":false'*) note "pasting not allowed yet: dictations go to the clipboard (System Settings -> Privacy & Security -> Accessibility -> BMeet)" ;;
esac
echo "  speech on the Neural Engine:"
if "$py" -I - "$turbo" <<'PY'
import os, sys, time
sys.path.insert(0, os.path.expanduser("~/.local/share/bmeet/src/macos"))
import bmeetmac, numpy as np
model = "turbo" if os.path.isdir(sys.argv[1]) else "small"   # (--no-turbo: only small is there)
t0 = time.monotonic()
a = bmeetmac.whisper({"op": "transcribe", "model": model, "language": "en"}, np.zeros(16000, np.float32), timeout=600)
print(f"  ok   Whisper {model} answers on {a.get('device')} ({time.monotonic() - t0:.1f} s)")
PY
then :; else bad "Whisper (bmeet-whisper) does not answer: see $(getconf DARWIN_USER_TEMP_DIR)bmeet/whisper.log"; fi
for b in "Google/Chrome" "Chromium" "BraveSoftware/Brave-Browser" "Microsoft Edge" "Vivaldi"; do
    f="$HOME/Library/Application Support/$b/NativeMessagingHosts/io.github.baakhoff.bmeet.json"
    [ -f "$f" ] && ok "Chrome extension's helper for $b"
done
[ "$fails" -eq 0 ] && echo "All good." || echo "$fails problem(s)."
exit $((fails > 0))
