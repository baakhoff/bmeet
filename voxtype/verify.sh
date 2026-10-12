#!/bin/bash
# voxtype health check (no sudo, changes nothing except a temp WAV in $XDG_RUNTIME_DIR).
#   bash verify.sh          checks + one synthetic transcription, reports NPU busy time
#   bash verify.sh --watch  print NPU busy time per second (Ctrl+C to stop) - dictate
#                           with Meta+H while it runs; non-zero deltas = NPU working
set -u

NPU_BUSY=/sys/class/accel/accel0/device/npu_busy_time_us
HERE="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
npu_busy() { cat "$NPU_BUSY" 2>/dev/null || echo 0; }   # microseconds; 0 if unreadable

if [ "${1:-}" = "--watch" ]; then
    prev=$(npu_busy)
    echo "NPU busy ms per second (Ctrl+C to stop):"
    while sleep 1; do
        cur=$(npu_busy); echo "$(date +%T)  $(( (cur - prev) / 1000 )) ms"; prev=$cur
    done
fi

ok()   { echo "  [ok]   $*"; }
bad()  { echo "  [FAIL] $*"; FAILED=1; }
info() { echo "  [info] $*"; }
FAILED=0

echo "== binaries and runtime"
v=$(readlink -f /usr/bin/voxtype)
[ "$v" = /usr/lib/voxtype/voxtype-onnx-avx2 ] && ok "/usr/bin/voxtype -> $v" || bad "/usr/bin/voxtype -> $v (want voxtype-onnx-avx2)"
voxtype --version 2>/dev/null | sed 's/^/         /'
lib=/opt/intel/openvino_genai/runtime/lib/intel64/libopenvino_genai_c.so
[ -e "$lib" ] && ok "OpenVINO GenAI C API: $(readlink -f "$lib")" || bad "missing $lib"
gpu=/opt/intel/openvino_genai/runtime/lib/intel64/libopenvino_intel_gpu_plugin.so
[ ! -e "$gpu" ] && ok "no OpenVINO GPU plugin: fallback is NPU -> CPU, never the iGPU" \
    || info "OpenVINO GPU plugin present: an NPU failure would fall back to the iGPU"
[ "$(voxtype config get engine 2>/dev/null)" = openvino ] && ok "engine = openvino, device = $(voxtype config get openvino.device), language = $(voxtype config get openvino.language)" || bad "engine is not openvino"

echo "== devices"
[ -r /dev/accel/accel0 ] && [ -w /dev/accel/accel0 ] && ok "NPU /dev/accel/accel0 rw for $(id -un)" || bad "no rw access to /dev/accel/accel0"
dpkg -l intel-level-zero-npu 2>/dev/null | grep -q '^ii' && ok "intel-level-zero-npu $(dpkg-query -W -f='${Version}' intel-level-zero-npu)" || bad "NPU user-space driver not installed"
getfacl -p /dev/uinput 2>/dev/null | grep -q "^user:$(id -un):rw" && ok "/dev/uinput rw (uaccess ACL) for ydotoold" || bad "/dev/uinput not writable -> paste keystroke will fail"

echo "== services"
for u in voxtype.service ydotool.service; do
    s=$(systemctl --user is-active "$u"); [ "$s" = active ] && ok "$u active" || bad "$u $s"
done
svc_env=$(systemctl --user show -p Environment voxtype.service 2>/dev/null)
case "$svc_env" in *GSK_RENDERER=cairo*) true ;; *) false ;; esac \
    && case "$svc_env" in *GDK_DISABLE=gl,vulkan,dmabuf*) true ;; *) false ;; esac \
    && ok "overlay renders in software (GSK_RENDERER=cairo, GDK_DISABLE=gl,vulkan,dmabuf)" \
    || info "overlay may render on the GPU (no GSK_RENDERER=cairo in the service environment)"
[ -S "$XDG_RUNTIME_DIR/.ydotool_socket" ] && ok "ydotool socket $XDG_RUNTIME_DIR/.ydotool_socket" || bad "no ydotool socket"
info "daemon state: $(voxtype status 2>/dev/null || echo '?'), meeting: $(head -n1 "$XDG_RUNTIME_DIR/voxtype/meeting_state" 2>/dev/null || echo idle)"
if journalctl --user -u voxtype.service -b --no-pager -o cat 2>/dev/null | grep -q "fell back to"; then
    bad "daemon log says it fell back from the NPU this boot:"
    journalctl --user -u voxtype.service -b --no-pager -o cat | grep "fell back to" | tail -n 2 | cut -c1-200 | sed 's/^/         /'
