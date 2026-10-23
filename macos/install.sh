#!/usr/bin/env bash
# BMeet installer for macOS on Apple silicon (M1 and later; macOS 14.2 or later).
#
# One line (the top-level install.sh hands over to this script on a Mac):
#   curl -fsSL https://raw.githubusercontent.com/baakhoff/bmeet/main/install.sh | bash
# From a checkout: ./install.sh (or bash macos/install.sh).
#
# Options:
#   --languages "en ru"  dictation languages that ⌃⌥⇧H cycles through
#                        (Whisper codes; default: English + your Mac's languages)
#   --no-turbo           Whisper small instead of large-v3-turbo everywhere (~400 MB less)
#   --no-speakers        do not tell meeting speakers apart (no speaker models; the meeting
#                        audio is deleted as soon as the live transcript is done)
#   --no-gigaam          skip GigaAM v3: Russian is transcribed with Whisper
#   --no-start           do not start BMeet.app at the end (and not at login)
#   --skip-system        do not install Homebrew packages (python@3.13, ffmpeg)
#   --version            print the version this installer installs
# Env: BMEET_SIGN_IDENTITY: a code signing identity for BMeet.app (default: ad hoc; see
# docs/macos.md: with an identity of your own, macOS keeps BMeet's permissions over updates).
#
# What goes where:
#   ~/.local/share/bmeet/{src,venv,bin}     program files, Python environment, native helpers
#   ~/.local/share/npu-models               the models (WhisperKit, Core ML builds), ~1.6 GB
#   ~/Applications/BMeet.app                the menu bar app (and the BMeet window inside it)
#   ~/.local/bin                            npu-transcribe, bmeet-ctl, bmeet
#   ~/Library/Logs/BMeet/install.log        everything the steps printed
# Nothing needs sudo (only Homebrew's own installer does, if you let this one start it).
# Undo: bash ~/.local/share/bmeet/src/uninstall.sh

