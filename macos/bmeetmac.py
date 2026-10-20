"""bmeetmac - what BMeet's Python programs need on macOS. Stdlib only; imported on darwin only.

BMeet's programs were written for Linux, where per-user runtime files go to $XDG_RUNTIME_DIR (a
tmpfs). macOS has no such variable; setup() points it at your user's temp folder (getconf
DARWIN_USER_TEMP_DIR, the same for every process of yours), so `$XDG_RUNTIME_DIR/bmeet/...`
means the same folder for BMeet.app, the app window, npu-transcribe and bmeet-ctl. It does so
even when your shell exports XDG_RUNTIME_DIR (BMeet.app never sees that one); tests set
BMEET_RUNTIME_ROOT instead, which stands for the temp folder (BMeet's folder is
$BMEET_RUNTIME_ROOT/bmeet), here and in BMeet.app, bmeet-record and bmeet-whisper alike.
Meeting audio is the exception: it stays in RAM, on the RAM disk that BMeet.app mounts on
`$XDG_RUNTIME_DIR/bmeet/meeting-audio` (audio_in_ram()).

Also here: where the native helpers are (bmeet-whisper, bmeet-record), the client of the
Whisper server (WhisperKit on the Neural Engine), the client of BMeet.app's control socket,
and notifications.
"""
from __future__ import annotations

import ctypes
import functools
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]      # the BMeet program files (a checkout or the install)
HOME = Path.home()
_CS_DARWIN_USER_TEMP_DIR = 65537


def temp_dir() -> Path:
    """Your user's temp folder (/var/folders/.../T), whatever $TMPDIR says."""
    try:
        libc = ctypes.CDLL(None)
        buf = ctypes.create_string_buffer(1024)
        if libc.confstr(_CS_DARWIN_USER_TEMP_DIR, buf, len(buf)) > 0:
            return Path(buf.value.decode())
    except (OSError, AttributeError, UnicodeDecodeError):
        pass
    return Path(os.environ.get("TMPDIR") or "/tmp")


def setup() -> None:
    """Give BMeet's Linux-shaped code its runtime folder (see the module doc), and Homebrew's
    programs (ffmpeg): apps started from the Dock or at login get only /usr/bin:/bin in PATH."""
    if os.environ.get("BMEET_RUNTIME_ROOT"):   # (tests)
        os.environ["XDG_RUNTIME_DIR"] = os.environ["BMEET_RUNTIME_ROOT"]
    elif sys.platform == "darwin":
        os.environ["XDG_RUNTIME_DIR"] = str(temp_dir()).rstrip("/")
    else:   # (Linux: bmeet-meeting with stand-ins, in tests)
        os.environ.setdefault("XDG_RUNTIME_DIR", str(temp_dir()).rstrip("/"))
    path = os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin").split(":")
    for brew in brew_bins():
        if brew not in path and os.path.isdir(brew):
            path.append(brew)
    os.environ["PATH"] = ":".join(path)


@functools.lru_cache(maxsize=None)
def brew_bins() -> tuple[str, ...]:
    """Homebrew's bin folders: the usual two, and the one of the Homebrew that BMeet's Python
    comes from (the installer makes the venv with Homebrew's python@3.13), wherever it is."""
    bins = ["/opt/homebrew/bin", "/usr/local/bin"]
    for parent in Path(sys.base_prefix).resolve().parents:
        if (parent / "bin/brew").is_file():
            if str(parent / "bin") not in bins:
                bins.append(str(parent / "bin"))
            break
    return tuple(bins)


def runtime() -> Path:
    setup()
    return Path(os.environ["XDG_RUNTIME_DIR"]) / "bmeet"


def audio_root() -> Path:
    return runtime() / "meeting-audio"


def audio_in_ram(path: Path | None = None) -> bool:
    """Is meeting audio (audio_root(), or a recording in it) on BMeet.app's RAM disk?"""
    root = audio_root().resolve()   # (/var is a link to /private/var)
    return os.path.ismount(root) and (path is None or root in Path(path).resolve().parents
                                      or Path(path).resolve() == root)


# --------------------------------------------------------------------------- native helpers

def data_dir() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME") or HOME / ".local/share")


