// Kartis B2B Snipes popup: reads the show off the open Automatiq / TickPick /
// CrowdVolt tab, finds it among /snipes events, and lets you add, edit, pause
// and delete its rules. A B2B rule covers Automatiq AND TickPick at once.
const $ = (s) => document.querySelector(s);
const ZONES = [["zone:ga", "GA"], ["zone:ga+", "GA+ / Early entry"], ["zone:vip", "VIP"]];
const PL = { automatiq: "AQ", tickpick: "TP", cv: "CV" };

let tab = null;       // the active tab
let page = null;      // {platform, ref, refField, id, name, date}
let detail = null;    // /api/snipe/event/detail for the matched show, or null
let pageSecs = null;  // sections read straight off the page, or null
let picked = new Set();

function money(v) { return v == null ? "?" : "$" + Number(v).toFixed(v % 1 ? 2 : 0); }
function ago(iso) {
  if (!iso) return "never";
  const s = (Date.now() - new Date(iso)) / 1000;
  if (s < 90) return Math.round(s) + "s ago";
  if (s < 5400) return Math.round(s / 60) + "m ago";
  if (s < 129600) return Math.round(s / 3600) + "h ago";
  return Math.round(s / 86400) + "d ago";
}
function zoneOf(sec) {
  const t = (sec || "").toLowerCase();
  if (/\bga\s*\+|\bga plus\b|\bearly entry\b|\bpriority entry\b/.test(t)) return "ga+";
  if (/\bvip\b/.test(t)) return "vip";
  if (/\bga\b|general admission|\bgen\.? adm|^(general|standing|floor)$/.test(t)) return "ga";
  return null;
}
function fail(e) {
  $("#msg").style.display = "";
  $("#msg").innerHTML = `<span class="bad"></span>`;
  $("#msg .bad").textContent = e.message || String(e);
}
function refreshBadge() { chrome.runtime.sendMessage({ type: "refresh" }).catch(() => {}); }

// --- sections straight off the page (runs inside the tab, your own session) ---

function pageReader(platform, id) {
  // Injected into the page (MAIN world). Returns [{section, price, qty, unmapped}]
  // or {error}. Uses the page's own session; never logs in.
  const norm = (rows) => rows.filter((r) => r.price != null);
  if (platform === "tickpick") {
    return fetch(`https://api.tickpick.com/1.0/listings/internal/event-v2/${id}?trackView=false&paymentMethod=ACH`,
                 { credentials: "omit" })
      .then((r) => r.ok ? r.json() : Promise.reject(new Error("TickPick HTTP " + r.status)))
      .then((d) => norm((d.listings || []).filter((l) => !l.is_mine).map((l) => (
        { section: l.sid || "", price: l.p, qty: l.q, unmapped: !l.ms }))))
      .catch((e) => ({ error: String(e.message || e) }));
  }
  if (platform === "automatiq") {
    // the SPA keeps its bearer token in web storage; find a JWT-looking value
    const jwts = [];
    for (const st of [localStorage, sessionStorage]) {
      for (let i = 0; i < st.length; i++) {
        const v = st.getItem(st.key(i)) || "";
        for (const m of v.match(/eyJ[\w-]+\.[\w-]+\.[\w-]+/g) || []) jwts.push(m);
      }
    }
    const url = `https://b2b.automatiq.com/api/ecomm/events/${id}/listings?page%5Bsize%5D=99999&order_by=price` +
      `&order_by_direction=asc&filter%5Bzone%5D=no&filter%5Bmy_listing%5D=all&filter%5Bhas_venue_section_id%5D=all&filter%5Bmirror%5D=all`;
    const tryNext = (i) => {
      if (i >= jwts.length) return Promise.resolve({ error: "no Automatiq session token found on the page" });
      return fetch(url, { headers: { Authorization: "Bearer " + jwts[i], Accept: "application/vnd.api+json" } })
        .then((r) => r.ok ? r.json() : tryNext(i + 1))
        .then((d) => d && d.data ? norm(d.data.filter((x) => !(x.attributes || {}).my_listing).map((x) => {
          const a = x.attributes || {}, w = (a.pricing || {}).wholesale || {};
          const c = w.price_including_estimated_tax || w.price || a.price;
          return { section: a.section || "", price: c == null ? null : c / 100, qty: a.quantity,
                   unmapped: a.venue_section_id == null };
        })) : d);
    };
    return tryNext(0).catch((e) => ({ error: String(e.message || e) }));
  }
  return Promise.resolve({ error: "not read from this site" });
}

async function readSectionsFromPage() {
  if (!tab || page.platform === "cv") return null;   // CrowdVolt comes from the server's read
  try {
    const [res] = await chrome.scripting.executeScript({
      target: { tabId: tab.id }, world: "MAIN", func: pageReader, args: [page.platform, page.id] });
    const rows = res && res.result;
    if (!Array.isArray(rows)) return null;
    const by = new Map();
    for (const l of rows) {
      const k = l.section;
      if (!k) continue;
      const r = by.get(k) || { platform: page.platform, section: k, zone: zoneOf(k), min_price: null,
                               min_qty: null, listings: 0, unmapped: false, best_offer: null };
      r.listings++; r.unmapped = r.unmapped || l.unmapped;
      if (r.min_price == null || l.price < r.min_price) { r.min_price = l.price; r.min_qty = l.qty; }
      by.set(k, r);
    }
    return [...by.values()].sort((a, b) => a.min_price - b.min_price);
  } catch { return null; }
}

