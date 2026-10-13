#!/usr/bin/env python3
"""End-to-end test of BMeet's Chrome extension + bmeet-chrome-host, in a throwaway Chrome.

A local page sets up a WebRTC call with itself (two RTCPeerConnections) and sends the
microphone: Chrome's fake one, fed a steady tone. The page then mutes it the ways a call
app can: a disabled track, no track, a silent WebAudio track, an inactive sender; and ends
the call. The extension (with localhost added to its pages) must log, through the real
native host, call + live while the tone is sent and call + not live while muted.

Your Chrome profile, microphone and BMeet meetings are not touched: the browser runs
headless with its own profile, and the host writes into a scratch "meeting".
Needs google-chrome (or chromium); it is driven over the DevTools pipe (stdlib only).

Usage: test_extension.py [WORKDIR]     (default: a temp dir, removed afterwards)
Exit status 0 = passed.
"""
from __future__ import annotations

import http.server
import json
import math
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import wave
from pathlib import Path

HERE = Path(__file__).resolve().parent
EXT = HERE.parent / "extension"
HOST = HERE.parent / "bmeet-chrome-host"

PAGE = """<!doctype html><meta charset="utf-8"><title>call</title><script>
let stream, track, sender, pcs = [], ctx, gain, processed;
async function connect(a, b) {
  a.onicecandidate = e => e.candidate && b.addIceCandidate(e.candidate);
  b.onicecandidate = e => e.candidate && a.addIceCandidate(e.candidate);
  const offer = await a.createOffer(); await a.setLocalDescription(offer); await b.setRemoteDescription(offer);
  const answer = await b.createAnswer(); await b.setLocalDescription(answer); await a.setRemoteDescription(answer);
}
async function start() {
  stream = await navigator.mediaDevices.getUserMedia({audio: {echoCancellation: false,
    noiseSuppression: false, autoGainControl: false}});
  track = stream.getAudioTracks()[0];
  ctx = new AudioContext();
  const src = ctx.createMediaStreamSource(stream);
  gain = ctx.createGain();
  const dst = ctx.createMediaStreamDestination();
  src.connect(gain).connect(dst);
  processed = dst.stream.getAudioTracks()[0];
  const a = new RTCPeerConnection(), b = new RTCPeerConnection();
  pcs = [a, b];
  sender = a.addTrack(track, stream);
  b.ontrack = e => { const el = new Audio(); el.srcObject = e.streams[0]; el.muted = true; el.play(); };
  await connect(a, b);
  return "started";
}
async function active(on) {
  const p = sender.getParameters(); p.encodings.forEach(e => e.active = on); await sender.setParameters(p);
  return "active " + on;
}
</script>"""

STEPS = [  # (what the page does, expected live, seconds)
    ("start()", True, 6),
    ("track.enabled = false", False, 6),
    ("track.enabled = true", True, 5),
    ("sender.replaceTrack(null)", False, 6),
    ("sender.replaceTrack(processed)", True, 5),
    ("gain.gain.value = 0", False, 6),
    ("gain.gain.value = 1", True, 5),
    ("active(false)", False, 6),
    ("active(true)", True, 5),
    ("pcs.forEach(pc => pc.close())", None, 4),   # call over
]


def tone(path: Path, seconds: int = 120) -> None:
    rate = 48000
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * 440 * i / rate)))
                               for i in range(rate * seconds)))


