#!/bin/bash
# BMeet, system part (root). The top-level install.sh runs it with sudo;
# idempotent, safe to run again. Undo with system/uninstall.sh.
#
#   sudo bash system/install.sh [--cache DIR] [--no-npu-driver] [--force]
#
#   --cache DIR       where downloads are kept (default: ~/.cache/bmeet/downloads
#                     of the user who ran sudo; files are fetched as that user)
#   --no-npu-driver   do not install Intel's NPU user-space driver even if it is missing
#   --force           run on something other than Ubuntu 26.04 (untested)
#
# What it changes:
#   apt:   ffmpeg python3-venv python3-pyqt6 wl-clipboard libnotify-bin desktop-file-utils
#          libespeak-ng1 libtbb12 libze1 curl  (whatever is missing; --no-install-recommends)
#          voxtype 1.1.0-1 (pinned .deb, sha256 + upstream GPG signature), ydotool,
#          libgtk4-layer-shell0
#          intel-level-zero-npu + intel-driver-compiler-npu 1.38.0 (Intel's signed .debs from
#          github.com/intel/linux-npu-driver), only if there is an NPU and no NPU driver is
#          installed yet. The firmware package is not needed: the kernel's linux-firmware
#          has the NPU firmware.
#   Signatures are checked with gpgv against the public keys in system/keys/.
#   /usr/bin/voxtype   -> symlink to /usr/lib/voxtype/voxtype-onnx-avx2 (the build with the
#                         OpenVINO engine); the package's wrapper is kept via dpkg-divert
#   /opt/intel/openvino_genai_ubuntu26_2026.4.1.0_x86_64 (+ symlink /opt/intel/openvino_genai)
#                         Intel OpenVINO GenAI C/C++ SDK; nothing added to ld.so paths.
#                         Its GPU plugin is NOT unpacked: voxtype's hard-coded fallback
#                         chain is NPU -> GPU -> CPU, and without the plugin it is
#                         NPU -> CPU, so dictation never lands on the iGPU.
#   /etc/udev/rules.d/70-voxtype-uinput.rules   /dev/uinput for the seat user (uaccess)
#   /etc/systemd/user/ydotool.service.d/10-voxtype.conf   keyboard-only ydotoold
#   ydotool.service is NOT enabled globally (voxtype.service pulls it in for you only)
#   /var/lib/bmeet/   what was already there before, so uninstall.sh only
#                              removes what this script added
# Nothing here touches GPU drivers, Mesa, firmware or kernel parameters.
set -eu

[ "$(id -u)" -eq 0 ] || { echo "Run it with: sudo bash $0"; exit 1; }
TARGET_USER="${SUDO_USER:-}"
[ -n "$TARGET_USER" ] && [ "$TARGET_USER" != root ] \
    || { echo "Run it via sudo from your desktop account (SUDO_USER is empty)."; exit 1; }
TARGET_UID="$(id -u "$TARGET_USER")"
TARGET_HOME="$(getent passwd "$TARGET_USER" | cut -d: -f6)"

DL="$TARGET_HOME/.cache/bmeet/downloads"
NPU_DRIVER=1
FORCE=
while [ $# -gt 0 ]; do
    case "$1" in
        --cache) DL="${2:?--cache needs a directory}"; shift ;;
        --no-npu-driver) NPU_DRIVER= ;;
        --force) FORCE=1 ;;
        -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
        *) echo "unknown option: $1 (see --help)" >&2; exit 2 ;;
    esac
    shift
done