// --- rendering ---------------------------------------------------------------

function describeRule(r) {
  const secs = (r.sections || []).map((s) => (ZONES.find((z) => z[0] === s) || [s, s])[1]).join(", ") || "any section";
  const where = r.scope === "cv" ? "CrowdVolt" : "Automatiq + TickPick";
  const what = r.kind === "under" ? "listing at or under" : r.kind === "offer_over" ? "offer at or over"
    : "offer beats B2B by";
  return { where, what, secs };
}

async function saveRule(id, patch, el) {
  if (el) el.style.opacity = .5;
  try { await api("/api/snipe/rule", { id, ...patch }); await load(); refreshBadge(); }
  catch (e) { fail(e); if (el) el.style.opacity = 1; }
}

function renderRules() {
  const box = $("#rules"); box.innerHTML = "";
  const rules = (detail && detail.rules) || [];
  if (!rules.length) { box.innerHTML = `<div class="mut">none yet</div>`; return; }
  for (const r of rules) {
    const d = describeRule(r);
    const el = document.createElement("div");
    el.className = "rule" + (r.enabled ? "" : " off");
    el.innerHTML = `<div class="what"></div>
      <div class="ctl">
        <input type="number" class="sm" min="0" step="1" title="price">
        qty <input type="number" class="sm" min="1" step="1" title="min qty">
        <button class="ghost tog"></button>
        <button class="ghost del" title="delete">✕</button>
      </div>`;
    el.querySelector(".what").textContent = `${d.where}: ${d.what} · ${d.secs}` +
      (r.include_unmapped ? " · +unmapped" : "");
    const [pIn, qIn] = el.querySelectorAll("input");
    pIn.value = r.price; qIn.value = r.min_qty;
    pIn.onchange = () => { const v = parseFloat(pIn.value); if (v >= 0) saveRule(r.id, { price: v }, el); };
    qIn.onchange = () => { const v = parseInt(qIn.value, 10); if (v >= 1) saveRule(r.id, { min_qty: v }, el); };
    const tog = el.querySelector(".tog");
    tog.textContent = r.enabled ? "Pause" : "Resume";
    tog.onclick = () => saveRule(r.id, { enabled: !r.enabled }, el);
    el.querySelector(".del").onclick = async () => {
      el.style.opacity = .4;
      try { await api("/api/snipe/rule/delete", { id: r.id }); await load(); refreshBadge(); } catch (e) { fail(e); }
    };
    box.appendChild(el);
  }
}

function renderPicked() {
  const names = [...picked].map((s) => (ZONES.find((z) => z[0] === s) || [s, s])[1]);
  $("#pickedTxt").textContent = names.length ? names.join(", ") : "any (click zones or rows to narrow)";
}

function renderSections() {
  $("#zoneChips").innerHTML = "";
  for (const [val, label] of ZONES) {
    const c = document.createElement("span");
    c.className = "chip" + (picked.has(val) ? " on" : "");
    c.textContent = "≈ " + label;
    c.title = "matches every section named like this, on every site";
    c.onclick = () => { picked.has(val) ? picked.delete(val) : picked.add(val); renderSections(); };
    $("#zoneChips").appendChild(c);
  }
  const fromServer = (detail && detail.sections) || [];
  // this page's own read wins for its platform; the laptop's read fills the rest
  const rows = pageSecs
    ? [...pageSecs, ...fromServer.filter((r) => r.platform !== page.platform)]
    : fromServer;
  const src = [];
  if (pageSecs) src.push("this page, live");
  if (detail) {
    const ts = Object.entries(detail.platforms || {})
      .filter(([p]) => !pageSecs || p !== page.platform)
      .map(([p, s]) => `${PL[p]} ${ago(s.fetched_at)}`);
    if (ts.length) src.push(ts.join(", "));
  }
  $("#secSrc").textContent = src.join(" · ");
  const t = $("#secTable"); t.innerHTML = "";
  if (!rows.length) {
    t.innerHTML = `<tr><td class="mut">${detail ? "no listings read yet" : "add a rule (or zone) and the laptop reads this show within a minute"}</td></tr>`;
    renderPicked();
    return;
  }
  for (const r of rows.slice(0, 80)) {
    const tr = document.createElement("tr");
    tr.className = "sec" + (picked.has(r.section) ? " on" : "");
    tr.innerHTML = `<td><span class="pl"></span></td><td class="s"></td><td class="num p"></td><td class="num o"></td>`;
    tr.querySelector(".pl").textContent = PL[r.platform] || r.platform;
    tr.querySelector(".s").textContent = r.section + (r.unmapped ? " ·unmapped" : "");
    tr.querySelector(".p").textContent = r.min_price == null ? "" :
      `${money(r.min_price)}${r.min_qty ? " ×" + r.min_qty : ""}${r.listings > 1 ? ` (${r.listings})` : ""}`;
    tr.querySelector(".o").textContent = r.best_offer != null ? `offer ${money(r.best_offer)}` : "";
    tr.title = "click to add this exact section to the new rule";
    tr.onclick = () => { picked.has(r.section) ? picked.delete(r.section) : picked.add(r.section); renderSections(); };
    t.appendChild(tr);
  }
  renderPicked();
}

