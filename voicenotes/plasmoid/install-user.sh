#!/bin/bash
# Voice Notes panel widget (io.github.baakhoff.voicenotes) for the current user - no sudo.
#   install-user.sh                  install, or upgrade if already installed (idempotent)
#   install-user.sh --add-to-panel   also put it on the bottom panel - only when it is newly
#                                    installed (an upgrade never re-adds a removed widget)
#   install-user.sh --uninstall      remove it again (and from every panel)
# Installs into ~/.local/share/plasma/plasmoids/. Without --add-to-panel: right-click the
# panel > Add or Manage Widgets > "Voice Notes".
set -eu

ID=io.github.baakhoff.voicenotes
D=$(dirname "$(readlink -f "$0")")
PKG="$D/$ID"
KPT=(kpackagetool6 -t Plasma/Applet)

[ "$(id -u)" -ne 0 ] || { echo "Run this as your own user, not as root." >&2; exit 1; }
command -v kpackagetool6 >/dev/null || { echo "kpackagetool6 not found (package: kpackage)." >&2; exit 1; }

installed() { "${KPT[@]}" -s "$ID" >/dev/null 2>&1; }

qdbus_cmd() {  # Qt 6 qdbus has a different name on different distros
    local q
    for q in qdbus6 qdbus-qt6 /usr/lib/qt6/bin/qdbus; do
        command -v "$q" >/dev/null && { echo "$q"; return 0; }
    done
    return 1
}

shell_script() {  # run a Plasma shell script (same API as the desktop scripting console)
    local q
    q=$(qdbus_cmd) || { echo "  (qdbus6 not found - skipped)"; return 1; }
    "$q" org.kde.plasmashell /PlasmaShell org.kde.PlasmaShell.evaluateScript "$1"
}

add_to_panel() {
    # The bottom panel (else the first panel). The widget is appended at the panel's
    # end; move it in the panel's edit mode.
    shell_script "
        var ps = panels(), target = null;
        for (var i = 0; i < ps.length; ++i) {
            if (ps[i].widgets('$ID').length > 0) { print('already on panel ' + ps[i].id); target = -1; break; }
            if (target === null || ps[i].location === 'bottom') target = ps[i];
        }
        if (target === null) print('no panel found');
        else if (target !== -1) { target.addWidget('$ID'); print('added to panel ' + target.id); }
    " || true
}

remove_from_panels() {
    shell_script "
        var ps = panels(), n = 0;
        for (var i = 0; i < ps.length; ++i) {
            var ws = ps[i].widgets('$ID');
            for (var j = 0; j < ws.length; ++j) { ws[j].remove(); ++n; }
        }
        print('removed ' + n + ' widget(s) from panels');
    " >/dev/null 2>&1 || true
}

case "${1:-}" in
    ""|--add-to-panel)
        [ -f "$PKG/metadata.json" ] || { echo "Package not found: $PKG" >&2; exit 1; }
        fresh=
        if installed; then
            echo "Upgrading $ID from $PKG"
            "${KPT[@]}" -u "$PKG"
        else
            echo "Installing $ID from $PKG"
            "${KPT[@]}" -i "$PKG"
            fresh=1
        fi
        "${KPT[@]}" -s "$ID" | sed -n '1,4p'
        if [ "${1:-}" = --add-to-panel ] && [ -n "$fresh" ]; then
            add_to_panel
        fi
        echo "Note: a Voice Notes widget that is already on a panel keeps running the old QML"
        echo "until plasmashell restarts:  systemctl --user restart plasma-plasmashell"
        ;;
    --uninstall)
        if installed; then
            remove_from_panels
            "${KPT[@]}" -r "$ID"
            echo "Removed $ID."
        else
            echo "$ID is not installed - nothing to do."
        fi
        ;;
    -h|--help)
        sed -n '2,8p' "$0"
        ;;
    *)
        echo "usage: $0 [--add-to-panel | --uninstall]" >&2
        exit 2
        ;;
esac