else
    ok "no NPU->GPU/CPU fallback logged by the daemon this boot"
fi

echo "== npu-transcribe and BMeet"
v=$("$HOME/.local/bin/npu-transcribe" --version 2>/dev/null) && ok "$v" || bad "npu-transcribe missing or broken (~/.local/bin/npu-transcribe)"
v=$("$HOME/.local/bin/bmeet-ctl" version 2>/dev/null) && ok "$v" || bad "bmeet-ctl missing (~/.local/bin/bmeet-ctl)"
[ -f "${XDG_DATA_HOME:-$HOME/.local/share}/applications/bmeet.desktop" ] && ok "BMeet app in the menu" || bad "bmeet.desktop missing"
kpackagetool6 -t Plasma/Applet -s io.github.baakhoff.bmeet >/dev/null 2>&1 \
    && ok "panel widget installed (io.github.baakhoff.bmeet)" || bad "panel widget not installed"
for m in whisper-small-int8-ov whisper-large-v3-turbo-int8-ov; do
    d="${NPU_MODELS_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/npu-models}/$m"
    [ -f "$d/openvino_encoder_model.xml" ] && ok "model $m" || info "model $m not installed"
done

echo "== meeting speakers (You / Person 1 / Person 2 ...)"
speakers=$(head -n1 "${XDG_CONFIG_HOME:-$HOME/.config}/bmeet/speakers" 2>/dev/null || echo off)
if [ "$speakers" = on ]; then
    ok "telling speakers apart is on (~/.config/bmeet/speakers)"
    sd="${NPU_MODELS_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/npu-models}/speaker-diarization"
    for m in pyannote-segmentation-3-0.onnx nemo_en_titanet_small.onnx; do
        [ -f "$sd/$m" ] && ok "speaker model $m" || bad "speaker model $m missing (run the installer again)"
    done
    "${XDG_DATA_HOME:-$HOME/.local/share}/bmeet/venv/bin/python" -I -c 'import sherpa_onnx' 2>/dev/null \
        && ok "sherpa-onnx in the venv" || bad "sherpa-onnx missing from the venv (run the installer again)"
    command -v pw-record >/dev/null && [ -x "$HOME/.local/bin/bmeet-meeting-audio" ] \
        && ok "meeting audio recorder (pw-record, bmeet-meeting-audio)" || bad "pw-record or bmeet-meeting-audio missing"
    if [ "$(head -n1 "${XDG_CONFIG_HOME:-$HOME/.config}/bmeet/remember-voices" 2>/dev/null)" = on ]; then
        n=$(python3 -I -c 'import json, sys; print(len(json.load(open(sys.argv[1]))["voices"]))' \
            "${XDG_DATA_HOME:-$HOME/.local/share}/bmeet/voices/voices.json" 2>/dev/null || echo 0)
        info "remembered voices: on, $n person/people (BMeet app: Name speakers… > Remembered voices…)"
    else
        info "remembered voices: off (people are numbered; turn on in the app's Name speakers… dialog)"
    fi
else
    info "telling speakers apart is off: meetings are labelled You / Remote (installer --no-speakers)"
fi

echo "== synthetic transcription (separate process, same config)"
clip="$XDG_RUNTIME_DIR/voxtype-verify.wav"
python3 -I "$HERE/tools/tts_clip.py" en "Testing one two three. Voice typing runs on the neural processing unit." "$clip" >/dev/null \
    || bad "could not make the synthetic test clip (libespeak-ng / ffmpeg?)"
before=$(npu_busy)
out=$(voxtype transcribe "$clip" 2>&1); rc=$?
after=$(npu_busy)
rm -f "$clip"
text=$(printf '%s\n' "$out" | tail -n 1)
delta=$(( (after - before) / 1000 ))
# The last output line is the text only on success; on failure it is an error message.
[ "$rc" -eq 0 ] && [ -n "$text" ] && ok "text: $text" \
    || bad "transcription failed (exit $rc): $(printf '%s\n' "$out" | tail -n 3 | cut -c1-200)"
# The counter is device-wide: other programs using the NPU at the same time add to it.
[ "$delta" -gt 0 ] && ok "NPU busy time grew by $delta ms during the run" || bad "NPU busy time did not move -> ran on CPU"
printf '%s\n' "$out" | grep -E "fell back|ERROR|Error" | cut -c1-200 | sed 's/^/         /'

echo
[ "$FAILED" -eq 0 ] && echo "All checks passed." || echo "Some checks failed - see https://github.com/baakhoff/bmeet/blob/main/docs/troubleshooting.md"
exit "$FAILED"