class Cdp:
    """Chrome DevTools Protocol over --remote-debugging-pipe (fd 3 in, fd 4 out)."""

    def __init__(self, cmd: list[str], env: dict) -> None:
        to_chrome_r, self.to_chrome = os.pipe()
        self.from_chrome, from_chrome_w = os.pipe()

        def fds():
            os.dup2(to_chrome_r, 3)
            os.dup2(from_chrome_w, 4)
        self.proc = subprocess.Popen(cmd, env=env, preexec_fn=fds, pass_fds=(3, 4),
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        os.close(to_chrome_r)
        os.close(from_chrome_w)
        self.buf, self.next_id = b"", 0

    def call(self, method: str, session: str | None = None, **params) -> dict:
        self.next_id += 1
        msg = {"id": self.next_id, "method": method, "params": params}
        if session:
            msg["sessionId"] = session
        os.write(self.to_chrome, json.dumps(msg).encode() + b"\0")
        while True:
            while b"\0" not in self.buf:
                chunk = os.read(self.from_chrome, 1 << 16)
                if not chunk:
                    raise RuntimeError("Chrome went away")
                self.buf += chunk
            raw, self.buf = self.buf.split(b"\0", 1)
            reply = json.loads(raw)
            if reply.get("id") == self.next_id:
                if "error" in reply:
                    raise RuntimeError(f"{method}: {reply['error']}")
                return reply.get("result", {})

    def close(self) -> None:
        try:
            self.call("Browser.close")
        except Exception:
            pass
        try:
            self.proc.wait(10)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def main() -> int:
    chrome = shutil.which("google-chrome") or shutil.which("chromium") or shutil.which("chromium-browser")
    if not chrome:
        print("SKIP: no google-chrome / chromium")
        return 0
    keep = len(sys.argv) > 1
    work = Path(sys.argv[1]) if keep else Path(tempfile.mkdtemp(prefix="bmeet-ext-"))
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    try:
        # the extension, also on localhost (the test page)
        ext = work / "extension"
        shutil.copytree(EXT, ext)
        manifest = json.loads((ext / "manifest.json").read_text())
        for cs in manifest["content_scripts"]:
            cs["matches"].append("http://localhost/*")
        (ext / "manifest.json").write_text(json.dumps(manifest))
        profile = work / "profile"
        (profile / "NativeMessagingHosts").mkdir(parents=True)
        (profile / "NativeMessagingHosts/io.github.baakhoff.bmeet.json").write_text(json.dumps({
            "name": "io.github.baakhoff.bmeet", "description": "test", "path": str(HOST), "type": "stdio",
            "allowed_origins": ["chrome-extension://pfhnhicfaajjfidmknapnoekclpjoaoc/"]}))
        meeting = work / "meeting-audio" / "2026-10-13_1200"
        meeting.mkdir(parents=True)
        (work / "recording").write_text(f"{meeting}\n")
        (work / "www").mkdir()
        (work / "www/index.html").write_text(PAGE)
        tone(work / "tone.wav")

        handler = lambda *a: http.server.SimpleHTTPRequestHandler(*a, directory=str(work / "www"))  # noqa: E731
        http.server.SimpleHTTPRequestHandler.log_message = lambda *a: None
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        url = f"http://localhost:{server.server_address[1]}/index.html"

        env = dict(os.environ, BMEET_RECORDING_FILE=str(work / "recording"),
                   BMEET_AUDIO_DIR=str(work / "meeting-audio"))
        cdp = Cdp([chrome, "--headless=new", f"--user-data-dir={profile}", "--remote-debugging-pipe",
                   "--enable-unsafe-extension-debugging", "--no-first-run", "--no-default-browser-check",
                   "--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream",
                   f"--use-file-for-fake-audio-capture={work / 'tone.wav'}", "--autoplay-policy=no-user-gesture-required",
                   "about:blank"], env)
        try:
            ext_id = cdp.call("Extensions.loadUnpacked", path=str(ext))["id"]
            assert ext_id == "pfhnhicfaajjfidmknapnoekclpjoaoc", ext_id
            target = cdp.call("Target.createTarget", url=url)["targetId"]
            session = cdp.call("Target.attachToTarget", targetId=target, flatten=True)["sessionId"]
            time.sleep(2)
            marks = []
            for js, live, seconds in STEPS:
                r = cdp.call("Runtime.evaluate", session, expression=js, awaitPromise=True)
                if r.get("exceptionDetails"):
                    print(f"FAIL: {js}: {r['exceptionDetails']}")
                    return 1
                marks.append((time.time(), js, live))
                time.sleep(seconds)
        finally:
            cdp.close()
            server.shutdown()

        log = meeting / "call-mic.jsonl"
        lines = [json.loads(x) for x in log.read_text().splitlines()] if log.exists() else []
        print(f"  {len(lines)} line(s) in call-mic.jsonl")
        ok = bool(lines)
        for i, (t0, js, live) in enumerate(marks):
            t1 = marks[i + 1][0] if i + 1 < len(marks) else time.time()
            # the state logged by the end of the step, and how soon after the change
            def state(x):
                return x["live"] if x["call"] else None
            window = [x for x in lines if t0 - 0.6 <= x["t"] <= t1 - 0.2]  # (changes are backdated)
            changed = next((x["t"] - t0 for j, x in enumerate(window)
                            if all(state(y) == live for y in window[j:])), None)
            good = changed is not None and -0.6 <= changed < 1.0
            ok &= good
            want = "call, live" if live else "call, not live" if live is False else "no call"
            print(f"  {'ok  ' if good else 'FAIL'} {js:32s} -> {want}: "
                  + (f"logged {changed:.1f} s later" if changed is not None else "not logged"))
        print("PASS" if ok else "FAIL")
        return 0 if ok else 1
    finally:
        if not keep:
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
