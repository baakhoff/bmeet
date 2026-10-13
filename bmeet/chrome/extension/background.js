// BMeet: the state of all calls (tabs and frames) together, for bmeet-chrome-host, which
// writes it into the meeting BMeet is recording (if any). In a call while any tab is;
// heard while any call sends your microphone's sound. Sent on every change (with the time
// the page says it began) and every 5 s during a call; between calls the worker sleeps
// and the host exits.
const HOST = "io.github.baakhoff.bmeet";
const STALE_MS = 12000;   // a frame that stopped reporting (crashed, navigated) no longer counts
const REPEAT_MS = 5000;
const frames = new Map(); // "tab:frame" -> {call, live, at}
const MAX_SKEW_MS = 10000;  // a page's time further off than this is replaced by ours
let port = null, last = null, lastAt = 0, lastT = 0;

function send(message) {
  try {
    if (!port) {
      port = chrome.runtime.connectNative(HOST);
      port.onDisconnect.addListener(() => {
        void chrome.runtime.lastError;  // host not installed, or it exited
        port = null;
      });
    }
    port.postMessage(message);
  } catch {
    port = null;
  }
}

function update(began) {
  const now = Date.now();
  let call = false, live = false;
  for (const [key, f] of frames) {
    if (now - f.at > STALE_MS) {
      frames.delete(key);
      continue;
    }
    call = call || f.call;
    live = live || (f.call && f.live);
  }
  const changed = !last || last.call !== call || last.live !== live;
  if (!changed && (!call || now - lastAt < REPEAT_MS)) return;
  if (!last && !call) return;
  const ok = typeof began === "number" && Math.abs(began - now) < MAX_SKEW_MS;
  const t = changed && ok ? Math.max(began, lastT) : now;  // (never before the last line)
  last = { call, live };
  lastAt = now;
  lastT = t;
  send({ t: t / 1000, call, live });
}

chrome.runtime.onMessage.addListener((msg, sender) => {
  if (!sender.tab || typeof msg?.call !== "boolean" || typeof msg?.live !== "boolean") return;
  frames.set(`${sender.tab.id}:${sender.frameId}`, { call: msg.call, live: msg.live, at: Date.now() });
  update(msg.t);
});

chrome.tabs.onRemoved.addListener((tabId) => {
  let gone = false;
  for (const key of frames.keys()) {
    if (key.startsWith(`${tabId}:`)) gone = frames.delete(key) || gone;
  }
  if (gone) update();
});
