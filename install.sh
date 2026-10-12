#!/usr/bin/env bash
# BMeet installer: voice typing, meeting transcripts and file transcription
# with Whisper on the Intel NPU, for Ubuntu/Kubuntu 26.04 with KDE Plasma 6.
#
# One line (downloads the release, asks for your sudo password once):
#   curl -fsSL https://raw.githubusercontent.com/baakhoff/bmeet/main/install.sh | bash
# With options:
#   curl -fsSL https://raw.githubusercontent.com/baakhoff/bmeet/main/install.sh | bash -s -- --languages "en de"
# From a checkout:
#   ./install.sh [options]
#
# Options:
#   --languages "en de"  dictation languages that Meta+Shift+H cycles through
#                        (Whisper codes; default: English + your desktop language)
#   --no-turbo           skip whisper-large-v3-turbo (~1 GB; best for file transcripts)
#   --no-speakers        do not tell meeting speakers apart (no meeting audio is kept, not
#                        even in RAM; no speaker models)
#   --no-panel           do not add the BMeet widget to the panel
#   --no-npu-driver      do not install Intel's NPU driver even if it is missing
#   --skip-system        skip the sudo part (it must have run before)
#   --reset-config       replace your voxtype config with the default one
#   --force              try on something other than Ubuntu 26.04
#   --version            print the version this installer installs
# Env: BMEET_REF=main installs the main branch instead of the release tag.
#
# Installs into ~/.local/share/bmeet (program + Python venv),
# ~/.local/share/npu-models (Whisper models) and ~/.local/bin. Undo: uninstall.sh.

