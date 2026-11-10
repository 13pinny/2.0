// Kartis B2B Snipes popup: reads the show off the open Automatiq / TickPick /
// CrowdVolt tab, finds it among /snipes events (same link, else same date +
// a shared name word) and adds a rule there. A B2B rule covers Automatiq AND
// TickPick at once, so setting it on either site is enough.
const $ = (s) => document.querySelector(s);
const MONTHS = ["jan","feb","mar","apr","may","jun","jul","aug","sep","oct","nov","dec"];
const NOISE = new Set(("the and with presents present pres live at tickets ticket tour night show in of a an " +
  "b2b featuring feat ft miami florida new york nyc brooklyn la los angeles california texas chicago " +
  "illinois san diego francisco las vegas nevada day pass ga vip event").split(" "));
const ZONES = [["zone:ga", "GA"], ["zone:ga+", "GA+ / Early entry"], ["zone:vip", "VIP"]];

let cfg = { base: "https://kartis.homes", user: "", pass: "" };
let page = null;      // {platform, ref, refField, name, date}
let match = null;     // matched snipe event (overview row) or null
let picked = new Set();

function tokens(t) {
  return ((t || "").toLowerCase().match(/[a-z0-9]+/g) || []).filter((w) => w.length > 1 && !NOISE.has(w));
}
function iso(y, m, d) {
  return `${y}-${String(m).padStart(2, "0")}-${String(d).padStart(2, "0")}`;
}
function nextOccurrence(m, d) {
  const now = new Date(); let y = now.getFullYear();
  if (new Date(y, m - 1, d) < new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1)) y++;
  return iso(y, m, d);
}
function titleCase(s) { return s.replace(/\b\w/g, (c) => c.toUpperCase()); }

function readPage(url, title) {
  let u; try { u = new URL(url); } catch { return null; }
  const host = u.hostname;
  if (host.includes("automatiq.com")) {
    const m = u.pathname.match(/\/events\/([A-Z0-9]{5,})/i);
    if (!m) return null;
    // "System of a Down at Sun, 06/06/27, 6:30 PM - B2B Event - Automatiq"
    const t = (title || "").match(/^(.*) at \w{3}, (\d\d)\/(\d\d)\/(\d\d)/);
    return { platform: "automatiq", refField: "automatiq_ref",
             ref: `https://app.sync.automatiq.com/sync/b2b/events/${m[1]}`, id: m[1],
             name: t ? t[1] : (title || "").split(" - ")[0], date: t ? iso(2000 + +t[4], +t[2], +t[3]) : null };
  }
  if (host.includes("tickpick.com")) {
    const m = u.pathname.match(/^\/buy-(.+?)-(\d{1,2})-(\d{1,2})-(\d{2})-[0-9apm]+\/(\d+)\/?$/);
    if (!m) return null;
    const name = m[1].split("-tickets-")[0].replace(/-/g, " ");
    return { platform: "tickpick", refField: "tickpick_ref", ref: `https://www.tickpick.com${u.pathname}`, id: m[5],
             name: titleCase(name), date: iso(2000 + +m[4], +m[2], +m[3]) };
  }
  if (host.includes("crowdvolt.com")) {
    const m = u.pathname.match(/\/events?\/([\w-]+)/);
    if (!m) return null;
    const slug = m[1], parts = slug.toLowerCase().split("-");
    let date = null, yr = parts.find((p) => /^20\d\d$/.test(p));
    for (let i = 0; i < parts.length - 1; i++) {
      const mi = MONTHS.indexOf(parts[i].slice(0, 3));
      const d = parseInt(parts[i + 1], 10);
      if (mi >= 0 && d >= 1 && d <= 31) { date = yr ? iso(+yr, mi + 1, d) : nextOccurrence(mi + 1, d); break; }
    }
    const name = (title || "").split(/ [|–-] /)[0] || slug.replace(/-/g, " ");
    return { platform: "cv", refField: "cv_url", ref: `https://www.crowdvolt.com/event/${slug}`, id: slug,
             name, date };
  }
  return null;
}

async function api(path, body) {
  const headers = { "Content-Type": "application/json" };
  if (cfg.user) headers.Authorization = "Basic " + btoa(`${cfg.user}:${cfg.pass}`);
  const r = await fetch(cfg.base.replace(/\/$/, "") + path,
    { method: body ? "POST" : "GET", headers, body: body ? JSON.stringify(body) : undefined });
  const text = await r.text();
  let data; try { data = JSON.parse(text); } catch { data = null; }
  if (r.status === 401) throw new Error("Kartis asked for a login: open ⚙ and enter the site user/password.");
  if (!r.ok || !data) throw new Error((data && data.error) || `Kartis answered HTTP ${r.status}`);
  return data;
}

function findMatch(events) {
  const byRef = events.find((e) =>
    (page.platform === "automatiq" && (e.automatiq_ref || "").includes(page.id)) ||
    (page.platform === "tickpick" && (e.tickpick_ref || "").includes(`/${page.id}`)) ||
    (page.platform === "cv" && e.cv_event_uqid === page.id));
  if (byRef) return byRef;
  if (!page.date) return null;
  const want = new Set(tokens(page.name));
  let best = null, bestScore = 0;
  for (const e of events) {
    if (e.event_date !== page.date) continue;
    const score = tokens(e.name).filter((w) => want.has(w)).length;
    if (score > bestScore) { best = e; bestScore = score; }
  }
  return best;
}

