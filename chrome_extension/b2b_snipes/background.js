// Badge: a green dot (or the rule count) on the toolbar icon when the tab is
// a show you already track; grey "off" when that show is paused.
importScripts("common.js");

const INDEX_TTL_MS = 60 * 1000;
let cache = { at: 0, events: [] };

async function trackedEvents(force) {
  if (!force && Date.now() - cache.at < INDEX_TTL_MS) return cache.events;
  try {
    const d = await api("/api/snipe/index");
    cache = { at: Date.now(), events: d.events || [] };
  } catch (e) {
    cache.at = Date.now();          // don't hammer a failing server
  }
  return cache.events;
}

async function paint(tabId, url, title) {
  const page = url && readPage(url, title);
  if (!page) {
    chrome.action.setBadgeText({ tabId, text: "" });
    chrome.action.setTitle({ tabId, title: "B2B Snipes" });
    return;
  }
  const ev = findMatch(await trackedEvents(false), page);
  if (!ev) {
    chrome.action.setBadgeText({ tabId, text: "" });
    chrome.action.setTitle({ tabId, title: "B2B Snipes: not tracked yet" });
    return;
  }
  const off = ev.paused || !ev.n_rules;
  chrome.action.setBadgeBackgroundColor({ tabId, color: off ? "#9aa1ad" : "#059669" });
  chrome.action.setBadgeText({ tabId, text: ev.paused ? "off" : ev.n_rules ? String(ev.n_rules) : "•" });
  const lp = ev.last_ping;
  chrome.action.setTitle({ tabId, title:
    `B2B Snipes: tracking ${ev.name}` +
    `\n${ev.n_rules} active rule${ev.n_rules === 1 ? "" : "s"}${ev.paused ? " · paused" : ""}${ev.muted ? " · muted" : ""}` +
    (lp ? `\nlast ping ${new Date(lp.at).toLocaleString()}: ${lp.title}` : "") });
}

chrome.tabs.onUpdated.addListener((tabId, info, tab) => {
  if (info.status === "complete" || info.url || info.title) paint(tabId, tab.url, tab.title);
});
chrome.tabs.onActivated.addListener(async ({ tabId }) => {
  try { const tab = await chrome.tabs.get(tabId); paint(tabId, tab.url, tab.title); } catch {}
});
// the popup changed something: repaint now with fresh data
chrome.runtime.onMessage.addListener((msg) => {
  if (msg && msg.type === "refresh") {
    trackedEvents(true).then(async () => {
      const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
      if (tab) paint(tab.id, tab.url, tab.title);
    });
  }
});