function render() {
  $("#msg").style.display = "none";
  $("#body").style.display = "";
  const label = { automatiq: "Automatiq", tickpick: "TickPick", cv: "CrowdVolt" }[page.platform];
  $("#site").textContent = `On ${label}` + (detail ? (detail.auto === "market" ? " · found in the CrowdVolt offer scan" : " · tracked") : " · not tracked yet");
  $("#evName").textContent = detail ? detail.name : page.name;
  $("#evMeta").textContent = [detail ? detail.event_date : page.date, detail && detail.venue].filter(Boolean).join(" · ") || "no date found";
  $("#evCtl").style.display = detail ? "" : "none";
  if (detail) {
    $("#pauseBtn").textContent = detail.paused ? "Resume event" : "Pause event";
    $("#pauseBtn").classList.toggle("on", !!detail.paused);
    $("#muteBtn").textContent = detail.muted ? "Unmute pings" : "Mute pings";
    $("#muteBtn").classList.toggle("on", !!detail.muted);
    const errs = Object.entries(detail.platforms || {}).filter(([, s]) => s.error && (!s.fetched_at || s.error_at > s.fetched_at));
    $("#evState").innerHTML = errs.length ? `<span class="warn"></span>` : "";
    if (errs.length) $("#evState .warn").textContent = errs.map(([p]) => PL[p]).join("/") + " read failing";
  }
  renderRules();
  renderSections();
  const isCv = page.platform === "cv";
  $("#kindRow").style.display = isCv ? "" : "none";
  if (!isCv) $("#kind").value = "under";
  $("#unmappedRow").style.display = isCv ? "none" : "";
  $("#scopeNote").textContent = isCv
    ? "CrowdVolt rules watch this CrowdVolt event's listings and offers."
    : "One rule covers Automatiq and TickPick. Prices are all-in per ticket.";
  const pings = (detail && detail.pings) || [];
  $("#pingsBox").style.display = pings.length ? "" : "none";
  $("#pings").innerHTML = "";
  for (const p of pings.slice(0, 3)) {
    const d = document.createElement("div"); d.className = "ping";
    d.textContent = `${ago(p.at)} · ${p.title}`;
    $("#pings").appendChild(d);
  }
}

async function load() {
  const { events } = await api("/api/snipe/index");
  const m = findMatch(events || [], page);
  detail = m ? await api("/api/snipe/event/detail?id=" + encodeURIComponent(m.id)) : null;
  render();
}

async function setEvent(patch) {
  try { await api("/api/snipe/event", { id: detail.id, ...patch }); await load(); refreshBadge(); }
  catch (e) { fail(e); }
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
  if (detail) {
    body.id = detail.id;
    if (detail.auto === "market") body.claim = true;
    if (!detail[page.refField]) body[page.refField] = page.ref;
  } else {
    Object.assign(body, { name: page.name, event_date: page.date, [page.refField]: page.ref });
  }
  $("#save").disabled = true; $("#saved").textContent = "saving…";
  try {
    await api("/api/snipe/event", body);
    $("#saved").innerHTML = `<span class="ok">saved</span>`;
    picked.clear(); $("#secCustom").value = ""; $("#price").value = "";
    await load();
    refreshBadge();
  } catch (e) { $("#saved").textContent = ""; fail(e); }
  $("#save").disabled = false;
}

async function main() {
  const cfg = await getCfg();
  $("#sBase").value = cfg.base; $("#sUser").value = cfg.user || ""; $("#sPass").value = cfg.pass || "";
  $("#gear").onclick = () => { const s = $("#settings"); s.style.display = s.style.display === "block" ? "none" : "block"; };
  $("#sSave").onclick = async () => {
    await chrome.storage.local.set({ base: $("#sBase").value.trim() || "https://kartis.homes",
                                     user: $("#sUser").value.trim(), pass: $("#sPass").value });
    $("#sMsg").textContent = "saved";
    if (page) load().catch(fail);
  };
  $("#save").onclick = save;
  $("#pauseBtn").onclick = () => setEvent({ paused: !detail.paused });
  $("#muteBtn").onclick = () => setEvent({ muted: !detail.muted });
  [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  page = tab && readPage(tab.url, tab.title);
  if (!page) {
    $("#msg").textContent = "Open an event page on Automatiq (B2B), TickPick or CrowdVolt, then click this again.";
    return;
  }
  try { await load(); } catch (e) { fail(e); return; }
  pageSecs = await readSectionsFromPage();
  if (pageSecs) renderSections();
}
main();