# Everything is inside main(), so a download cut short by the network cannot run half
# a script.
main() {
    set -euo pipefail

    local VERSION=0.3.0
    local REPO=baakhoff/bmeet
    local data="${XDG_DATA_HOME:-$HOME/.local/share}"
    local ROOT="$data/bmeet"
    local CACHE="${XDG_CACHE_HOME:-$HOME/.cache}/bmeet"
    local MODELS_DIR="${NPU_MODELS_DIR:-$data/npu-models}"

    if [ -t 1 ]; then
        say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
    else
        say() { printf '\n==> %s\n' "$*"; }
    fi
    die() { printf 'error: %s\n' "$*" >&2; exit 1; }

    # --- options -------------------------------------------------------------------
    local languages="" turbo=1 speakers=1 panel=1 system=1 system_args=() reset_config="" args=("$@")
    while [ $# -gt 0 ]; do
        case "$1" in
            --languages) languages="${2:?--languages needs a value, e.g. \"en de\"}"; shift ;;
            --no-turbo) turbo= ;;
            --no-speakers) speakers= ;;
            --no-panel) panel= ;;
            --no-npu-driver) system_args+=(--no-npu-driver) ;;
            --skip-system) system= ;;
            --reset-config) reset_config=--reset-config ;;
            --force) system_args+=(--force) ;;
            --version) echo "BMeet installer $VERSION"; return 0 ;;
            -h|--help)
                if [ -f "${BASH_SOURCE[0]:-}" ]; then sed -n '2,27p' "${BASH_SOURCE[0]}"
                else echo "See https://github.com/$REPO#install"; fi
                return 0 ;;
            *) die "unknown option: $1 (see --help)" ;;
        esac
        shift
    done

    [ "$(id -u)" -ne 0 ] || die "run this as your normal user (it asks for sudo itself)"

    # --- bootstrap: running from a pipe -> fetch the release and run its install.sh ---
    local self="${BASH_SOURCE[0]:-}" src
    # (piped into bash, BASH_SOURCE[0] is "bash" or empty: never a file named install.sh)
    if [ "$(basename -- "$self")" = install.sh ] && [ -f "$self" ] && [ -f "$(dirname "$self")/VERSION" ]; then
        src="$(cd "$(dirname "$self")" && pwd)"
    else
        local ref="${BMEET_REF:-v$VERSION}" tmp
        command -v curl >/dev/null || die "curl is needed (sudo apt install curl)"
        command -v tar >/dev/null || die "tar is needed"
        say "Downloading BMeet $ref"
        tmp="$(mktemp -d "${TMPDIR:-/tmp}/bmeet.XXXXXX")"
        trap 'rm -rf "$tmp"' EXIT   # on failure; the real installer removes it when done
        curl -fsSL "https://github.com/$REPO/archive/$ref.tar.gz" | tar -xz -C "$tmp" --strip-components=1 \
            || die "could not download https://github.com/$REPO/archive/$ref.tar.gz"
        [ -f "$tmp/install.sh" ] || die "the download does not look like BMeet"
        export BMEET_TMP="$tmp"   # the real installer removes it when done
        trap - EXIT
        # stdin is the pipe from curl: give the real installer the terminal instead (if
        # there is one: not under ssh without -t, cloud-init or CI)
        if { : </dev/tty; } 2>/dev/null; then
            exec bash "$tmp/install.sh" ${args[@]+"${args[@]}"} </dev/tty
        fi
        exec bash "$tmp/install.sh" ${args[@]+"${args[@]}"}
    fi
    local version
    version="$(cat "$src/VERSION")"
    if [ -n "${BMEET_TMP:-}" ] && [ "$src" = "$(cd "$BMEET_TMP" && pwd)" ]; then
        trap 'rm -rf "$BMEET_TMP"' EXIT   # the downloaded release
    fi

    # --- preflight -------------------------------------------------------------------
    say "BMeet $version"
    . /etc/os-release
    echo "  system:  ${PRETTY_NAME:-unknown}, $(uname -m)"
    echo "  desktop: ${XDG_CURRENT_DESKTOP:-unknown} (${XDG_SESSION_TYPE:-?})"
    if [ -e /dev/accel/accel0 ]; then
        echo "  NPU:     /dev/accel/accel0"
    else
        echo "  NPU:     not found - everything will run on the CPU (slower)"
    fi
    [[ ${XDG_CURRENT_DESKTOP:-} == *KDE* ]] \
        || echo "  note: made for KDE Plasma 6; the shortcuts and the panel widget need it"
    command -v systemctl >/dev/null && systemctl --user show-environment >/dev/null 2>&1 \
        || die "no systemd user session - run this from your desktop session"
    case "$(head -n1 "${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/voxtype/meeting_state" 2>/dev/null || echo idle)" in
        recording|paused) die "a meeting is being recorded - stop it first (Meta+Alt+M), then run this again" ;;
    esac
    local free_gb
    free_gb=$(( $(df -Pk "$HOME" | awk 'NR==2 {print $4}') / 1024 / 1024 ))
    [ "$free_gb" -ge 6 ] || die "only ${free_gb} GB free in $HOME; about 6 GB are needed"

    # BMeet was called npu-voicenotes before 0.2.0: reuse its downloads (no second ~600 MB
    # fetch); its program files and venv are removed at the end.
    local old_root="$data/npu-voicenotes" old_cache="${XDG_CACHE_HOME:-$HOME/.cache}/npu-voicenotes"
    if [ -d "$old_cache" ] && [ ! -e "$CACHE" ]; then
        mv "$old_cache" "$CACHE"
    fi

    # --- 1. system part (sudo) -----------------------------------------------------------
    if [ -n "$system" ]; then
        say "System part (sudo): packages, NPU driver, voxtype, OpenVINO GenAI SDK"
        echo "  Downloads (~600 MB) go to $CACHE/downloads and are checked against pinned hashes."
        sudo bash "$src/system/install.sh" --cache "$CACHE/downloads" ${system_args[@]+"${system_args[@]}"}
    fi

    # --- 2. program files ------------------------------------------------------------
    say "Program files -> $ROOT/src"
    mkdir -p "$ROOT"
    local stage="$ROOT/.src.new"
    rm -rf "$stage"
    mkdir -p "$stage"
    # the runtime tree only (no tests, docs, CI)
    tar -C "$src" --exclude='tests' --exclude='__pycache__' --exclude='.work' \
        -cf - VERSION LICENSE install.sh uninstall.sh system voxtype transcribe bmeet \
        | tar -C "$stage" -xf -
    rm -rf "$ROOT/src.old"
    [ ! -d "$ROOT/src" ] || mv "$ROOT/src" "$ROOT/src.old"
    mv "$stage" "$ROOT/src"
    rm -rf "$ROOT/src.old"
    src="$ROOT/src"
    echo "  $version installed"

    # --- 3. Python venv for npu-transcribe ------------------------------------------------
    local venv="$ROOT/venv"
    say "Python environment -> $venv"
    # the system Python (python3-venv): the app uses the system PyQt6 with it as well
    if ! "$venv/bin/python" -m pip --version >/dev/null 2>&1; then
        rm -rf "$venv"
        if ! /usr/bin/python3 -m venv "$venv"; then
            rm -rf "$venv"
            die "could not create a Python venv (sudo apt install python3-venv)"
        fi
    fi
    echo "  OpenVINO GenAI + numpy (the first run downloads ~200 MB)"
    "$venv/bin/python" -m pip install --quiet --disable-pip-version-check --upgrade pip
    "$venv/bin/python" -m pip install --quiet --disable-pip-version-check -r "$src/transcribe/requirements.txt"
    # (with OpenVINO's telemetry module blocked, as npu-transcribe does: importing
    # openvino would otherwise count as consent and send a usage event)
    "$venv/bin/python" -I -c 'import sys; sys.modules["openvino_telemetry"] = None