def helper(name: str) -> Path:
    """A native helper (bmeet-whisper, bmeet-record, ...): $BMEET_<NAME> (tests), the installed
    one (~/.local/share/bmeet/bin), or the one built in this checkout (macos/.build/release)."""
    env = os.environ.get("BMEET_" + name.upper().replace("BMEET-", "").replace("-", "_"))
    if env:
        return Path(env)
    for candidate in (data_dir() / "bmeet/bin" / name, ROOT / "macos/.build/release" / name):
        if os.access(candidate, os.X_OK):
            return candidate
    return data_dir() / "bmeet/bin" / name


# --------------------------------------------------------------------------- Whisper server

WHISPER_START_TIMEOUT = 30.0      # seconds for a new server to listen (loading is per request)


def whisper_socket() -> Path:
    return runtime() / "whisper.sock"


def start_whisper() -> None:
    """Start bmeet-whisper serve in the background (it exits by itself when idle)."""
    exe = helper("bmeet-whisper")
    if not os.access(exe, os.X_OK):
        raise RuntimeError(f"{exe} is missing - run the installer again")
    runtime().mkdir(mode=0o700, parents=True, exist_ok=True)
    log = open(runtime() / "whisper.log", "ab")
    subprocess.Popen([str(exe), "serve"], stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                     start_new_session=True, close_fds=True)
    log.close()


def whisper_up() -> None:
    """Start the Whisper server now if it is not running (without waiting for it): before niced
    work that uses it (a meeting's live transcript, the speaker pass). A server that work starts
    keeps its nice level, and would serve dictation with it too."""
    try:
        _connect(whisper_socket(), 1).close()
    except OSError:
        start_whisper()


def _connect(path: Path, timeout: float | None) -> socket.socket:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(str(path))
    except OSError:
        s.close()
        raise
    return s


def whisper(request: dict, samples=None, timeout: float | None = None) -> dict:
    """One request to the Whisper server (started if it is not running): `request` as JSON,
    then the float32 samples. Raises RuntimeError with the server's error."""
    data = b""
    if samples is not None:
        import numpy as np
        data = np.ascontiguousarray(samples, dtype="<f4").tobytes()
        request = dict(request, samples=len(data) // 4)
    try:
        s = _connect(whisper_socket(), 5)
    except OSError:
        start_whisper()
        deadline = time.monotonic() + WHISPER_START_TIMEOUT
        while True:
            time.sleep(0.1)
            try:
                s = _connect(whisper_socket(), 5)
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise RuntimeError(f"bmeet-whisper did not start (see {runtime() / 'whisper.log'})")
    with s:
        s.settimeout(timeout)
        s.sendall(json.dumps(request).encode() + b"\n" + data)
        line = s.makefile("rb").readline()
    if not line:
        raise RuntimeError("bmeet-whisper closed the connection (see " + str(runtime() / "whisper.log") + ")")
    answer = json.loads(line)
    if not answer.get("ok"):
        raise RuntimeError(answer.get("error") or "bmeet-whisper failed")
    return answer


# --------------------------------------------------------------------------- BMeet.app

def agent_socket() -> Path:
    return runtime() / "agent.sock"


def agent(command: dict, timeout: float = 5.0) -> dict | None:
    """One command to BMeet.app (its answer), or None if it is not running."""
    try:
        with _connect(agent_socket(), timeout) as s:
            s.sendall(json.dumps(command).encode() + b"\n")
            line = s.makefile("rb").readline()
    except OSError:
        return None
    try:
        return json.loads(line) if line else {}
    except ValueError:
        return {}


def agent_running() -> bool:
    return agent({"op": "ping"}, timeout=1.0) is not None


def notify(summary: str, body: str = "", ok: bool = True) -> None:
    """A notification from BMeet (BMeet.app posts it), else through osascript."""
    if agent({"op": "notify", "title": summary, "body": body, "ok": ok}) is not None:
        return
    if shutil.which("osascript"):
        # (ensure_ascii=False: AppleScript strings have no \u escapes; quotes and backslashes are escaped)
        script = (f"display notification {json.dumps(body[:500], ensure_ascii=False)} "
                  f"with title {json.dumps(summary, ensure_ascii=False)}")
        subprocess.run(["osascript", "-e", script], check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def reveal(path: Path) -> bool:
    """Show `path` selected in Finder."""
    return subprocess.run(["open", "-R", str(path)], check=False).returncode == 0


def is_mac() -> bool:
    return sys.platform == "darwin"
