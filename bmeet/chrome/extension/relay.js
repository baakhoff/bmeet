// BMeet: passes page.js's {call, live} (a DOM event: the page's world cannot reach the
// extension) on to the service worker.
document.addEventListener("bmeet-call-mic", (event) => {
  let msg;
  try {
    msg = JSON.parse(event.detail);
  } catch {
    return;
  }
  if (!msg || typeof msg.call !== "boolean" || typeof msg.live !== "boolean") return;
  try {
    chrome.runtime.sendMessage({ call: msg.call, live: msg.live, t: Number(msg.t) || Date.now() }).catch(() => {});
  } catch {
    // the extension was reloaded or removed: this page keeps the old content script
  }
});