# --- pinned artifacts (sha256 computed after downloading them, 2026-10-11) -------------
VOXTYPE_VER=1.1.0-1
VOXTYPE_DEB=voxtype_1.1.0-1_amd64.deb
VOXTYPE_URL=https://github.com/peteonrails/voxtype/releases/download/v1.1.0/$VOXTYPE_DEB
VOXTYPE_SHA256=36d1bd3c869a2464b53f9fe60e03820a54cc87c83b2c7190af2b9d1214387b69
VOXTYPE_SIGNER=9CCF7915B750CAE8B095ED1AA3FC9F33FD209279   # "Voxtype Release Signing"
ONNX_AVX2_SHA256=07ee9863f1d611ee3fc638f9ba766f3bd38783b9a97b276d7eceb9b995a7167b  # = SHA256SUMS.txt

SDK_TGZ=openvino_genai_ubuntu26_2026.4.1.0_x86_64.tar.gz
SDK_URL=https://storage.openvinotoolkit.org/repositories/openvino_genai/packages/2026.4.1/linux/$SDK_TGZ
SDK_SHA256=af724fc57b1999040c596e883e890db77c3ac48c3779a0143cb89f017a138ba6  # = Intel's .sha256
SDK_DIR=/opt/intel/openvino_genai_ubuntu26_2026.4.1.0_x86_64
SDK_LINK=/opt/intel/openvino_genai
GPU_PLUGIN=runtime/lib/intel64/libopenvino_intel_gpu_plugin.so   # relative to $SDK_DIR

NPU_VER=1.38.0.20260910-34487311128
NPU_TGZ=linux-npu-driver-v$NPU_VER-ubuntu2604.tar.gz
NPU_URL=https://github.com/intel/linux-npu-driver/releases/download/v1.38.0/$NPU_TGZ
NPU_TGZ_SHA256=83685e6d92f4db1ca65e442846d487c7feb1335634b33994292ceb38f933bcc7
NPU_L0_DEB="intel-level-zero-npu_${NPU_VER}~ubuntu26.04_amd64.deb"
NPU_L0_SHA256=706a86895b5a635e6b1e631f09cf7c3c50e49d917b3e92a3086039f44657a6e5
NPU_CC_DEB="intel-driver-compiler-npu_${NPU_VER}~ubuntu26.04_amd64.deb"
NPU_CC_SHA256=3557e31034e65dbc61d8ec481b101cae40656e14e7ed3750993f64c49b2d3379
NPU_SIGNER=EA267657A608300C296B8F8AD52C9665A4077678       # Intel linux-npu-driver releases

KEYS="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)/keys"
APT_OPTS=(-o DPkg::Lock::Timeout=600)   # wait for unattended-upgrades instead of failing
BASE_PKGS="ffmpeg python3-venv python3-pyqt6 wl-clipboard libnotify-bin desktop-file-utils pipewire-bin
           libespeak-ng1 libtbb12 libze1 curl"
UDEV_RULE=/etc/udev/rules.d/70-voxtype-uinput.rules
YDO_DROPIN=/etc/systemd/user/ydotool.service.d/10-voxtype.conf
STATE_DIR=/var/lib/bmeet
OLD_STATE_DIRS="/var/lib/npu-voicenotes /var/lib/voxtype-npu-setup"   # names used before 0.2.0

step() { echo; echo "=== $* ==="; }
as_user() {  # run a command as the desktop user inside their session environment
    runuser -u "$TARGET_USER" -- env XDG_RUNTIME_DIR="/run/user/$TARGET_UID" \
        DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$TARGET_UID/bus" "$@"
}
is_installed() { dpkg-query -W -f='${Status}' "$1" 2>/dev/null | grep -q "ok installed"; }

step "0/8 checks"
. /etc/os-release
if [ "${ID:-}" != ubuntu ] && [[ " ${ID_LIKE:-} " != *" ubuntu "* ]]; then
    [ -n "$FORCE" ] || { echo "  This is ${PRETTY_NAME:-unknown}, not Ubuntu. Only Ubuntu 26.04 is supported (--force to try anyway)."; exit 1; }
