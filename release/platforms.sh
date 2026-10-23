# shellcheck shell=bash
# BMeet's release lines: one per platform, each with its own version, changelog, tags and
# GitHub workflow. Sourced by the scripts in release/ (bash 3.2: they run on a Mac too).
#
# Adding a platform (Windows): a case in platform_info and version_lines, its VERSION file
# and CHANGELOG.md, a .github/workflows/release-PLATFORM.yml like release-macos.yml, and
# its row in docs/releasing.md.

PLATFORMS="linux macos"
# shellcheck disable=SC2034  # (used by the scripts that source this file)
REPO=baakhoff/bmeet

# platform_info PLATFORM: sets NAME, VERSION_FILE, CHANGELOG, TAG_PREFIX, LATEST, WORKFLOW
# shellcheck disable=SC2034  # (the scripts that source this file use them)
platform_info() {
    case "$1" in
        linux)
            NAME="BMeet" VERSION_FILE=VERSION CHANGELOG=CHANGELOG.md TAG_PREFIX=v
            LATEST=true WORKFLOW=release-linux.yml ;;
        macos)
            NAME="BMeet for macOS" VERSION_FILE=macos/VERSION CHANGELOG=macos/CHANGELOG.md
            TAG_PREFIX=macos-v LATEST=false WORKFLOW=release-macos.yml ;;
        *)
            echo "unknown platform: $1 (one of: $PLATFORMS)" >&2
            return 2 ;;
    esac
}

# platform_of_tag TAG: the platform TAG is a release of (macos-v0.9.0 -> macos)
platform_of_tag() {
    case "$1" in
        macos-v[0-9]*) echo macos ;;
        v[0-9]*) echo linux ;;
        *) echo "not a release tag: $1 (vX.Y.Z, macos-vX.Y.Z)" >&2; return 2 ;;
    esac
}

# version_lines PLATFORM: where the platform's version is written besides its VERSION file
# and its changelog's "## [X.Y.Z]" heading: "FILE<TAB>TEXT" lines, @V@ standing for the
# version. Each TEXT ends its line in FILE.
# The programs both platforms share (npu-transcribe, bmeet-ctl, the app, the Chrome
# extension) carry the Linux line's version; on a Mac they report macos/VERSION
# (macos/bmeetmac.py).
version_lines() {
    case "$1" in
        linux)
            printf '%s\t%s\n' \
                install.sh 'local VERSION=@V@' \
                transcribe/npu-transcribe '__version__ = "@V@"' \
                bmeet/bmeet-ctl '__version__ = "@V@"' \
                bmeet/app/bmeet '__version__ = "@V@"' \
                bmeet/dictate/bmeet-dictate '__version__ = "@V@"' \
                bmeet/plasmoid/io.github.baakhoff.bmeet/metadata.json '"Version": "@V@",' \
                bmeet/chrome/extension/manifest.json '"version": "@V@",' ;;
        macos)
            printf '%s\t%s\n' \
                install.sh 'local MACOS_VERSION=@V@' ;;
    esac
}

# platform_version PLATFORM: the version in its VERSION file
platform_version() {
    platform_info "$1" || return
    tr -d ' \n' < "$VERSION_FILE"
}