function money(v) { return v == null ? "?" : "$" + Number(v).toFixed(v % 1 ? 2 : 0); }
function describeRule(r) {
  const secs = (r.sections || []).map((s) => (ZONES.find((z) => z[0] === s) || [s, s])[1]).join(", ") || "any section";
  const where = r.scope === "cv" ? "CrowdVolt" : "Automatiq + TickPick";
  const what = r.kind === "under" ? `listing ≤ ${money(r.price)}` : r.kind === "offer_over" ? `offer ≥ ${money(r.price)}`
    : `offer beats B2B by ≥ ${money(r.price)}`;
  return `${where}: ${what} · ${secs}${r.min_qty > 1 ? ` · qty ${r.min_qty}+` : ""}${r.include_unmapped ? " · +unmapped" : ""}`;
}

function renderChips() {
  const opts = [...ZONES, ...((match && match.sections) || []).map((s) => [s, s])];
  $("#secChips").innerHTML = "";
  for (const [val, label] of opts) {
    const c = document.createElement("span");
    c.className = "chip" + (picked.has(val) ? " on" : "");
    c.textContent = val.startsWith("zone:") ? "≈ " + label : label;
    c.title = val.startsWith("zone:") ? "matches every section named like this on all sites" : "exact section";
    c.onclick = () => { picked.has(val) ? picked.delete(val) : picked.add(val); renderChips(); };
    $("#secChips").appendChild(c);
  }
}

function render() {
  $("#msg").style.display = "none";
  $("#body").style.display = "";
  const label = { automatiq: "Automatiq", tickpick: "TickPick", cv: "CrowdVolt" }[page.platform];
  $("#site").textContent = `On ${label}` + (match ? (match.auto === "market" ? " · found in the CrowdVolt offer scan" : " · already tracked") : " · not tracked yet");
  $("#evName").textContent = match ? match.name : page.name;
  $("#evMeta").textContent = [match ? match.event_date : page.date, match && match.venue].filter(Boolean).join(" · ") || "no date found";
  const rules = (match && match.rules) || [];
  $("#rules").innerHTML = rules.length ? "" : `<li class="mut">none yet</li>`;
  for (const r of rules) {
    const li = document.createElement("li");
    li.innerHTML = `<span></span><button class="ghost">✕</button>`;
    li.firstChild.textContent = describeRule(r);
    li.lastChild.onclick = async () => {
      li.style.opacity = .4;
      try { await api("/api/snipe/rule/delete", { id: r.id }); await load(); } catch (e) { fail(e); }
    };
    $("#rules").appendChild(li);
  }
  const isCv = page.platform === "cv";
  $("#kindRow").style.display = isCv ? "" : "none";
  if (!isCv) $("#kind").value = "under";
  $("#unmappedRow").style.display = isCv ? "none" : "";
  $("#scopeNote").textContent = isCv
    ? "CrowdVolt rules watch this CrowdVolt event's listings and offers."
    : "One rule covers Automatiq and TickPick. Prices are all-in per ticket.";
  renderChips();
}

function fail(e) { $("#msg").style.display = ""; $("#msg").innerHTML = `<span class="bad"></span>`; $("#msg .bad").textContent = e.message || String(e); }

async function load() {
  const d = await api("/api/snipe");
  match = findMatch([...(d.events || []), ...(d.market || [])]);
  render();
}

async function save() {
  const price = parseFloat($("#price").value);
  if (!(price >= 0)) { $("#saved").textContent = "enter a price"; return; }
  const custom = $("#secCustom").value.split(",").map((s) => s.trim()).filter(Boolean);
  const isCv = page.platform === "cv";
  const rule = { scope: isCv ? "cv" : "b2b", kind: isCv ? $("#kind").value : "under", price,
                 min_qty: Math.max(1, parseInt($("#minQty").value, 10) || 1),
                 sections: [...picked, ...custom], include_unmapped: !isCv && $("#unmapped").checked };
  const body = { rules: [rule] };
  if (match) {
    body.id = match.id;
    if (match.auto === "market") body.claim = true;
    if (!match[page.refField]) body[page.refField] = page.ref;
  } else {
    Object.assign(body, { name: page.name, event_date: page.date, [page.refField]: page.ref });
  }
  $("#save").disabled = true; $("#saved").textContent = "saving…";
  try {
    await api("/api/snipe/event", body);
    $("#saved").innerHTML = `<span class="ok">saved</span>`;
    picked.clear(); $("#secCustom").value = ""; $("#price").value = "";
    await load();
  } catch (e) { $("#saved").textContent = ""; fail(e); }
  $("#save").disabled = false;
}

async function main() {
  cfg = Object.assign(cfg, await chrome.storage.local.get(["base", "user", "pass"]));
  $("#sBase").value = cfg.base; $("#sUser").value = cfg.user || ""; $("#sPass").value = cfg.pass || "";
  $("#gear").onclick = () => { const s = $("#settings"); s.style.display = s.style.display === "block" ? "none" : "block"; };
  $("#sSave").onclick = async () => {
    cfg = { base: $("#sBase").value.trim() || "https://kartis.homes", user: $("#sUser").value.trim(), pass: $("#sPass").value };
    await chrome.storage.local.set(cfg);
    $("#sMsg").textContent = "saved";
    if (page) load().catch(fail);
  };
  $("#save").onclick = save;
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  page = tab && readPage(tab.url, tab.title);
  if (!page) {
    $("#msg").textContent = "Open an event page on Automatiq (B2B), TickPick or CrowdVolt, then click this again.";
    return;
  }
  try { await load(); } catch (e) { fail(e); }
}
main();
