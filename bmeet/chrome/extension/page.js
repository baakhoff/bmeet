// BMeet: can the call hear your microphone? Runs inside the Meet / Slack page (MAIN world,
// before the page's own scripts). It keeps the RTCPeerConnections the page sets up and,
// while one is connected with audio, reads its statistics twice a second: how much sound
// from your microphone it sends. Muted (Meet's or Slack's button) it sends none, however
// the app mutes (a disabled track, no track, a silent one). It reads sound energy only, no
// audio. {call, live, t} goes to relay.js on every change and every 5 s during a call:
//   call  a peer connection with audio is connected
//   live  it sent sound from the microphone in the last second
//   t     when that began, as close as the polls tell: sound found in a poll began after
//         the poll before; silence began after the last poll that found sound
(() => {
  "use strict";
  const PC = window.RTCPeerConnection;
  if (!PC || PC.prototype.__bmeetCallMic) return;
  Object.defineProperty(PC.prototype, "__bmeetCallMic", { value: true });

  const POLL_MS = 500;
  const HOLD_POLLS = 2;     // sound in this poll or the one before counts as live
  const REPEAT_MS = 5000;   // repeat the state this often during a call
  const pcs = new Set();
  const energy = new WeakMap();   // pc -> Map(media-source id -> totalAudioEnergy)
  let timer = null, polls = 0, lastSound = -HOLD_POLLS, lastSoundAt = 0, prevPollAt = 0;
  let sent = null, sentAt = 0, sentT = 0, busy = false;

  for (const name of ["addTrack", "addTransceiver", "setLocalDescription", "setRemoteDescription"]) {
    const orig = PC.prototype[name];
    if (typeof orig !== "function") continue;
    const wrapped = { [name](...args) { note(this); return orig.apply(this, args); } }[name];
    Object.defineProperty(PC.prototype, name, { value: wrapped, writable: true, configurable: true });
  }

  function note(pc) {
    if (!(pc instanceof PC) || pcs.has(pc)) return;
    pcs.add(pc);
    if (!timer) timer = setInterval(poll, POLL_MS);
  }

  async function poll() {
    if (busy) return;  // (getStats can be slow on a busy page)
    busy = true;
    try {
      polls++;
      let call = false, sound = false;
      for (const pc of [...pcs]) {
        const state = pc.connectionState;
        if (state === "closed" || state === "failed") {
          pcs.delete(pc);
          continue;
        }
        if (state !== "connected") continue;
        let stats;
        try {
          stats = await pc.getStats();
        } catch {
          continue;
        }
        const sources = new Map(), sending = new Set();
        let audio = false;
        stats.forEach((r) => {
          if (r.kind !== "audio") return;
          if (r.type === "inbound-rtp" || r.type === "outbound-rtp") audio = true;
          if (r.type === "media-source") sources.set(r.id, r.totalAudioEnergy || 0);
          if (r.type === "outbound-rtp" && r.mediaSourceId && r.active !== false) sending.add(r.mediaSourceId);
        });
        if (!audio) continue;
        call = true;
        const before = energy.get(pc);
        for (const [id, e] of sources) {
          if (sending.has(id) && before && before.has(id) && e > before.get(id)) sound = true;
        }
        energy.set(pc, sources);
      }
      const now = Date.now();
      const wasLive = !!sent && sent.live;
      if (sound) {
        lastSound = polls;
        lastSoundAt = now;
      }
      const live = call && polls - lastSound < HOLD_POLLS;
      report(call, live, live && !wasLive ? prevPollAt || now : !live && wasLive && call ? lastSoundAt : now);
      prevPollAt = now;
      if (!pcs.size) {
        clearInterval(timer);
        timer = null;
      }
    } finally {
      busy = false;
    }
  }

  function report(call, live, began) {
    const now = Date.now();
    const changed = !sent || sent.call !== call || sent.live !== live;
    if (!changed && (!call || now - sentAt < REPEAT_MS)) return;
    if (!sent && !call) return;  // nothing to say before a call
    const t = changed ? Math.max(began, sentT) : now;  // (never before the last line)
    sent = { call, live };
    sentAt = now;
    sentT = t;
    document.dispatchEvent(new CustomEvent("bmeet-call-mic", { detail: JSON.stringify({ call, live, t }) }));
  }
})();