main() {
    set -euo pipefail

    local src
    src="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
    # On the way out: a BMeet.app that step 4 quit is opened again if a later step failed
    # (BMEET_REOPEN), and the release that install.sh downloaded is deleted
    BMEET_REOPEN=""
    local clean=":"
    if [ -n "${BMEET_TMP:-}" ] && [ "$src" = "$(cd "$BMEET_TMP" && pwd)" ]; then
        clean='rm -rf "$BMEET_TMP"'
    fi
    trap '[ $? -eq 0 ] || [ -z "$BMEET_REOPEN" ] || open -g "$BMEET_REOPEN" >/dev/null 2>&1 || true; '"$clean" EXIT
    local VERSION
    VERSION="$(cat "$src/VERSION")"
    local data="${XDG_DATA_HOME:-$HOME/.local/share}"
    local config="${XDG_CONFIG_HOME:-$HOME/.config}/bmeet"
    local ROOT="$data/bmeet"
    local CACHE="${XDG_CACHE_HOME:-$HOME/.cache}/bmeet"
    local MODELS_DIR="${NPU_MODELS_DIR:-$data/npu-models}"
    local APP="$HOME/Applications/BMeet.app"
    local BIN="$HOME/.local/bin"
    local LOG="$HOME/Library/Logs/BMeet/install.log"

    local languages="" turbo=1 speakers=1 gigaam=1 start=1 system=1
    while [ $# -gt 0 ]; do
        case "$1" in
            --languages) languages="${2:?--languages needs a value, e.g. \"en ru\"}"; shift ;;
            --no-turbo) turbo= ;;
            --no-speakers) speakers= ;;
            --no-gigaam) gigaam= ;;
            --no-start) start= ;;
            --skip-system) system= ;;
            --no-panel|--no-npu-driver|--reset-config|--force) ;;   # (Linux options: nothing to do here)
            --version) echo "BMeet installer $VERSION"; return 0 ;;
            -h|--help) sed -n '2,30p' "${BASH_SOURCE[0]}"; return 0 ;;
            *) printf 'unknown option: %s (see --help)\n' "$1" >&2; return 2 ;;
        esac
        shift
    done

    # --- how it looks: numbered steps, a spinner for the long ones, details in the log ----
    mkdir -p "$(dirname "$LOG")"
    printf 'BMeet %s installer, %s\n' "$VERSION" "$(date)" > "$LOG"
    local tty="" B="" D="" G="" R="" Y="" C="" N=""
    if [ -t 1 ]; then
        tty=1 B=$'\033[1m' D=$'\033[2m' G=$'\033[32m' R=$'\033[31m' Y=$'\033[33m' C=$'\033[36m' N=$'\033[0m'
    fi
    local STEP=0 STEPS=9
    step() { STEP=$((STEP + 1)); printf '\n%s%d/%d%s  %s%s%s\n' "$C" "$STEP" "$STEPS" "$N" "$B" "$*" "$N"; }
    ok() { printf '      %s✓%s %s\n' "$G" "$N" "$*"; }
    note() { printf '      %s%s%s\n' "$D" "$*" "$N"; }
    warn() { printf '      %s!%s %s\n' "$Y" "$N" "$*"; }
    die() {
        printf '\n      %s✗ %s%s\n' "$R" "$*" "$N" >&2
        printf '        %sDetails: %s%s\n' "$D" "$LOG" "$N" >&2
        exit 1
    }
    took() { local s=$1; [ "$s" -ge 60 ] && printf '%dm %02ds' $((s / 60)) $((s % 60)) || printf '%ds' "$s"; }
    run() {  # run "what" cmd...: the command's output goes to the log, a spinner shows meanwhile
        local what=$1 t0=$SECONDS
        shift
        printf '\n=== %s\n$ %s\n' "$what" "$*" >> "$LOG"
        if [ -n "$tty" ]; then
            "$@" >> "$LOG" 2>&1 &
            local pid=$! i=0
            # Ctrl-C: a step started like this ignores it (a script has no job control), so it is
            # stopped here, with what it started, before the installer ends
            trap 'pkill -TERM -P "$pid" 2>/dev/null || true; kill -TERM "$pid" 2>/dev/null || true
                wait "$pid" 2>/dev/null || true; printf "\r\033[K\n"; exit 130' INT TERM
            local frames=(⠋ ⠙ ⠹ ⠸ ⠼ ⠴ ⠦ ⠧ ⠇ ⠏)
            while kill -0 "$pid" 2>/dev/null; do
                printf '\r      %s%s%s %s %s%s%s' "$C" "${frames[i % 10]}" "$N" "$what" "$D" "$(took $((SECONDS - t0)))" "$N"
                i=$((i + 1))
                sleep 0.1
            done
            local code=0
            wait "$pid" || code=$?
            trap - INT TERM
            printf '\r\033[K'
            [ "$code" -eq 0 ] || { tail -n 12 "$LOG" | sed 's/^/        /' >&2; return "$code"; }
        else
            "$@" >> "$LOG" 2>&1 || { tail -n 12 "$LOG" | sed 's/^/        /' >&2; return 1; }
        fi
        ok "$what $D($(took $((SECONDS - t0))))$N"
    }

    # Meeting transcripts go to ~/Documents/Meetings. With iCloud Drive's "Desktop & Documents
    # Folders" on, iCloud syncs ~/Documents, and the transcripts with it. Finder keeps that
    # setting (one switch for both folders); without it, a note says what to check. (Not a look
    # into iCloud Drive: macOS would ask whether this terminal may access it.)
    local icloud_docs=""
    case "$(defaults read com.apple.finder FXICloudDriveDocuments 2>/dev/null \
            || defaults read com.apple.finder FXICloudDriveDesktop 2>/dev/null || true)" in
        1) icloud_docs=1 ;;
        0) ;;
        *) icloud_docs=unknown ;;
    esac

    printf '\n  %sBMeet %s for macOS%s\n' "$B" "$VERSION" "$N"
    printf '  %sDictation, meeting transcripts and file transcription on the Neural Engine.%s\n' "$D" "$N"
    if [ "$icloud_docs" = 1 ]; then
        printf '  %sWhat you say is transcribed on this Mac, but iCloud syncs the meeting transcripts (see below).%s\n' "$D" "$N"
    else
        printf '  %sNothing you say leaves this Mac.%s\n' "$D" "$N"
    fi

    # --- 1. this Mac ---------------------------------------------------------------------
    step "Checking this Mac"
    [ "$(uname -s)" = Darwin ] || die "this is the macOS installer"
    [ "$(id -u)" -ne 0 ] || die "run this as your normal user, without sudo"
    local arch os chip
    arch="$(uname -m)"
    os="$(sw_vers -productVersion)"
    chip="$(sysctl -n machdep.cpu.brand_string 2>/dev/null || echo '?')"
    [ "$arch" = arm64 ] || die "BMeet needs a Mac with Apple silicon (M1 or later): the models run on its Neural Engine"
    local major minor
    major="${os%%.*}"; minor="$(echo "$os" | cut -d. -f2)"
    if [ "$major" -lt 14 ] || { [ "$major" -eq 14 ] && [ "${minor:-0}" -lt 2 ]; }; then
        die "BMeet needs macOS 14.2 or later (for recording the system audio); this Mac has $os"
    fi
    ok "$chip, $(( $(sysctl -n hw.memsize) / 1073741824 )) GB, macOS $os"
    local free_gb
    free_gb=$(( $(df -Pk "$HOME" | awk 'NR==2 {print $4}') / 1024 / 1024 ))
    [ "$free_gb" -ge 5 ] || die "only ${free_gb} GB free on this Mac; about 5 GB are needed"
    local run_dir
    run_dir="$(getconf DARWIN_USER_TEMP_DIR)bmeet"
    case "$(head -n1 "$run_dir/meeting_state" 2>/dev/null || echo idle)" in
        recording|paused) die "a meeting is being recorded - stop it first (⌃⌥M), then run this again" ;;
    esac
    if [ "$icloud_docs" = 1 ]; then
        warn "iCloud syncs your Documents folder: the meeting transcripts in ~/Documents/Meetings go to iCloud too"
        note "(System Settings -> your name -> iCloud -> iCloud Drive -> Desktop & Documents Folders)"
    elif [ -n "$icloud_docs" ]; then
        note "if iCloud Drive's Desktop & Documents Folders is on, iCloud syncs the meeting transcripts in ~/Documents/Meetings too"
        note "(System Settings -> your name -> iCloud -> iCloud Drive -> Desktop & Documents Folders)"
    fi
    # A signing identity of your own (BMEET_SIGN_IDENTITY): checked now, before step 4 quits BMeet.app
    local identity="${BMEET_SIGN_IDENTITY:--}"
    if [ "$identity" != - ]; then
        case "$(security find-identity -p codesigning 2>/dev/null | tr 'A-Z' 'a-z' || true)" in
            *"$(printf '%s' "$identity" | tr 'A-Z' 'a-z')"*) ;;
            *) die "no code signing identity \"$identity\" in your keychain (BMEET_SIGN_IDENTITY; security find-identity -p codesigning lists yours)" ;;
        esac
    fi
    # Apple's Command Line Tools (the compilers BMeet's Swift parts need; not Xcode)
    if ! xcode-select -p >/dev/null 2>&1; then
        [ -n "$tty" ] || die "the Command Line Tools are missing: run  xcode-select --install  and then this again"
        warn "Apple's Command Line Tools are needed: macOS shows a window to install them - click Install"
        xcode-select --install >/dev/null 2>&1 || true
        local waited=0
        until xcode-select -p >/dev/null 2>&1; do
            sleep 5
            waited=$((waited + 5))
            [ "$waited" -lt 3600 ] || die "the Command Line Tools did not get installed"
            printf '\r      %swaiting for the Command Line Tools… %s%s' "$D" "$(took "$waited")" "$N"
        done
        printf '\r\033[K'
    fi
    command -v swift >/dev/null || die "swift is missing: reinstall the Command Line Tools (xcode-select --install)"
    local swift_v
    swift_v="$(swift --version 2>&1 | grep -oE 'Swift version [0-9]+\.[0-9]+' | head -n1 | awk '{print $3}')"
    case "$swift_v" in
        ""|5.[0-9]) die "Swift ${swift_v:-?} is too old (5.10 or later): update the Command Line Tools in System Settings -> General -> Software Update" ;;
    esac
    ok "Command Line Tools, Swift $swift_v"
    # Homebrew (for Python and ffmpeg): the one for Apple silicon first, as a Mac moved over from
    # an Intel one may still have the Intel Homebrew in /usr/local, and first in PATH
    local brew=""
    for b in /opt/homebrew/bin/brew "$(command -v brew || true)" /usr/local/bin/brew; do
        [ -n "$b" ] && [ -x "$b" ] && { brew=$b; break; }
    done
    if [ -z "$brew" ]; then
        [ -n "$tty" ] && [ -n "$system" ] || die "Homebrew is needed for Python and ffmpeg: install it from https://brew.sh, then run this again"
        warn "Homebrew (https://brew.sh) is needed for Python and ffmpeg"
        local answer=""
        read -r -p "      Install Homebrew now with its official installer? [Y/n] " answer </dev/tty || true
        case "$answer" in [nN]*) die "install Homebrew from https://brew.sh, then run this again" ;; esac
        /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)" </dev/tty \
            || die "Homebrew's installer did not finish"
        brew=/opt/homebrew/bin/brew
        [ -x "$brew" ] || die "Homebrew is not in /opt/homebrew after its installer"
        note "Homebrew's own analytics are on (BMeet's installs here send none); to turn them off: brew analytics off"
    fi
    ok "Homebrew $("$brew" --version 2>/dev/null | head -n1 | awk '{print $2}')"
    mkdir -p "$CACHE/downloads"

    # --- 2. Homebrew packages ----------------------------------------------------------------
    step "Python and ffmpeg"
    local brew_prefix
    brew_prefix="$("$brew" --prefix)"
    local py="$brew_prefix/opt/python@3.13/bin/python3.13"
    if [ -n "$system" ]; then
        local want=()
        [ -x "$py" ] || want+=(python@3.13)
        # (Homebrew's ffmpeg even when another one is in this shell's PATH: BMeet.app's programs
        # get /usr/bin:/bin and Homebrew's bin folders only, see macos/bmeetmac.py)
        [ -x "$brew_prefix/bin/ffmpeg" ] || want+=(ffmpeg)
        if [ ${#want[@]} -gt 0 ]; then
            local intel=""   # (an Intel Homebrew refuses to install on Apple silicon)
            [ "$brew_prefix" = /opt/homebrew ] \
                || intel=" (is the Homebrew in $brew_prefix the one for Intel Macs? BMeet needs Homebrew for Apple silicon, in /opt/homebrew)"
            run "brew install ${want[*]}" env HOMEBREW_NO_AUTO_UPDATE=1 HOMEBREW_NO_ENV_HINTS=1 HOMEBREW_NO_ANALYTICS=1 \
                "$brew" install "${want[@]}" \
                || die "Homebrew could not install ${want[*]}$intel"
        fi
    fi
    [ -x "$py" ] || die "python@3.13 is missing (brew install python@3.13)"
    [ "$("$py" -c 'import platform; print(platform.machine())' 2>/dev/null)" = arm64 ] \
        || die "$py is not for Apple silicon: install Homebrew for Apple silicon, in /opt/homebrew (https://brew.sh), then run this again"
    local brew_bin="$brew_prefix/bin"
    [ -x "$brew_bin/ffmpeg" ] || die "Homebrew's ffmpeg is missing (brew install ffmpeg; BMeet.app does not see one elsewhere in your PATH)"
    ok "Python $("$py" -c 'import platform; print(platform.python_version())'), ffmpeg $("$brew_bin/ffmpeg" -version 2>/dev/null | head -n1 | awk '{print $3}')"

    # --- 3. program files and the Python environment ---------------------------------------
    step "Program files and Python packages"
    mkdir -p "$ROOT/bin" "$HOME/.local/share/npu-transcribe"
    # Only you: other accounts on this Mac may look into your home folder (unlike its Documents or
    # Library), and these hold meeting titles, voices and the transcription history
    chmod 700 "$ROOT" "$HOME/.local/share/npu-transcribe"
    local stage="$ROOT/.src.new"
    rm -rf "$stage"
    mkdir -p "$stage"
    tar -C "$src" --exclude='tests' --exclude='__pycache__' --exclude='.work' --exclude='.build' \
        --exclude='macos/icons' -cf - VERSION LICENSE install.sh uninstall.sh voxtype transcribe bmeet macos \
        | tar -C "$stage" -xf -
    rm -rf "$ROOT/src.old"
    [ ! -d "$ROOT/src" ] || mv "$ROOT/src" "$ROOT/src.old"
    mv "$stage" "$ROOT/src"
    rm -rf "$ROOT/src.old"
    local build_src="$src"      # (Swift builds from the source tree; the build itself goes to the cache)
    src="$ROOT/src"
    ok "BMeet $VERSION in ${ROOT/#$HOME/~}"
    local venv="$ROOT/venv"
    if ! "$venv/bin/python" -c 'import platform, sys; sys.exit(sys.version_info[:2] != (3, 13) or platform.machine() != "arm64")' \
        2>/dev/null; then
        rm -rf "$venv"
        "$py" -m venv "$venv" >> "$LOG" 2>&1 || die "could not make a Python environment"
    fi
    run "numpy, Core ML tools, ONNX Runtime, PyQt6" sh -c "'$venv/bin/python' -m pip install --disable-pip-version-check --upgrade pip \
        && '$venv/bin/python' -m pip install --disable-pip-version-check -r '$src/macos/requirements.txt'" \
        || die "the Python packages did not install"

    # --- 4. the native parts -------------------------------------------------------------------
    step "BMeet.app"
    local swift_build="$CACHE/swift-build"
    run "Building BMeet.app, the Whisper server and the recorder (Swift)" \
        swift build -c release --package-path "$build_src/macos" --scratch-path "$swift_build" \
        || die "the Swift build failed"
    local out="$swift_build/release"
    # What macOS compiled for the Neural Engine (Whisper turbo: ~2 minutes on an M1) belongs to
    # the very bmeet-whisper file that loaded it: a new file - even the same code built again, or
    # the old one signed again - compiles it anew. So bmeet-whisper is only replaced (and signed)
    # when its code changes: its source, WhisperKit's pinned version, Swift. Then the old
    # compilation is let go (the warm-up below makes the new one).
    local whisper_stamp
    whisper_stamp="$(cat "$build_src/macos/Sources/bmeet-whisper/main.swift" "$build_src/macos/Package.swift" \
        "$build_src/macos/Package.resolved" | shasum -a 256 | cut -c1-16) swift $swift_v"
    local new_whisper=""
    if [ ! -x "$ROOT/bin/bmeet-whisper" ] || [ "$(cat "$ROOT/bin/.bmeet-whisper.stamp" 2>/dev/null)" != "$whisper_stamp" ]; then
        new_whisper=1
        rm -rf "$HOME/Library/Caches/bmeet-whisper/com.apple.e5rt.e5bundlecache"
        install -m 0755 "$out/bmeet-whisper" "$ROOT/bin/bmeet-whisper.new"
        mv -f "$ROOT/bin/bmeet-whisper.new" "$ROOT/bin/bmeet-whisper"
        echo "$whisper_stamp" > "$ROOT/bin/.bmeet-whisper.stamp"
    fi
    rm -f "$ROOT/bin/bmeet-whisper.built"   # (how 0.9.0's first builds decided it)
    install -m 0755 "$out/bmeet-record" "$ROOT/bin/bmeet-record.new"
    mv -f "$ROOT/bin/bmeet-record.new" "$ROOT/bin/bmeet-record"
    # An update: quit the running app first (SIGTERM: it quits as from its menu; an AppleScript
    # "quit" would have macOS ask whether this terminal may control BMeet).
    if pgrep -qf "$APP/Contents/MacOS/BMeet"; then
        pkill -TERM -f "$APP/Contents/MacOS/BMeet" 2>/dev/null || true
        for _ in 1 2 3 4 5 6 7 8 9 10; do pgrep -qf "$APP/Contents/MacOS/BMeet" || break; sleep 0.3; done
        pkill -KILL -f "$APP/Contents/MacOS/BMeet" 2>/dev/null || true
        note "quit the running BMeet.app (it starts again at the end)"
        [ -z "$start" ] || BMEET_REOPEN="$APP"   # (or on the way out, should a later step fail)
    fi
    [ -z "$new_whisper" ] || pkill -f "$ROOT/bin/bmeet-whisper serve" 2>/dev/null || true   # (the next request starts the new one)
    local stage_app="$CACHE/BMeet.app"
    local window="$stage_app/Contents/Helpers/BMeet Window.app"
    rm -rf "$stage_app"
    mkdir -p "$stage_app/Contents/MacOS" "$stage_app/Contents/Resources" "$window/Contents/MacOS" "$window/Contents/Resources"
    sed "s/@VERSION@/$VERSION/g" "$src/macos/app/Info.plist" > "$stage_app/Contents/Info.plist"
    sed "s/@VERSION@/$VERSION/g" "$src/macos/app/Window-Info.plist" > "$window/Contents/Info.plist"
    install -m 0755 "$out/BMeet" "$stage_app/Contents/MacOS/BMeet"
    local cfg="$py-config"
    [ -x "$cfg" ] || cfg="$(dirname "$py")/python3.13-config"
    # shellcheck disable=SC2046
    clang -O2 -Wall "$src/macos/window/launcher.c" $("$cfg" --embed --cflags) $("$cfg" --embed --ldflags) \
        -o "$window/Contents/MacOS/BMeet" >> "$LOG" 2>&1 || die "building the BMeet window failed"
    # the app icon, and the window's icons (SF Symbols, drawn on this Mac)
    local icon="$CACHE/BMeet.icns"
    if [ ! -s "$icon" ] || [ "$src/macos/tools/make-icon.swift" -nt "$icon" ]; then
        { swiftc -O "$src/macos/tools/make-icon.swift" -o "$CACHE/make-icon" && "$CACHE/make-icon" "$icon"; } >> "$LOG" 2>&1 \
            || warn "no app icon (drawing it failed)"
    fi
    if [ -s "$icon" ]; then
        cp "$icon" "$stage_app/Contents/Resources/BMeet.icns"
        cp "$icon" "$window/Contents/Resources/BMeet.icns"
    fi
    { swiftc -O "$src/macos/tools/make-symbols.swift" -o "$CACHE/make-symbols" && "$CACHE/make-symbols" "$src/macos/icons"; } \
        >> "$LOG" 2>&1 || warn "the window keeps Qt's own icons (drawing the SF Symbols failed)"
    printf 'APPL????' > "$stage_app/Contents/PkgInfo"
    codesign --force --sign "$identity" "$window" >> "$LOG" 2>&1 || die "signing the BMeet window failed"
    codesign --force --sign "$identity" --identifier io.github.baakhoff.bmeet "$stage_app" >> "$LOG" 2>&1 \
        || die "signing BMeet.app failed"
    codesign --force --sign "$identity" "$ROOT/bin/bmeet-record" >> "$LOG" 2>&1 || true
    [ -z "$new_whisper" ] || codesign --force --sign "$identity" "$ROOT/bin/bmeet-whisper" >> "$LOG" 2>&1 || true
    mkdir -p "$HOME/Applications"
    rm -rf "$APP.old"
    [ ! -d "$APP" ] || mv "$APP" "$APP.old"
    mv "$stage_app" "$APP"
    rm -rf "$APP.old"
    /System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister \
        -f "$APP" >/dev/null 2>&1 || true
    ok "${APP/#$HOME/~}"

    # --- 5. Whisper --------------------------------------------------------------------------
    step "Whisper for the Neural Engine"
    local variants=(openai_whisper-small_216MB)
    [ -z "$turbo" ] || variants+=(openai_whisper-large-v3-v20240930_626MB)
    note "WhisperKit's Core ML models from huggingface.co/argmaxinc, checked against pinned hashes"
    "$venv/bin/python" -I "$src/macos/fetch-models" "$src/macos/models/whisperkit.tsv" "$MODELS_DIR/whisperkit" \
        "${variants[@]}" | sed -u 's/^/      /' || die "downloading Whisper failed - run this again to go on where it stopped"
    ok "${variants[*]}"

    # --- 6. GigaAM and the speaker models --------------------------------------------------------
    step "Russian speech and speaker models for the Neural Engine"
    local dl="$CACHE/downloads" rel=https://github.com/k2-fsa/sherpa-onnx/releases/download
    fetch() {  # file url sha256: download into the cache once, check it every time
        if ! echo "$3  $dl/$1" | shasum -a 256 -c --status >/dev/null 2>&1; then
            printf '      %s%s%s\n' "$D" "$1" "$N"
            # (-C -: a download cut off goes on from its .part, now or the next time)
            curl -fL --retry 3 -C - --progress-bar -o "$dl/$1.part" "$2" \
                || die "downloading $1 failed - run the installer again to go on where it stopped"
            mv "$dl/$1.part" "$dl/$1"
            echo "$3  $dl/$1" | shasum -a 256 -c --status \
                || { rm -f "$dl/$1"; die "checksum mismatch for $1 - run the installer again"; }
        fi
    }
    # (prepare-coreml's progress bars and warnings go to the log)
    local prep=(env TQDM_DISABLE=1 PYTHONWARNINGS=ignore "$venv/bin/python" -I "$src/macos/prepare-coreml")
    local prep_hash
    prep_hash="$(cat "$src/macos/prepare-coreml" "$src/transcribe/prepare-models" "$src/transcribe/prepare-diarization" \
        | shasum -a 256 | cut -c1-16)"

    # GigaAM v3 (Russian): the same pinned file as on Linux, built for the Neural Engine once
    local giga_dir="$MODELS_DIR/gigaam-v3-ctc-ru"
    local giga_name=sherpa-onnx-nemo-ctc-punct-giga-am-v3-russian-2025-12-16
    local giga_tar_sha=20e41e160efa6f2460a7ef6554cdbbb9e8fffb2b92e6bc708e9406bd8b9256ea
    local giga_onnx_sha=d5fea8df94263c285e54b21e5774b707c707192d3bdbeffd7b1eb07fb6743b35
    if [ -n "$gigaam" ]; then
        local giga_stamp="coreml $giga_onnx_sha $prep_hash"
        if [ -d "$giga_dir/model.mlmodelc" ] && [ "$(cat "$giga_dir/.prepared" 2>/dev/null)" = "$giga_stamp" ]; then
            ok "GigaAM v3 (Russian)"
        else
            fetch "$giga_name.tar.bz2" "$rel/asr-models/$giga_name.tar.bz2" "$giga_tar_sha"
            local giga_tmp
            giga_tmp="$(mktemp -d "$CACHE/gigaam.XXXXXX")"
            tar -xjf "$dl/$giga_name.tar.bz2" -C "$giga_tmp" "$giga_name/model.int8.onnx" "$giga_name/tokens.txt" \
                "$giga_name/test_wavs"
            echo "$giga_onnx_sha  $giga_tmp/$giga_name/model.int8.onnx" | shasum -a 256 -c --status \
                || { rm -rf "$giga_tmp"; die "unexpected model in $giga_name.tar.bz2"; }
            mkdir -p "$giga_dir"
            if ! run "Building GigaAM v3 (Russian) for the Neural Engine" "${prep[@]}" gigaam "$giga_tmp/$giga_name" "$giga_dir"; then
                rm -rf "$giga_tmp"
                die "building GigaAM for the Neural Engine failed (--no-gigaam installs without it)"
            fi
            rm -rf "$giga_tmp"
            echo "$giga_stamp" > "$giga_dir/.prepared"
        fi
    else
        note "skipped (--no-gigaam): Russian is transcribed with Whisper"
    fi

    # Speaker models: Nemotron 3 Diarization and TitaNet-small (as on Linux), for the Neural Engine
    mkdir -p "$config"
    chmod 700 "$config"
    if [ -n "$speakers" ]; then
        local sd="$MODELS_DIR/speaker-diarization"
        mkdir -p "$sd"
        local emb=nemo_en_titanet_small.onnx
        local emb_sha=ad4a1802485d8b34c722d2a9d04249662f2ece5d28a7a039063ca22f515a789e
        local emb_stamp="coreml $emb_sha $prep_hash"
        if [ ! -d "$sd/titanet.mlmodelc" ] || [ "$(cat "$sd/.titanet-prepared" 2>/dev/null)" != "$emb_stamp" ]; then
            fetch "$emb" "$rel/speaker-recongition-models/$emb" "$emb_sha"   # (sic: upstream's tag)
            run "Building TitaNet (voice fingerprints) for the Neural Engine" "${prep[@]}" titanet "$dl/$emb" "$sd" \
                || die "building TitaNet for the Neural Engine failed (--no-speakers installs without it)"
            echo "$emb_stamp" > "$sd/.titanet-prepared"
        else
            ok "TitaNet (voice fingerprints)"
        fi
        local nem_dir="$sd/nemotron-3-diarization" nem_file=nemotron-3-diarization.safetensors
        local nem_rev=f667ed73aee57d40cc39428eb768b4fd87a0a29e
        local nem_sha=c074d86335b3b794f8fa5edc25594558f128bdb3914d27806a3a5a2e44963cb6
        local nem_stamp="coreml $nem_sha $prep_hash"
        if [ ! -d "$nem_dir/encoder.mlmodelc" ] || [ "$(cat "$nem_dir/.prepared" 2>/dev/null)" != "$nem_stamp" ]; then
            fetch "$nem_file" "https://huggingface.co/nvidia/Nemotron-3-Diarization/resolve/$nem_rev/model.safetensors" \
                "$nem_sha"
            run "Building Nemotron 3 Diarization (who speaks when) for the Neural Engine" \
                "${prep[@]}" nemotron "$dl/$nem_file" "$nem_dir" \
                || die "building Nemotron 3 Diarization for the Neural Engine failed (--no-speakers installs without it)"
            echo "$nem_stamp" > "$nem_dir/.prepared"
        else
            ok "Nemotron 3 Diarization (who speaks when)"
        fi
        [ -s "$config/speakers" ] || echo on > "$config/speakers"   # keeps an "off" you set
    else
        echo off > "$config/speakers"
        note "skipped (--no-speakers): meetings keep You / Remote"
    fi

    # --- 7. settings, commands, the Chrome extension's helper ---------------------------------
    step "Settings and commands"
    if [ -z "$languages" ] && [ ! -s "$config/languages" ]; then
        # English + the languages of your Mac (System Settings -> General -> Language & Region)
        languages="$( { echo en; defaults read -g AppleLanguages 2>/dev/null | tr -d ' ",()' | cut -c1-2; } \
            | tr 'A-Z' 'a-z' | grep -E '^[a-z]{2}$' | awk '!seen[$0]++' | head -n 3 | tr '\n' ' ' | sed 's/ $//')"
    fi
    [ -z "$languages" ] || echo "$languages" > "$config/languages"
    [ -s "$config/language" ] || awk '{print $1; exit}' "$config/languages" > "$config/language"
    [ -s "$config/live-dictation" ] || echo on > "$config/live-dictation"
    if [ -n "$start" ]; then
        [ -s "$config/start-at-login" ] || echo on > "$config/start-at-login"
    else
        echo off > "$config/start-at-login"
    fi
    ok "dictation languages: $(cat "$config/languages") (settings in ${config/#$HOME/~})"
    mkdir -p "$BIN"
    wrapper() {  # name script: a command in ~/.local/bin that runs `script` with BMeet's Python
        local tmp
        tmp="$(mktemp "$BIN/.$1.XXXXXX")"
        cat > "$tmp" <<EOF
#!/bin/sh
# BMeet ($VERSION): $1 with BMeet's Python environment (macos/install.sh)
PATH="$brew_bin:\$PATH" exec "$venv/bin/python" -I "$2" "\$@"
EOF
        chmod 755 "$tmp"
        mv -f "$tmp" "$BIN/$1"
    }
    wrapper npu-transcribe "$src/transcribe/npu-transcribe"
    wrapper bmeet-ctl "$src/bmeet/bmeet-ctl"
    wrapper bmeet-chrome-host "$src/bmeet/chrome/bmeet-chrome-host"
    cat > "$BIN/bmeet" <<EOF
#!/bin/sh
# BMeet ($VERSION): open the BMeet window (macos/install.sh)
exec "$BIN/bmeet-ctl" open "\$@"
EOF
    chmod 755 "$BIN/bmeet"
    ok "npu-transcribe, bmeet-ctl and bmeet in ~/.local/bin"
    local path_note=""
    case ":$PATH:" in *":$BIN:"*) ;; *) path_note=1 ;; esac
    local chrome
    chrome="$(bash "$src/bmeet/chrome/install-user.sh" 2>&1)" || true
    printf '%s\n' "$chrome" >> "$LOG"
    case "$chrome" in
        *"native host for:"*) ok "the Chrome extension's helper ($(printf '%s' "$chrome" | sed -n 's/.*native host for: //p' | head -n1))" ;;
        *) note "no Chromium-based browser here: the Chrome extension's helper is skipped" ;;
    esac

    # --- 8. warm up: macOS compiles each model for the Neural Engine the first time ----------------
    step "Preparing the Neural Engine"
    [ -z "$new_whisper" ] || note "macOS compiles each model for this Mac's Neural Engine once (Whisper: ~2 minutes)"
    run "Compiling the models" env PATH="$brew_bin:$PATH" "$venv/bin/python" -I "$src/macos/warm-up" \
        || warn "not now: they are prepared the first time they are used"

    # --- 9. start ---------------------------------------------------------------------------------
    step "Starting BMeet"
    /System/Library/CoreServices/pbs -update >/dev/null 2>&1 || true   # (Finder's Services menu)
    if [ -n "$start" ]; then
        open "$APP" && ok "BMeet is in your menu bar (the microphone)"
    else
        note "not started (--no-start): open ${APP/#$HOME/~} when you want it"
    fi

    local L
    L="$(cat "$config/languages")"
    cat <<EOF

  ${G}${B}✓ BMeet $VERSION is ready.${N}

  ${B}The first time, macOS asks you to allow BMeet:${N}
    Microphone              dictation and meetings
    System Audio Recording  the other side of a call
    Accessibility           pasting what you dictate where you type
    Documents folder        saving the meeting transcripts in ~/Documents/Meetings

  ${B}Shortcuts${N}
    ⌃⌥H    dictate: press, speak, press again        ⌃⌥⌘H   dictate to the clipboard
    ⌃⌥M    meeting: microphone + system audio        ⌃⌥⇧M   in-person meeting
    ⌃⌥⇧H   next dictation language (${L})
    ${D}Finder: right-click a recording -> Quick Actions -> Transcribe with BMeet${N}

  ${B}Chrome${N} ${D}(leaves out what you say while muted in Meet and Slack): load it once at${N}
    chrome://extensions -> Developer mode -> Load unpacked -> ${src/#$HOME/~}/bmeet/chrome/extension

  ${D}Check: bash ${src/#$HOME/~}/macos/verify.sh   Uninstall: bash ${src/#$HOME/~}/uninstall.sh
  Log:   ${LOG/#$HOME/~}${N}
EOF
    if [ "$icloud_docs" = 1 ]; then
        printf '  %s!%s iCloud syncs your Documents folder, and with it the meeting transcripts in ~/Documents/Meetings.\n' "$Y" "$N"
    fi
    if [ -n "$path_note" ]; then
        printf '  %sTo use npu-transcribe and bmeet in Terminal, add ~/.local/bin to your PATH:%s\n' "$D" "$N"
        printf '    echo '"'"'export PATH="$HOME/.local/bin:$PATH"'"'"' >> ~/.zshrc\n'
    fi
    echo
}

main "$@"
