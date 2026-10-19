#!/usr/bin/env bash
# BMeet installer for macOS on Apple silicon (M1 and later; macOS 14.2 or later).
#
# The top-level install.sh hands over to this script on a Mac, so the same one line installs
# it (curl -fsSL https://raw.githubusercontent.com/baakhoff/bmeet/main/install.sh | bash).
# From a checkout: ./install.sh (or bash macos/install.sh).
#
# Options:
#   --languages "en ru"  dictation languages that ⌃⌥⇧H cycles through
#                        (Whisper codes; default: English + your Mac's language)
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
# Nothing needs sudo. Undo: bash ~/.local/share/bmeet/src/uninstall.sh

main() {
    set -euo pipefail

    local src
    src="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
    if [ -n "${BMEET_TMP:-}" ] && [ "$src" = "$(cd "$BMEET_TMP" && pwd)" ]; then
        trap 'rm -rf "$BMEET_TMP"' EXIT   # the release that install.sh downloaded
    fi
    local VERSION
    VERSION="$(cat "$src/VERSION")"
    local data="${XDG_DATA_HOME:-$HOME/.local/share}"
    local config="${XDG_CONFIG_HOME:-$HOME/.config}/bmeet"
    local ROOT="$data/bmeet"
    local CACHE="${XDG_CACHE_HOME:-$HOME/.cache}/bmeet"
    local MODELS_DIR="${NPU_MODELS_DIR:-$data/npu-models}"
    local APP="$HOME/Applications/BMeet.app"
    local BIN="$HOME/.local/bin"

    if [ -t 1 ]; then
        say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
    else
        say() { printf '\n==> %s\n' "$*"; }
    fi
    die() { printf 'error: %s\n' "$*" >&2; exit 1; }

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
            -h|--help) sed -n '2,28p' "${BASH_SOURCE[0]}"; return 0 ;;
            *) die "unknown option: $1 (see --help)" ;;
        esac
        shift
    done

    # --- preflight -------------------------------------------------------------------
    say "BMeet $VERSION for macOS"
    [ "$(uname -s)" = Darwin ] || die "this is the macOS installer"
    [ "$(id -u)" -ne 0 ] || die "run this as your normal user"
    local arch os
    arch="$(uname -m)"
    os="$(sw_vers -productVersion)"
    echo "  system:  macOS $os, $arch, $(sysctl -n machdep.cpu.brand_string 2>/dev/null || echo '?'), $(( $(sysctl -n hw.memsize) / 1073741824 )) GB"
    [ "$arch" = arm64 ] || die "BMeet needs a Mac with Apple silicon (M1 or later): the models run on its Neural Engine"
    local major minor
    major="${os%%.*}"; minor="$(echo "$os" | cut -d. -f2)"
    if [ "$major" -lt 14 ] || { [ "$major" -eq 14 ] && [ "${minor:-0}" -lt 2 ]; }; then
        die "BMeet needs macOS 14.2 or later (for recording the system audio); this is $os"
    fi
    xcode-select -p >/dev/null 2>&1 \
        || die "the Command Line Tools are missing: run  xcode-select --install  and then this installer again"
    command -v swift >/dev/null || die "swift is missing (Command Line Tools): xcode-select --install"
    local swift_v
    swift_v="$(swift --version 2>&1 | grep -oE 'Swift version [0-9]+\.[0-9]+' | head -n1 | awk '{print $3}')"
    case "$swift_v" in
        ""|5.[0-9]) die "Swift ${swift_v:-?} is too old (5.10 or later is needed): update the Command Line Tools (System Settings -> General -> Software Update, or xcode-select --install)" ;;
    esac
    local brew
    brew="$(command -v brew || echo /opt/homebrew/bin/brew)"
    [ -x "$brew" ] || die "Homebrew is needed for Python and ffmpeg: see https://brew.sh, then run this again"
    local free_gb
    free_gb=$(( $(df -Pk "$HOME" | awk 'NR==2 {print $4}') / 1024 / 1024 ))
    [ "$free_gb" -ge 5 ] || die "only ${free_gb} GB free in $HOME; about 5 GB are needed"
    local run_dir
    run_dir="$(getconf DARWIN_USER_TEMP_DIR)bmeet"
    case "$(head -n1 "$run_dir/meeting_state" 2>/dev/null || echo idle)" in
        recording|paused) die "a meeting is being recorded - stop it first (⌃⌥M), then run this again" ;;
    esac
    mkdir -p "$CACHE/downloads" "$CACHE/logs"

    # --- 1. Homebrew packages ------------------------------------------------------------
    local py=/opt/homebrew/opt/python@3.13/bin/python3.13
    [ -x "$py" ] || py="$("$brew" --prefix)/opt/python@3.13/bin/python3.13"
    if [ -n "$system" ]; then
        say "Homebrew packages: python@3.13, ffmpeg"
        local want=()
        [ -x "$py" ] || want+=(python@3.13)
        command -v ffmpeg >/dev/null || [ -x "$("$brew" --prefix)/bin/ffmpeg" ] || want+=(ffmpeg)
        if [ ${#want[@]} -gt 0 ]; then
            HOMEBREW_NO_AUTO_UPDATE=1 HOMEBREW_NO_ENV_HINTS=1 "$brew" install "${want[@]}"
        else
            echo "  present"
        fi
    fi
    [ -x "$py" ] || die "python@3.13 is missing (brew install python@3.13)"
    local brew_bin
    brew_bin="$("$brew" --prefix)/bin"
    [ -x "$brew_bin/ffmpeg" ] || command -v ffmpeg >/dev/null || die "ffmpeg is missing (brew install ffmpeg)"

    # --- 2. program files ------------------------------------------------------------
    say "Program files -> $ROOT/src"
    mkdir -p "$ROOT/bin"
    local stage="$ROOT/.src.new"
    rm -rf "$stage"
    mkdir -p "$stage"
    tar -C "$src" --exclude='tests' --exclude='__pycache__' --exclude='.work' --exclude='.build' \
        -cf - VERSION LICENSE install.sh uninstall.sh voxtype transcribe bmeet macos \
        | tar -C "$stage" -xf -
    rm -rf "$ROOT/src.old"
    [ ! -d "$ROOT/src" ] || mv "$ROOT/src" "$ROOT/src.old"
    mv "$stage" "$ROOT/src"
    rm -rf "$ROOT/src.old"
    local build_src="$src"      # (Swift builds from the source tree; its .build stays a cache)
    src="$ROOT/src"
    echo "  $VERSION installed"

    # --- 3. Python environment --------------------------------------------------------
    local venv="$ROOT/venv"
    say "Python environment -> $venv"
    if ! "$venv/bin/python" -c 'import sys; sys.exit(sys.version_info[:2] != (3, 13))' 2>/dev/null; then
        rm -rf "$venv"
        "$py" -m venv "$venv"
    fi
    echo "  numpy, Core ML tools, ONNX (Runtime), PyQt6 (the first run downloads ~125 MB)"
    "$venv/bin/python" -m pip install --quiet --disable-pip-version-check --upgrade pip
    "$venv/bin/python" -m pip install --quiet --disable-pip-version-check -r "$src/macos/requirements.txt"
    "$venv/bin/python" -I -c 'import numpy, coremltools, onnxruntime, PyQt6.QtWidgets
print("  coremltools", coremltools.__version__, "- numpy", numpy.__version__)' 2>/dev/null

    # --- 4. the native parts: Whisper server, recorder, BMeet.app ---------------------------
    say "BMeet.app and its helpers (Swift; the first build takes a few minutes)"
    local swift_build="$CACHE/swift-build"
    swift build -c release --package-path "$build_src/macos" --scratch-path "$swift_build" \
        --product BMeet 2>&1 | tail -n 1
    swift build -c release --package-path "$build_src/macos" --scratch-path "$swift_build" \
        --product bmeet-whisper 2>&1 | tail -n 1
    swift build -c release --package-path "$build_src/macos" --scratch-path "$swift_build" \
        --product bmeet-record 2>&1 | tail -n 1
    local out="$swift_build/release"
    # macOS keeps what it compiled for the Neural Engine per program (~1 GB for Whisper turbo), and
    # a new bmeet-whisper gets a new copy: let the old one go (the warm-up below makes the new one).
    if [ -x "$ROOT/bin/bmeet-whisper" ] && ! cmp -s "$out/bmeet-whisper" "$ROOT/bin/bmeet-whisper.built" 2>/dev/null; then
        rm -rf "$HOME/Library/Caches/bmeet-whisper/com.apple.e5rt.e5bundlecache"
    fi
    cp "$out/bmeet-whisper" "$ROOT/bin/bmeet-whisper.built"
    for f in bmeet-whisper bmeet-record; do
        install -m 0755 "$out/$f" "$ROOT/bin/$f.new"
        mv -f "$ROOT/bin/$f.new" "$ROOT/bin/$f"
    done

    # The app: quit the running one first (an update), then put the new bundle in place.
    if pgrep -qf "$APP/Contents/MacOS/BMeet"; then
        echo "  quitting the running BMeet.app"
        # (SIGTERM: it quits as from its menu; an AppleScript "quit" would have macOS ask whether
        # this terminal may control BMeet)
        pkill -TERM -f "$APP/Contents/MacOS/BMeet" 2>/dev/null || true
        for _ in 1 2 3 4 5 6 7 8 9 10; do pgrep -qf "$APP/Contents/MacOS/BMeet" || break; sleep 0.3; done
        pkill -KILL -f "$APP/Contents/MacOS/BMeet" 2>/dev/null || true
    fi
    pkill -f "$ROOT/bin/bmeet-whisper serve" 2>/dev/null || true   # (the next request starts the new one)
    local stage_app="$CACHE/BMeet.app"
    rm -rf "$stage_app"
    mkdir -p "$stage_app/Contents/MacOS" "$stage_app/Contents/Resources" \
        "$stage_app/Contents/Helpers/BMeet Window.app/Contents/MacOS" \
        "$stage_app/Contents/Helpers/BMeet Window.app/Contents/Resources"
    sed "s/@VERSION@/$VERSION/g" "$src/macos/app/Info.plist" > "$stage_app/Contents/Info.plist"
    sed "s/@VERSION@/$VERSION/g" "$src/macos/app/Window-Info.plist" \
        > "$stage_app/Contents/Helpers/BMeet Window.app/Contents/Info.plist"
    install -m 0755 "$out/BMeet" "$stage_app/Contents/MacOS/BMeet"
    local cfg="$py"-config
    [ -x "$cfg" ] || cfg="$(dirname "$py")/python3.13-config"
    # shellcheck disable=SC2046
    clang -O2 -Wall "$src/macos/window/launcher.c" $("$cfg" --embed --cflags) $("$cfg" --embed --ldflags) \
        -o "$stage_app/Contents/Helpers/BMeet Window.app/Contents/MacOS/BMeet" 2>&1 \
        | grep -v -E 'unused-command-line-argument|^$' || true
    [ -x "$stage_app/Contents/Helpers/BMeet Window.app/Contents/MacOS/BMeet" ] || die "building the BMeet window failed"
    local icon="$CACHE/BMeet.icns"
    if [ ! -s "$icon" ] || [ "$src/macos/tools/make-icon.swift" -nt "$icon" ]; then
        swiftc -O "$src/macos/tools/make-icon.swift" -o "$CACHE/make-icon" 2>/dev/null && "$CACHE/make-icon" "$icon" \
            || echo "  (no icon: drawing it failed)"
    fi
    if [ -s "$icon" ]; then
        cp "$icon" "$stage_app/Contents/Resources/BMeet.icns"
        cp "$icon" "$stage_app/Contents/Helpers/BMeet Window.app/Contents/Resources/BMeet.icns"
    fi
    printf 'APPL????' > "$stage_app/Contents/PkgInfo"
    local identity="${BMEET_SIGN_IDENTITY:--}"
    codesign --force --sign "$identity" "$stage_app/Contents/Helpers/BMeet Window.app" >/dev/null 2>&1
    codesign --force --sign "$identity" --identifier io.github.baakhoff.bmeet "$stage_app" >/dev/null 2>&1 \
        || die "signing BMeet.app failed"
    codesign --force --sign "$identity" "$ROOT/bin/bmeet-whisper" "$ROOT/bin/bmeet-record" >/dev/null 2>&1 || true
    mkdir -p "$HOME/Applications"
    rm -rf "$APP.old"
    [ ! -d "$APP" ] || mv "$APP" "$APP.old"
    mv "$stage_app" "$APP"
    rm -rf "$APP.old"
    /System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister \
        -f "$APP" >/dev/null 2>&1 || true
    echo "  $APP"

    # --- 5. models -------------------------------------------------------------------
    say "Whisper for the Neural Engine (WhisperKit) -> $MODELS_DIR/whisperkit"
    local variants=(openai_whisper-small_216MB)
    [ -z "$turbo" ] || variants+=(openai_whisper-large-v3-v20240930_626MB)
    "$venv/bin/python" -I "$src/macos/fetch-models" "$src/macos/models/whisperkit.tsv" "$MODELS_DIR/whisperkit" \
        "${variants[@]}"

    local dl="$CACHE/downloads" rel=https://github.com/k2-fsa/sherpa-onnx/releases/download
    fetch() {  # file url sha256: download into the cache once, check it every time
        if ! echo "$3  $dl/$1" | shasum -a 256 -c --status >/dev/null 2>&1; then
            curl -fL --retry 3 --progress-bar -o "$dl/$1.part" "$2" && mv "$dl/$1.part" "$dl/$1"
            echo "$3  $dl/$1" | shasum -a 256 -c --status \
                || { rm -f "$dl/$1"; die "checksum mismatch for $1 - run the installer again"; }
        fi
    }
    local prep="$venv/bin/python -I $src/macos/prepare-coreml"
    local prep_hash
    prep_hash="$(cat "$src/macos/prepare-coreml" "$src/transcribe/prepare-models" "$src/transcribe/prepare-diarization" \
        | shasum -a 256 | cut -c1-16)"

    # GigaAM v3 (Russian): the same pinned file as on Linux, built for the Neural Engine once
    local giga_dir="$MODELS_DIR/gigaam-v3-ctc-ru"
    local giga_name=sherpa-onnx-nemo-ctc-punct-giga-am-v3-russian-2025-12-16
    local giga_tar_sha=20e41e160efa6f2460a7ef6554cdbbb9e8fffb2b92e6bc708e9406bd8b9256ea
    local giga_onnx_sha=d5fea8df94263c285e54b21e5774b707c707192d3bdbeffd7b1eb07fb6743b35
    if [ -n "$gigaam" ]; then
        say "Russian speech model (GigaAM v3) for the Neural Engine -> $giga_dir"
        local giga_stamp="coreml $giga_onnx_sha $prep_hash"
        if [ -d "$giga_dir/model.mlmodelc" ] && [ "$(cat "$giga_dir/.prepared" 2>/dev/null)" = "$giga_stamp" ]; then
            echo "  present"
        else
            fetch "$giga_name.tar.bz2" "$rel/asr-models/$giga_name.tar.bz2" "$giga_tar_sha"
            local giga_tmp
            giga_tmp="$(mktemp -d "$CACHE/gigaam.XXXXXX")"
            mkdir -p "$giga_tmp/src/test_wavs"
            tar -xjf "$dl/$giga_name.tar.bz2" -C "$giga_tmp" "$giga_name/model.int8.onnx" "$giga_name/tokens.txt" \
                "$giga_name/test_wavs"
            echo "$giga_onnx_sha  $giga_tmp/$giga_name/model.int8.onnx" | shasum -a 256 -c --status \
                || { rm -rf "$giga_tmp"; die "unexpected model in $giga_name.tar.bz2"; }
            mkdir -p "$giga_dir"
            if ! $prep gigaam "$giga_tmp/$giga_name" "$giga_dir"; then
                rm -rf "$giga_tmp"
                die "building GigaAM for the Neural Engine failed (--no-gigaam installs without it)"
            fi
            rm -rf "$giga_tmp"
            echo "$giga_stamp" > "$giga_dir/.prepared"
        fi
    fi

    # Speaker models: Nemotron 3 Diarization and TitaNet-small (as on Linux), for the Neural Engine
    mkdir -p "$config"
    if [ -n "$speakers" ]; then
        local sd="$MODELS_DIR/speaker-diarization"
        say "Speaker models for the Neural Engine -> $sd"
        mkdir -p "$sd"
        local emb=nemo_en_titanet_small.onnx
        local emb_sha=ad4a1802485d8b34c722d2a9d04249662f2ece5d28a7a039063ca22f515a789e
        local emb_stamp="coreml $emb_sha $prep_hash"
        if [ ! -d "$sd/titanet.mlmodelc" ] || [ "$(cat "$sd/.titanet-prepared" 2>/dev/null)" != "$emb_stamp" ]; then
            fetch "$emb" "$rel/speaker-recongition-models/$emb" "$emb_sha"   # (sic: upstream's tag)
            $prep titanet "$dl/$emb" "$sd" || die "building TitaNet for the Neural Engine failed"
            echo "$emb_stamp" > "$sd/.titanet-prepared"
        fi
        local nem_dir="$sd/nemotron-3-diarization" nem_file=nemotron-3-diarization.safetensors
        local nem_rev=f667ed73aee57d40cc39428eb768b4fd87a0a29e
        local nem_sha=c074d86335b3b794f8fa5edc25594558f128bdb3914d27806a3a5a2e44963cb6
        local nem_stamp="coreml $nem_sha $prep_hash"
        if [ ! -d "$nem_dir/encoder.mlmodelc" ] || [ "$(cat "$nem_dir/.prepared" 2>/dev/null)" != "$nem_stamp" ]; then
            fetch "$nem_file" "https://huggingface.co/nvidia/Nemotron-3-Diarization/resolve/$nem_rev/model.safetensors" \
                "$nem_sha"
            $prep nemotron "$dl/$nem_file" "$nem_dir" \
                || die "building Nemotron 3 Diarization for the Neural Engine failed (--no-speakers installs without it)"
            echo "$nem_stamp" > "$nem_dir/.prepared"
        fi
        echo "  nemotron-3-diarization, ${emb%.onnx}: present"
        [ -s "$config/speakers" ] || echo on > "$config/speakers"   # keeps an "off" you set
    else
        echo off > "$config/speakers"
    fi

    # --- 6. settings, commands -----------------------------------------------------------
    say "Settings -> $config"
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
    echo "  dictation languages: $(cat "$config/languages") (now: $(cat "$config/language"))"

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
    echo "  $BIN: npu-transcribe, bmeet-ctl, bmeet"
    case ":$PATH:" in
        *":$BIN:"*) ;;
        *) echo "  note: $BIN is not in your PATH: add  export PATH=\"\$HOME/.local/bin:\$PATH\"  to ~/.zshrc" ;;
    esac

    # --- 7. Chrome extension's helper ------------------------------------------------------
    say "Chrome extension (leaves out what you say while muted in Meet / Slack)"
    bash "$src/bmeet/chrome/install-user.sh" || echo "  skipped (see above)"

    # --- 8. warm up: macOS compiles each model for the Neural Engine the first time ------------
    say "Preparing the models for the Neural Engine (the first time: a few minutes)"
    PATH="$brew_bin:$PATH" "$venv/bin/python" -I "$src/macos/warm-up" || echo "  (they are prepared on first use instead)"

    # --- 9. start ------------------------------------------------------------------------
    /System/Library/CoreServices/pbs -update >/dev/null 2>&1 || true   # (Finder's Services menu)
    if [ -n "$start" ]; then
        open "$APP"
    fi

    say "Done - BMeet $VERSION"
    cat <<EOF
  BMeet is in your menu bar (a microphone). The first time you use it, macOS asks to allow:
    Microphone              dictation and meetings
    System Audio Recording  meetings: the other side of the call
    Accessibility           pasting the dictated text where you type (System Settings ->
                            Privacy & Security -> Accessibility -> BMeet)
  ⌃⌥H        dictate: press, speak, press again -> the text is pasted where you type
  ⌃⌥⌘H       dictate to the clipboard only
  ⌃⌥M        start / stop a meeting transcript (microphone + system audio)
  ⌃⌥⇧M       the same for a meeting in the room (microphone only, split by voice)
  ⌃⌥⇧H       next dictation language ($(cat "$config/languages"))
  Finder     right-click an audio/video file -> Quick Actions or Services -> Transcribe with BMeet

  Chrome     load the extension once: chrome://extensions -> Developer mode -> Load unpacked ->
             $src/bmeet/chrome/extension

  Check the setup:  bash $src/macos/verify.sh
  Uninstall:        bash $src/uninstall.sh
EOF
}

main "$@"
