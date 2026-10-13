#!/usr/bin/env bash
# BMeet's Chrome extension (extension/): it tells BMeet when your Meet or Slack call cannot
# hear your microphone, so meeting transcripts leave out what you said while muted.
# Installs, for the current user only (no sudo):
#   ~/.local/bin/bmeet-chrome-host -> this tree (the native messaging host)
#   <browser>/NativeMessagingHosts/io.github.baakhoff.bmeet.json for each Chromium-based
#   browser with a profile here (Google Chrome, Chromium, Brave, Edge, Vivaldi)
# The extension itself is loaded once by hand (browsers do not let a script add it):
#   chrome://extensions -> Developer mode -> Load unpacked -> this tree's extension/ folder.
#
# Usage: ./install-user.sh              install / update (idempotent)
#        ./install-user.sh --uninstall
set -euo pipefail

here=$(cd "$(dirname "$(readlink -f "$0")")" && pwd)
bin_dir="$HOME/.local/bin"
config="${XDG_CONFIG_HOME:-$HOME/.config}"
name=io.github.baakhoff.bmeet
id=pfhnhicfaajjfidmknapnoekclpjoaoc   # fixed by the "key" in extension/manifest.json
browsers=(google-chrome google-chrome-beta google-chrome-unstable chromium
          BraveSoftware/Brave-Browser microsoft-edge vivaldi)

case ${1:-} in
    --uninstall)
        for b in "${browsers[@]}"; do
            rm -fv "$config/$b/NativeMessagingHosts/$name.json"
        done
        if [[ -L $bin_dir/bmeet-chrome-host ]]; then rm -v "$bin_dir/bmeet-chrome-host"; fi
        exit 0 ;;
    "") ;;
    *) echo "usage: $0 [--uninstall]" >&2; exit 2 ;;
esac

mkdir -p "$bin_dir"
ln -sfn "$here/bmeet-chrome-host" "$bin_dir/bmeet-chrome-host"
found=()
for b in "${browsers[@]}"; do
    [[ -d $config/$b ]] || continue
    install -d -m 0755 "$config/$b/NativeMessagingHosts"
    cat > "$config/$b/NativeMessagingHosts/$name.json" <<JSON
{
  "name": "$name",
  "description": "BMeet: can your browser call hear your microphone",
  "path": "$bin_dir/bmeet-chrome-host",
  "type": "stdio",
  "allowed_origins": ["chrome-extension://$id/"]
}
JSON
    found+=("$b")
done
if ((${#found[@]})); then
    echo "  native host for: ${found[*]}"
else
    echo "  no Chromium-based browser profile found; run this again after installing one"
fi
echo "  extension (load once): chrome://extensions -> Developer mode -> Load unpacked -> $here/extension"
