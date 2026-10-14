#!/usr/bin/env bash
# Runs a command against a private, headless KWin: its own Wayland socket, its own D-Bus session,
# and its config, cache and data folders under WORKDIR, so nothing reaches your desktop, your
# clipboard or your KWin settings. Software rendering (KWIN_COMPOSE=Q).
#
# Usage: kwin-nested.sh WORKDIR [--] COMMAND [ARGS...]
# Env:   NESTED_KWINRC  its kwinrc (printf %b; default: the virtual keyboard slot enabled)
#        NESTED_IM      an input method for KWin to start (--inputmethod)
#        NESTED_X=1     with Xwayland; the command gets its display in NESTED_DISPLAY
# Services the private D-Bus starts (portals, ksecretd) are stopped at the end.
set -u
work=$(realpath -m "${1:?usage: kwin-nested.sh WORKDIR [--] COMMAND...}"); shift
[[ ${1:-} == -- ]] && shift
mkdir -p "$work"/{config,cache,data,state}
printf '%b' "${NESTED_KWINRC:-[Wayland]\\nVirtualKeyboardEnabled=true\\n}" > "$work/config/kwinrc"
sock=bmeet-test-$$
export XDG_CONFIG_HOME=$work/config XDG_CACHE_HOME=$work/cache XDG_DATA_HOME=$work/data XDG_STATE_HOME=$work/state
export KWIN_COMPOSE=Q WAYLAND_DISPLAY=$sock
unset DISPLAY

inner() {
    local before new kw rc
    before=$(ls /tmp/.X11-unix/ 2>/dev/null)
    kwin_wayland --virtual --socket "$sock" --no-lockscreen --no-global-shortcuts --width 1280 --height 800 \
        ${NESTED_IM:+--inputmethod "$NESTED_IM"} ${NESTED_X:+--xwayland} >"$work/kwin.log" 2>&1 &
    kw=$!
    for _ in $(seq 50); do [[ -S $XDG_RUNTIME_DIR/$sock ]] && break; sleep 0.1; done
    sleep 1
    new=$(comm -13 <(echo "$before") <(ls /tmp/.X11-unix/ 2>/dev/null) | head -1)
    export NESTED_DISPLAY=${new:+:${new#X}}
    "$@"
    rc=$?
    kill "$kw" 2>/dev/null
    wait "$kw" 2>/dev/null
    return $rc
}
export -f inner
export sock work
dbus-run-session -- bash -c 'inner "$@"' inner "$@"
rc=$?
for env in /proc/[0-9]*/environ; do
    pid=${env#/proc/}; pid=${pid%/environ}
    [[ $pid == "$$" ]] && continue
    if { tr '\0' '\n' < "$env"; } 2>/dev/null | grep -qx "XDG_CONFIG_HOME=$work/config"; then
        kill "$pid" 2>/dev/null
    fi
done
exit $rc
