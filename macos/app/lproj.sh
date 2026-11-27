#!/usr/bin/env bash
# Puts BMeet.app's translations into its bundle: lproj.sh RESOURCES (BMeet.app/Contents/Resources).
# Each macos/app/<lang>.lproj/*.strings goes there as a binary property list, which plutil also
# checks: a broken table stops here, not in BMeet. Run by macos/install.sh while it assembles
# BMeet.app; macos/tests/test_settings.py runs it on a scratch bundle.
set -euo pipefail

resources="${1:?usage: lproj.sh BMeet.app/Contents/Resources}"
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
for lproj in "$here"/*.lproj; do
    dest="$resources/$(basename "$lproj")"
    mkdir -p "$dest"
    for strings in "$lproj"/*.strings; do
        plutil -convert binary1 -o "$dest/$(basename "$strings")" "$strings"
    done
done