import openvino_genai, numpy; print("  openvino-genai", openvino_genai.__version__)'

    # --- 4. Whisper models -------------------------------------------------------------
    say "Whisper models -> $MODELS_DIR"
    mkdir -p "$MODELS_DIR"
    # OpenVINO's int8 exports on Hugging Face, pinned to the revision that was tested.
    local models=("OpenVINO/whisper-small-int8-ov 5b831719e093f86e1970be663e524fe001489f9b")
    [ -z "$turbo" ] || models+=("OpenVINO/whisper-large-v3-turbo-int8-ov b568445dd5dc8c695bde596f8acbb4694fd6ba64")
    local m repo rev dir
    for m in "${models[@]}"; do
        repo=${m% *} rev=${m#* } dir="$MODELS_DIR/${repo#*/}"
        if [ -f "$dir/openvino_encoder_model.xml" ] && [ -f "$dir/openvino_decoder_model.bin" ]; then
            echo "  ${repo#*/}: present"
            continue
        fi
        echo "  ${repo#*/}: downloading (revision ${rev:0:8})"
        HF_HUB_DISABLE_TELEMETRY=1 "$venv/bin/hf" download "$repo" --revision "$rev" \
            --local-dir "$dir" >/dev/null
    done

    # --- 4b. speaker models (meetings: Person 1, Person 2, ...; npu-transcribe --speakers) ---
    # sherpa-onnx's exports, sha256-pinned: pyannote segmentation-3.0 (MIT) and the
    # speaker-embedding model (see docs/npu-transcribe.md for the choice and its license).
    local speakers_file="${XDG_CONFIG_HOME:-$HOME/.config}/bmeet/speakers"
    mkdir -p "$(dirname "$speakers_file")"
    if [ -n "$speakers" ]; then
        local sd="$MODELS_DIR/speaker-diarization" dl="$CACHE/downloads"
        local rel=https://github.com/k2-fsa/sherpa-onnx/releases/download
        local seg_tar=sherpa-onnx-pyannote-segmentation-3-0.tar.bz2
        local seg_tar_sha=24615ee884c897d9d2ba09bb4d30da6bb1b15e685065962db5b02e76e4996488
        local seg_sha=220ad67ca923bef2fa91f2390c786097bf305bceb5e261d4af67b38e938e1079
        local emb=nemo_en_titanet_small.onnx
        local emb_sha=ad4a1802485d8b34c722d2a9d04249662f2ece5d28a7a039063ca22f515a789e
        say "Speaker models -> $sd"
        mkdir -p "$sd" "$dl"
        fetch() {  # file url sha256: download into the cache once, check it every time
            if ! echo "$3  $dl/$1" | sha256sum -c --status >/dev/null 2>&1; then
                curl -fL --retry 3 --progress-bar -o "$dl/$1.part" "$2" && mv "$dl/$1.part" "$dl/$1"
                echo "$3  $dl/$1" | sha256sum -c --status \
                    || { rm -f "$dl/$1"; die "checksum mismatch for $1 - run the installer again"; }
            fi
        }
        if ! echo "$seg_sha  $sd/pyannote-segmentation-3-0.onnx" | sha256sum -c --status >/dev/null 2>&1; then
            fetch "$seg_tar" "$rel/speaker-segmentation-models/$seg_tar" "$seg_tar_sha"
            "$venv/bin/python" -I -c 'import sys, tarfile
with tarfile.open(sys.argv[1]) as t:
    data = t.extractfile("sherpa-onnx-pyannote-segmentation-3-0/model.onnx").read()
with open(sys.argv[2], "wb") as f:
    f.write(data)' "$dl/$seg_tar" "$sd/.segmentation.part"
            echo "$seg_sha  $sd/.segmentation.part" | sha256sum -c --status \
                || { rm -f "$sd/.segmentation.part"; die "unexpected segmentation model in $seg_tar"; }
            chmod 0644 "$sd/.segmentation.part"
            mv "$sd/.segmentation.part" "$sd/pyannote-segmentation-3-0.onnx"
        fi
        if ! echo "$emb_sha  $sd/$emb" | sha256sum -c --status >/dev/null 2>&1; then
            fetch "$emb" "$rel/speaker-recongition-models/$emb" "$emb_sha"   # (sic: upstream's tag)
            install -m 0644 "$dl/$emb" "$sd/$emb"
        fi
        echo "  pyannote-segmentation-3-0, ${emb%.onnx}: present"
        [ -s "$speakers_file" ] || echo on > "$speakers_file"   # keeps an "off" you set
    else
        echo off > "$speakers_file"
        echo "  skipped (--no-speakers): meetings keep the You / Remote labels"
    fi

    # --- 5. per-user parts -----------------------------------------------------------
    say "Dictation and meetings (voxtype)"
    NPU_MODELS_DIR="$MODELS_DIR" BMEET_LANGUAGES="$languages" \
        bash "$src/voxtype/user-setup.sh" $reset_config

    say "File transcription (npu-transcribe + Dolphin menu)"
    NPU_MODELS_DIR="$MODELS_DIR" bash "$src/transcribe/install-user.sh" --venv "$venv"

    say "BMeet app"
    bash "$src/bmeet/app/install-user.sh"

    say "BMeet panel widget"
    if command -v kpackagetool6 >/dev/null; then
        bash "$src/bmeet/plasmoid/install-user.sh" ${panel:+--add-to-panel}
    else
        echo "  skipped: kpackagetool6 not found (the widget needs KDE Plasma 6)"
    fi

    if [ -d "$old_root" ]; then
        say "Removing the old npu-voicenotes install"
        rm -rf "$old_root"
        echo "  removed $old_root (BMeet replaces it; your settings and transcripts were moved or kept)"
    fi

    # --- done --------------------------------------------------------------------------
    say "Done - BMeet $version"
    cat <<EOF
  Meta+H          dictate: press, speak, press again -> text is typed at the cursor
  Meta+Alt+H      dictate to the clipboard only (for terminals: then Ctrl+Shift+V)
  Meta+Alt+M      start / stop a meeting transcript (microphone + system audio)
  Meta+Shift+H    next dictation language ($(cat "${XDG_CONFIG_HOME:-$HOME/.config}/bmeet/languages" 2>/dev/null || echo en))
  BMeet           app menu, or the microphone in the panel: meetings, transcripts, files
  Dolphin         right-click an audio/video file -> Transcribe (NPU)

  Check the setup:  bash $src/voxtype/verify.sh
  Uninstall:        bash $src/uninstall.sh
  If a shortcut does nothing, log out and in once.
EOF
}

main "$@"
