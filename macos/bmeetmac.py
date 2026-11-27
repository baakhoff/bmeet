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
Whisper server (WhisperKit on the Neural Engine), predictions of the Core ML models that are
safe from Core ML's own threads (coreml_predict), the client of BMeet.app's control socket,
and notifications.
"""
from __future__ import annotations

import atexit
import ctypes
import functools
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]      # the BMeet program files (a checkout or the install)
HOME = Path.home()
# BMeet for macOS is released on its own (docs/releasing.md): the programs it shares with Linux
# carry the Linux version, and report this one on a Mac
try:
    VERSION = (ROOT / "macos" / "VERSION").read_text().strip()
except OSError:
    VERSION = "?"
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


def whisper_unload(model: str | None = None, compute: str | None = None) -> list[str] | None:
    """Have the Whisper server release `model` (every model when None; on `compute` only, "ane" or
    "cpu", when given) now. Returns what it released, or None when no server runs (none is
    started for this)."""
    request = {"op": "unload", **({"model": model} if model else {}), **({"compute": compute} if compute else {})}
    try:
        with _connect(whisper_socket(), 5) as s:
            s.settimeout(None)    # (it waits for the request that runs)
            s.sendall(json.dumps(request).encode() + b"\n")
            line = s.makefile("rb").readline()
    except OSError:
        return None
    answer = json.loads(line) if line else {}
    if not answer.get("ok"):
        raise RuntimeError(answer.get("error") or "bmeet-whisper failed")
    return answer.get("unloaded", [])


def memory_pressure() -> str:
    """"warning" or "critical" while macOS says memory is short, else "": the level behind its
    memory-pressure events (sysctl kern.memorystatus_vm_pressure_level: 1 normal, 2 warning,
    4 critical). One system call, no process: cheap enough for a process's idle tick."""
    level, size = ctypes.c_int(0), ctypes.c_size_t(ctypes.sizeof(ctypes.c_int))
    try:
        if ctypes.CDLL(None).sysctlbyname(b"kern.memorystatus_vm_pressure_level", ctypes.byref(level),
                                          ctypes.byref(size), None, ctypes.c_size_t(0)) != 0:
            return ""
    except (OSError, AttributeError):
        return ""
    return {2: "warning", 4: "critical"}.get(level.value, "")


# --------------------------------------------------------------------------- Core ML

# Core ML keeps the inputs of a model's last prediction after predict() has returned, and lets go
# of them later: at the model's next prediction, when the model is freed, or on a thread of its
# own (the reset queue of MLE5ExecutionStream, ~1 s after the first prediction of a model loaded
# on the Neural Engine, macOS 27). coremltools (9.0) passes numpy arrays to it without a copy: its
# MLMultiArray (PybindCompatibleArray) keeps a reference to the array and drops it in its
# destructor, without Python's lock and on a thread that Python does not know. Where that is the
# last reference, numpy frees the array there, and Python 3.13 crashes (EXC_BAD_ACCESS at 0x10:
# the thread state it looks for is NULL). That ended the speaker pass of 2026-11-27 (GigaAM's
# feature_lengths, an int32 array nothing else held, freed while the pass waited for Whisper) and
# four of the install's model checks of 2026-11-26.
# So every input Core ML gets is a copy that only _HELD refers to, through the dict handed to
# predict(), until Core ML has let go of it. Nothing in Python touches those arrays meanwhile (a
# reference count changed from two threads at once could come out wrong): whether Core ML still
# holds one is read from the array's reference count in memory, without changing it. One that is
# back to 1 (only that dict) is Core ML's no longer, and is freed here, by Python.
_HELD: list[list] = []          # [dict handed to predict(), addresses of its arrays, predict() returned]
_HELD_LOCK = threading.Lock()


def _refcount(address: int) -> int:
    return ctypes.c_ssize_t.from_address(address).value


@functools.lru_cache(maxsize=None)
def _refcounts_readable() -> bool:
    """Is the reference count where _refcount() reads it (CPython with the GIL: ob_refcnt first)?"""
    probe = object()
    keep = [probe]   # noqa: F841  (a second reference, so the count is not a constant)
    return _refcount(id(probe)) == sys.getrefcount(probe) - 1


def coreml_predict(model, inputs: dict) -> dict:
    """model.predict(inputs) for a coremltools model, with inputs that Core ML may let go of on
    any thread (see above). int64 arrays are given as int32 (Core ML takes no int64)."""
    import numpy as np
    feed = {}
    for name, value in inputs.items():
        given = np.asarray(value)
        feed[name] = np.array(given, dtype=np.int32 if given.dtype == np.int64 else given.dtype,
                              order="C", copy=True)
    entry = [feed, tuple(id(a) for a in feed.values()), False]
    with _HELD_LOCK:
        _drop_released()
        _HELD.append(entry)
    try:
        return model.predict(feed)
    finally:
        with _HELD_LOCK:
            entry[2] = True


def release_coreml_inputs() -> int:
    """Free the inputs Core ML has let go of (each prediction does it too; a process that goes idle
    calls it). Returns how many predictions' inputs are still held."""
    with _HELD_LOCK:
        _drop_released()
        return len(_HELD)


def _drop_released() -> None:
    """(with _HELD_LOCK) Drop the entries whose predict() has returned and whose arrays are back to
    one reference each. On a Python where the count cannot be read so, everything stays: safe."""
    if _refcounts_readable():
        _HELD[:] = [e for e in _HELD if not e[2] or any(_refcount(address) > 1 for address in e[1])]


@atexit.register
def _keep_held_inputs() -> None:
    """At exit, inputs Core ML still holds must not be freed by Python's shutdown (Core ML would
    then drop the last reference later, on its own thread): their dicts get one more reference,
    from nowhere, so they stay. (Only Python touches a dict's count: Core ML holds the arrays.)"""
    with _HELD_LOCK:
        _drop_released()
        for e in _HELD:
            ctypes.pythonapi.Py_IncRef(ctypes.py_object(e[0]))


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
    """A notification from BMeet (BMeet.app posts it), else through osascript. Tests: with
    BMEET_NOTIFY_LOG it is appended to that file as a JSON line instead, and with BMEET_RUNTIME_ROOT
    (a test's runtime folder) osascript is never used, so a test never puts one on the screen."""
    if log := os.environ.get("BMEET_NOTIFY_LOG"):
        with open(log, "a", encoding="utf-8") as f:
            f.write(json.dumps({"title": summary, "body": body, "ok": ok}, ensure_ascii=False) + "\n")
        return
    if agent({"op": "notify", "title": summary, "body": body, "ok": ok}) is not None:
        return
    if os.environ.get("BMEET_RUNTIME_ROOT"):
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
