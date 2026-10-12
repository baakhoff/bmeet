#!/usr/bin/env bash
# BMeet tests, all offscreen (QT_QPA_PLATFORM=offscreen); see test_bmeet.py.
#
#   tests/run-tests.sh           everything, incl. real npu-transcribe runs (Whisper small on the
#                                NPU, synthetic espeak-ng speech; ~20 s with a warm compile cache)
#   tests/run-tests.sh --quick   without the real npu-transcribe runs
#
# Never touches voxtype's real meetings DB (a backup-API copy is used), never records audio,
# never opens anything on the desktop. Work dir: tests/.work (removed afterwards; KEEP_WORK=1).
set -euo pipefail

here=$(cd "$(dirname "$(readlink -f "$0")")" && pwd)
real=1
case ${1:-} in
    --quick) real= ;;
    "") ;;
    *) echo "usage: $0 [--quick]" >&2; exit 2 ;;
esac

REAL_TRANSCRIBE=$real /usr/bin/python3 -I "$here/test_bmeet.py" -v

desktop="${XDG_DATA_HOME:-$HOME/.local/share}/applications/bmeet.desktop"
if [[ -f $desktop ]]; then
    desktop-file-validate "$desktop" | grep -v 'more than one main category' || true
    echo "desktop entry: $desktop OK"
else
    echo "desktop entry not installed (run ../install-user.sh)"
fi