fi
if [ "${VERSION_ID:-}" != 26.04 ] && [ "${UBUNTU_CODENAME:-}" != resolute ]; then
    [ -n "$FORCE" ] || { echo "  This is ${PRETTY_NAME:-unknown}; the pinned packages are built for Ubuntu 26.04 (--force to try anyway)."; exit 1; }
    echo "  WARNING: ${PRETTY_NAME:-unknown} is not Ubuntu 26.04 - continuing because of --force"
fi
[ "$(uname -m)" = x86_64 ] || { echo "  x86_64 only (Intel Core Ultra)."; exit 1; }
grep -qw avx2 /proc/cpuinfo || { echo "  This CPU has no AVX2; voxtype's OpenVINO build needs it."; exit 1; }
if [ -e /dev/accel/accel0 ]; then
    echo "  NPU: /dev/accel/accel0 (kernel driver $(basename "$(readlink -f /sys/class/accel/accel0/device/driver)"))"
else
    echo "  WARNING: no NPU found (/dev/accel/accel0). Everything still works, but on the CPU."
fi
echo "  user: $TARGET_USER, downloads: $DL"

# Scratch space on disk, not in /tmp: /tmp is often a RAM-backed tmpfs and the .deb
# alone is 375 MB. Removed on any exit, including errors.
WORK="$(mktemp -d /var/tmp/bmeet-install.XXXXXX)"
chmod 0755 "$WORK"   # apt's _apt user must reach $WORK/dl
SDK_TMP=""           # set in step 5; removed here too if the unpack fails half-way
cleanup() {
    rm -rf "$WORK"
    [ -n "$SDK_TMP" ] && rm -rf "$SDK_TMP"
    true
}
trap cleanup EXIT

want_npu_driver=
if [ -n "$NPU_DRIVER" ] && ! is_installed intel-level-zero-npu; then
    if [ -e /dev/accel/accel0 ]; then
        want_npu_driver=1
    else
        echo "  no NPU device - Intel's NPU driver is not installed"
    fi
fi

