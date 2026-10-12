#!/bin/bash
# BMeet, undo the system part (root). Idempotent. The top-level uninstall.sh
# runs it with sudo after removing the per-user parts.
#   sudo bash system/uninstall.sh
#
# Removes: the /usr/bin/voxtype diversion, the voxtype package, ydotool and
# libgtk4-layer-shell0 (each only if system/install.sh added it and nothing else needs
# it), the udev rule, the ydotool drop-in, the OpenVINO GenAI SDK under /opt/intel
# (unless it was there before) and /var/lib/bmeet.
# Kept: general packages (ffmpeg, python3-pyqt6, ...) and Intel's NPU driver, which
# other programs can use. Remove the driver with:
#   sudo apt purge intel-level-zero-npu intel-driver-compiler-npu
set -eu
APT_OPTS=(-o DPkg::Lock::Timeout=600)   # wait for unattended-upgrades instead of failing

[ "$(id -u)" -eq 0 ] || { echo "Run it with: sudo bash $0"; exit 1; }
TARGET_USER="${SUDO_USER:-}"
TARGET_UID="$( [ -n "$TARGET_USER" ] && id -u "$TARGET_USER" || true)"

SDK_DIR=/opt/intel/openvino_genai_ubuntu26_2026.4.1.0_x86_64
SDK_LINK=/opt/intel/openvino_genai
UDEV_RULE=/etc/udev/rules.d/70-voxtype-uinput.rules
YDO_DROPIN=/etc/systemd/user/ydotool.service.d/10-voxtype.conf
STATE_DIR=/var/lib/bmeet
for old in /var/lib/npu-voicenotes /var/lib/voxtype-npu-setup; do  # names used before 0.2.0
    [ -e "$STATE_DIR" ] || [ ! -d "$old" ] || STATE_DIR=$old
done

step() { echo; echo "=== $* ==="; }
# system/install.sh wrote $STATE_DIR/preexisting before its first apt run: "pkg <name>"
# for every package that was already installed, "ydotool-global-enabled" if it was.
was_preexisting() { grep -qx "$1" "$STATE_DIR/preexisting" 2>/dev/null; }

step "1/4 /usr/bin/voxtype back to the package wrapper, then purge voxtype"
if dpkg-divert --list /usr/bin/voxtype | grep -q voxtype.distrib; then
    # While diverted, the package's own wrapper is /usr/bin/voxtype.distrib; whatever is
    # at /usr/bin/voxtype is install.sh's symlink (or something `voxtype setup ...` wrote,
    # possibly a regular file). Remove it, else `--rename --remove` refuses to overwrite.
    rm -f /usr/bin/voxtype
    dpkg-divert --local --rename --divert /usr/bin/voxtype.distrib --remove /usr/bin/voxtype
fi
# Purge only with an install record that says install.sh added it (no record =
# install.sh never ran here, or this script already did).
if [ ! -e "$STATE_DIR/preexisting" ] || was_preexisting "pkg voxtype"; then
    echo "  keeping the voxtype package (not added by system/install.sh, or no install record)"
elif dpkg-query -W -f='${Status}' voxtype 2>/dev/null | grep -q -E 'ok (installed|config-files)'; then
    apt-get "${APT_OPTS[@]}" purge -y voxtype
fi

step "2/4 ydotool + libgtk4-layer-shell0 (only if system/install.sh added them)"
purge_if_ours() {  # purge only if install.sh added it and nothing installed depends on it
    dpkg-query -W -f='${Status}' "$1" 2>/dev/null | grep -q "ok installed" || return 0
    if [ ! -e "$STATE_DIR/preexisting" ] || was_preexisting "pkg $1"; then
        echo "  keeping $1 (not added by system/install.sh, or no install record)"; return 0
    fi
    users="$(apt-cache rdepends --installed --no-recommends --no-suggests --no-enhances "$1" | tail -n +3 | tr -d ' ' | tr '\n' ' ')"
    if [ -n "$users" ]; then echo "  keeping $1 (needed by: $users)"; else apt-get "${APT_OPTS[@]}" purge -y "$1"; fi
}
rm -f "$YDO_DROPIN"; rmdir "$(dirname "$YDO_DROPIN")" 2>/dev/null || true
purge_if_ours ydotool          # its purge also drops the global user-unit enablement
purge_if_ours libgtk4-layer-shell0
# ydotool kept and it was enabled for all users before install.sh disabled that: restore.
if dpkg-query -W -f='${Status}' ydotool 2>/dev/null | grep -q "ok installed" \
   && was_preexisting ydotool-global-enabled; then
    systemctl --global enable ydotool.service
fi
if [ -n "$TARGET_UID" ]; then
    runuser -u "$TARGET_USER" -- env XDG_RUNTIME_DIR="/run/user/$TARGET_UID" \
        systemctl --user daemon-reload 2>/dev/null || true
fi

step "3/4 udev rule for /dev/uinput"
if [ -f "$UDEV_RULE" ]; then
    rm -f "$UDEV_RULE"
    udevadm control --reload-rules
    udevadm trigger --action=change --sysname-match=uinput
    udevadm settle
fi
echo "  (other rules may still grant access, e.g. ydotool's own 80-uinput.rules)"

step "4/4 OpenVINO GenAI SDK under /opt/intel"
if [ -L "$SDK_LINK" ] && [ "$(readlink "$SDK_LINK")" = "$SDK_DIR" ]; then rm -f "$SDK_LINK"; fi
if was_preexisting sdk-dir; then
    echo "  keeping $SDK_DIR (it existed before system/install.sh)"
elif [ -e "$STATE_DIR/preexisting" ] || [ ! -e "$SDK_DIR" ]; then
    rm -rf "$SDK_DIR"
else
    echo "  keeping $SDK_DIR (no install record - remove it by hand if install.sh put it there)"
fi
rm -rf /opt/intel/.sdk-unpack.*   # leftovers of an interrupted install.sh, if any
rmdir /opt/intel 2>/dev/null || true
rm -rf "$STATE_DIR"

echo; echo "System part removed. Kept: the NPU driver and general packages (see the header of $0)."
