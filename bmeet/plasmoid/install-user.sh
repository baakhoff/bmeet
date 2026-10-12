#!/bin/bash
# BMeet panel widget (io.github.baakhoff.bmeet) for the current user - no sudo.
#   install-user.sh                  install, or upgrade if already installed (idempotent)
#   install-user.sh --add-to-panel   also put it on the bottom panel - only when it is newly
#                                    installed (an upgrade never re-adds a removed widget)
#   install-user.sh --uninstall      remove it again (and from every panel)
# Installs into ~/.local/share/plasma/plasmoids/. Without --add-to-panel: right-click the
# panel > Add or Manage Widgets > "BMeet".
set -eu

ID=io.github.baakhoff.bmeet
OLD_ID=io.github.baakhoff.voicenotes   # the same widget before 0.2.0 (BMeet was npu-voicenotes)
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

replace_old_widget() {
    # Put the new widget where the old one was (same panel; same place in its AppletOrder,
    # which Plasma applies on its next start), remove the old one, uninstall its package.
    "${KPT[@]}" -s "$OLD_ID" >/dev/null 2>&1 || return 0
    echo "Replacing the old $OLD_ID widget"
    shell_script "
        var ps = panels(), n = 0;
        for (var i = 0; i < ps.length; ++i) {
            var p = ps[i], olds = p.widgets('$OLD_ID');
            if (olds.length === 0) continue;
            p.currentConfigGroup = ['General'];
            var before = String(p.readConfig('AppletOrder', '')).split(';');
            var at = before.indexOf(String(olds[0].id));
            var w = p.widgets('$ID').length ? p.widgets('$ID')[0] : p.addWidget('$ID');
            for (var j = 0; j < olds.length; ++j) olds[j].remove();
            var ids = String(p.readConfig('AppletOrder', '')).split(';').filter(function (x) {
                return x !== '' && x !== String(w.id) && x !== String(olds[0].id); });
            if (at >= 0) { ids.splice(Math.min(at, ids.length), 0, String(w.id)); p.writeConfig('AppletOrder', ids.join(';')); }
            ++n;
        }
        print('replaced on ' + n + ' panel(s)');
    " || true
    "${KPT[@]}" -r "$OLD_ID" >/dev/null 2>&1 || true
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
        replace_old_widget
        if [ "${1:-}" = --add-to-panel ] && [ -n "$fresh" ]; then
            add_to_panel
        fi
        echo "Note: a BMeet widget that is already on a panel keeps running the old QML"
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