step "1/8 downloads (as $TARGET_USER) present and unmodified"
as_user mkdir -p "$DL"
fetch() {  # url file: download as the user (not root) if the file is missing
    [ -s "$DL/$2" ] && return 0
    echo "  downloading $1"
    as_user curl -fL --retry 3 --progress-bar -o "$DL/$2.part" "$1"   # no half-written file on failure
    as_user mv "$DL/$2.part" "$DL/$2"
}
fetch "$VOXTYPE_URL" "$VOXTYPE_DEB"
fetch "$VOXTYPE_URL.asc" "$VOXTYPE_DEB.asc"
fetch "$SDK_URL" "$SDK_TGZ"
[ -z "$want_npu_driver" ] || fetch "$NPU_URL" "$NPU_TGZ"
# $DL is in your home, i.e. writable by every process running as you. Copy the files
# into root-owned scratch space FIRST and check/use only those copies, so nothing can
# swap a file between the check and its use as root (time-of-check/time-of-use). They
# are read AS YOU, so a symlink planted in $DL cannot make root copy a file you could
# not read yourself. World-readable copies, so apt's _apt sandbox user can read the .debs.
VDL="$WORK/dl"
install -d -m 0755 "$VDL"
copy_in() {  # file sha256 (empty: not pinned, e.g. a signature)
    as_user cat "$DL/$1" > "$VDL/$1"
    chmod 0644 "$VDL/$1"
    [ -n "$2" ] || return 0
    if ! echo "$2  $VDL/$1" | sha256sum -c --quiet - 2>/dev/null; then
        as_user rm -f "$DL/$1"
        echo "  $1: wrong sha256 (corrupt or changed download) - deleted it; run the installer again"
        exit 1
    fi
    echo "  $1: sha256 OK"
}
copy_in "$VOXTYPE_DEB" "$VOXTYPE_SHA256"
copy_in "$VOXTYPE_DEB.asc" ""
copy_in "$SDK_TGZ" "$SDK_SHA256"
if [ -n "$want_npu_driver" ]; then
    copy_in "$NPU_TGZ" "$NPU_TGZ_SHA256"
    tar -xzf "$VDL/$NPU_TGZ" -C "$VDL" --no-same-owner \
        "./$NPU_L0_DEB" "./$NPU_L0_DEB.asc" "./$NPU_CC_DEB" "./$NPU_CC_DEB.asc"
    chmod 0644 "$VDL"/*.deb "$VDL"/*.asc
    echo "$NPU_L0_SHA256  $VDL/$NPU_L0_DEB" | sha256sum -c -
    echo "$NPU_CC_SHA256  $VDL/$NPU_CC_DEB" | sha256sum -c -
fi
# Upstream signatures, checked with gpgv (part of every Ubuntu install: apt needs it)
# against the public keys shipped in system/keys/ - no keyserver, nothing imported.
# voxtype: the release key upstream pins in its PKGBUILD and CI workflow.
# NPU driver: the key that signs Intel's linux-npu-driver releases.
dearmor() {  # armored public key -> binary keyring for gpgv
    python3 -I - "$1" "$2" <<'EOF'
import base64, sys
lines = open(sys.argv[1]).read().splitlines()
i = next(n for n, l in enumerate(lines) if l.startswith("-----BEGIN PGP PUBLIC KEY BLOCK")) + 1
while lines[i].strip():  # armor headers end at the first blank line
    i += 1
body = []
for line in lines[i + 1:]:
    if line.startswith(("=", "-----END")):  # CRC line / end of the block
        break
    body.append(line.strip())
open(sys.argv[2], "wb").write(base64.b64decode("".join(body)))
EOF
}
verify_sig() {  # fingerprint keyfile file...
    local fpr=$1 key=$2 f; shift 2
    dearmor "$KEYS/$key" "$WORK/$key.gpg"
    for f in "$@"; do
        if gpgv --status-fd 1 --keyring "$WORK/$key.gpg" "$f.asc" "$f" 2>/dev/null \
                | grep -q "^\[GNUPG:\] VALIDSIG $fpr "; then
            echo "  good signature by $fpr: $(basename "$f")"
        else
            echo "  signature check FAILED for $(basename "$f")"; exit 1
        fi
    done
}
command -v gpgv >/dev/null || { echo "  gpgv is missing (sudo apt install gpgv)"; exit 1; }
verify_sig "$VOXTYPE_SIGNER" voxtype-release.asc "$VDL/$VOXTYPE_DEB"
[ -z "$want_npu_driver" ] || verify_sig "$NPU_SIGNER" intel-npu-driver.asc "$VDL/$NPU_L0_DEB" "$VDL/$NPU_CC_DEB"

step "2/8 apt: dependencies, voxtype $VOXTYPE_VER, ydotool, libgtk4-layer-shell0${want_npu_driver:+, NPU driver}"
# Remember what was NOT installed before the first run (never overwritten by later
# runs), and whether ydotoold was enabled for all users, so uninstall.sh can restore it.
for old in $OLD_STATE_DIRS; do
    if [ -d "$old" ] && [ ! -e "$STATE_DIR" ]; then
        mv "$old" "$STATE_DIR"
    fi
done
install -d -m 0755 "$STATE_DIR"
if [ ! -e "$STATE_DIR/preexisting" ]; then
    {
        for p in voxtype ydotool libgtk4-layer-shell0 intel-level-zero-npu intel-driver-compiler-npu; do
            is_installed "$p" && echo "pkg $p"
        done
        systemctl --global is-enabled ydotool.service >/dev/null 2>&1 && echo "ydotool-global-enabled"
        [ -e "$SDK_DIR" ] && echo "sdk-dir"   # uninstall.sh must not delete an SDK it did not add
        true
    } > "$STATE_DIR/preexisting"
fi
missing=""
for p in $BASE_PKGS ydotool libgtk4-layer-shell0; do
    is_installed "$p" || missing="$missing $p"
done
debs=""
[ "$(dpkg-query -W -f='${Status} ${Version}' voxtype 2>/dev/null)" = "install ok installed $VOXTYPE_VER" ] \
    || debs="$VDL/$VOXTYPE_DEB"
[ -z "$want_npu_driver" ] || debs="$debs $VDL/$NPU_L0_DEB $VDL/$NPU_CC_DEB"
# wtype (a voxtype Recommends) is skipped on purpose: KWin lacks the virtual-keyboard
# protocol. --no-remove: abort instead of removing anything, should apt ever want to
# (-y would otherwise accept removals silently).
if [ -n "$missing$debs" ]; then
    apt-get "${APT_OPTS[@]}" update -qq
    # shellcheck disable=SC2086  # word splitting of the package lists is intended
    apt-get "${APT_OPTS[@]}" install -y --no-remove --no-install-recommends $missing $debs
else
    echo "  everything already installed"
fi
rm -f "$VDL/$VOXTYPE_DEB"   # free 375 MB of /var/tmp early
# The ydotool postinst enables ydotoold for EVERY user session. Undo that right away
# (step 7 does it again), so an abort in steps 3-6 does not leave it like that.
systemctl --global disable ydotool.service 2>/dev/null || true
echo "$ONNX_AVX2_SHA256  /usr/lib/voxtype/voxtype-onnx-avx2" | sha256sum -c -

step "3/8 select the onnx-avx2 binary (the one with the OpenVINO engine)"
# The package's /usr/bin/voxtype is a wrapper that picks voxtype-avx2 (whisper.cpp only).
# Upstream switches variants by replacing /usr/bin/voxtype (`voxtype setup variant --to
# voxtype-onnx-avx2`). We do the same, but divert the package's file first so a later
# .deb upgrade cannot silently put the wrapper back. Never run `voxtype setup gpu
# --enable` afterwards: it switches to the Vulkan whisper.cpp binary, which has no
# OpenVINO engine.
if ! dpkg-divert --list /usr/bin/voxtype | grep -q voxtype.distrib; then
    dpkg-divert --local --rename --divert /usr/bin/voxtype.distrib --add /usr/bin/voxtype
fi
ln -sfn /usr/lib/voxtype/voxtype-onnx-avx2 /usr/bin/voxtype
/usr/bin/voxtype --version
echo "  /usr/bin/voxtype -> $(readlink -f /usr/bin/voxtype)"

step "4/8 NPU user-space driver"
if is_installed intel-level-zero-npu; then
    echo "  intel-level-zero-npu $(dpkg-query -W -f='${Version}' intel-level-zero-npu)," \
         "intel-driver-compiler-npu $(dpkg-query -W -f='${Version}' intel-driver-compiler-npu 2>/dev/null || echo MISSING)"
else
    echo "  not installed (--no-npu-driver): voxtype and npu-transcribe will use the CPU"
fi

step "5/8 OpenVINO GenAI C/C++ SDK 2026.4.1 -> $SDK_DIR"
# voxtype dlopen()s libopenvino_genai_c.so from config `openvino_dir`; the pip wheel
# does not ship that C library. Kept self-contained under /opt/intel: no ldconfig,
# no LD_LIBRARY_PATH, no system library replaced.
if [ -e "$SDK_LINK" ] && [ ! -L "$SDK_LINK" ]; then
    echo "  $SDK_LINK exists and is not a symlink - refusing to touch it"; exit 1
fi
if [ -L "$SDK_LINK" ] && [ "$(readlink "$SDK_LINK")" != "$SDK_DIR" ]; then
    echo "  $SDK_LINK points to $(readlink "$SDK_LINK") (another OpenVINO?) - refusing to change it"; exit 1
fi
if [ ! -e "$SDK_DIR/runtime/lib/intel64/libopenvino_genai_c.so.2026.4.1.0" ]; then
    install -d -m 0755 /opt/intel
    SDK_TMP="$(mktemp -d /opt/intel/.sdk-unpack.XXXXXX)"
    # The GPU plugin is left out on purpose (see header): then OpenVINO has no "GPU"
    # device and voxtype's NPU -> GPU -> CPU fallback goes straight NPU -> CPU.
    # (Archive checked: one top-level dir, no absolute/.. paths or links, no setuid
    # bits.) Unpacked from the verified root-owned copy, not from $DL.
    tar -xzf "$VDL/$SDK_TGZ" -C "$SDK_TMP" --no-same-owner \
        --exclude="$(basename "$SDK_DIR")/$GPU_PLUGIN"
    chmod -R u+rwX,go+rX,go-w "$SDK_TMP"    # the tarball has two world-writable files
    rm -rf "$SDK_DIR"
    mv "$SDK_TMP/$(basename "$SDK_DIR")" "$SDK_DIR"
    rmdir "$SDK_TMP"
    SDK_TMP=""
fi
rm -f "$SDK_DIR/$GPU_PLUGIN"   # also for an SDK unpacked by an older version of this script
ln -sfn "$SDK_DIR" "$SDK_LINK"
cat "$SDK_DIR/runtime/version.txt"
echo "  $SDK_LINK -> $(readlink "$SDK_LINK")  ($(du -sh "$SDK_DIR" | cut -f1))"

step "6/8 /dev/uinput for the logged-in seat user (ydotoold sends the paste keystroke)"
# uaccess = ACL only for the user at the active local seat; no `input` group needed.
# Note: the ydotool package itself adds 80-uinput.rules (GROUP="input", MODE="0660").
# Security trade-off of either rule: any process you run can create a virtual keyboard
# (inject keystrokes).
cat > "$UDEV_RULE" <<'EOF'
# voxtype: let the active seat user's ydotoold create its virtual keyboard.
# Installed by BMeet (system/install.sh), removed by system/uninstall.sh
KERNEL=="uinput", SUBSYSTEM=="misc", TAG+="uaccess", OPTIONS+="static_node=uinput"
EOF
udevadm control --reload-rules
udevadm trigger --action=change --sysname-match=uinput
udevadm settle
getfacl -p /dev/uinput 2>/dev/null | grep -E "^user:" || ls -l /dev/uinput

step "7/8 ydotoold: keyboard only, started on demand by voxtype.service"
install -d -m 0755 "$(dirname "$YDO_DROPIN")"
cat > "$YDO_DROPIN" <<'EOF'
# voxtype only needs key events (Ctrl+V); no virtual mouse.
# Installed by BMeet (system/install.sh), removed by system/uninstall.sh
[Service]
ExecStart=
ExecStart=/usr/bin/ydotoold --mouse-off
EOF
# Ubuntu's ydotool package enables ydotoold for EVERY user session. Undo that; the
# voxtype user unit has Wants=ydotool.service, so it still starts for you.
systemctl --global disable ydotool.service 2>/dev/null || true
as_user systemctl --user daemon-reload || true

step "8/8 NPU access for $TARGET_USER"
if [ -e /dev/accel/accel0 ]; then
    # Ubuntu's systemd tags accel devices "uaccess" (ACL for the seat user); otherwise the
    # `render` group grants it.
    if getfacl -p /dev/accel/accel0 2>/dev/null | grep -q "^user:$TARGET_USER:rw" \
       || id -nG "$TARGET_USER" | grep -qw "$(stat -c %G /dev/accel/accel0)"; then
        echo "  ok: $TARGET_USER can use /dev/accel/accel0"
    else
        echo "  WARNING: $TARGET_USER has no access to /dev/accel/accel0 ($(stat -c '%U:%G %a' /dev/accel/accel0))."
        echo "  Fix: sudo usermod -aG $(stat -c %G /dev/accel/accel0) $TARGET_USER  (then log out and in)"
    fi
fi
echo; echo "System part done."
