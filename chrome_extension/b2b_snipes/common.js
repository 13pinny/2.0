// Shared by the popup and the background worker: what show is this tab on,
// and the kartis.homes API.
const MONTHS = ["jan","feb","mar","apr","may","jun","jul","aug","sep","oct","nov","dec"];
const NOISE = new Set(("the and with presents present pres live at tickets ticket tour night show in of a an " +
  "b2b featuring feat ft miami florida new york nyc brooklyn la los angeles california texas chicago " +
  "illinois san diego francisco las vegas nevada day pass ga vip event").split(" "));

function tokens(t) {
  return ((t || "").toLowerCase().match(/[a-z0-9]+/g) || []).filter((w) => w.length > 1 && !NOISE.has(w));
}
function isoDate(y, m, d) {
  return `${y}-${String(m).padStart(2, "0")}-${String(d).padStart(2, "0")}`;
}
function nextOccurrence(m, d) {
  const now = new Date(); let y = now.getFullYear();
  if (new Date(y, m - 1, d) < new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1)) y++;
  return isoDate(y, m, d);
}
function titleCase(s) { return s.replace(/\b\w/g, (c) => c.toUpperCase()); }

// {platform, refField, ref, id, name, date} for an event page, else null.
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
             name: t ? t[1] : (title || "").split(" - ")[0], date: t ? isoDate(2000 + +t[4], +t[2], +t[3]) : null };
  }
  if (host.includes("tickpick.com")) {
    const m = u.pathname.match(/^\/buy-(.+?)-(\d{1,2})-(\d{1,2})-(\d{2})-[0-9apm]+\/(\d+)\/?$/);
    if (!m) return null;
    const name = m[1].split("-tickets-")[0].replace(/-/g, " ");
    return { platform: "tickpick", refField: "tickpick_ref", ref: `https://www.tickpick.com${u.pathname}`, id: m[5],
             name: titleCase(name), date: isoDate(2000 + +m[4], +m[2], +m[3]) };
  }
  if (host.includes("crowdvolt.com")) {
    const m = u.pathname.match(/\/events?\/([\w-]+)/);
    if (!m) return null;
    const slug = m[1], parts = slug.toLowerCase().split("-");
    let date = null; const yr = parts.find((p) => /^20\d\d$/.test(p));
    for (let i = 0; i < parts.length - 1; i++) {
      const mi = MONTHS.indexOf(parts[i].slice(0, 3));
      const d = parseInt(parts[i + 1], 10);
      if (mi >= 0 && d >= 1 && d <= 31) { date = yr ? isoDate(+yr, mi + 1, d) : nextOccurrence(mi + 1, d); break; }
    }
    const name = (title || "").split(/ [|–-] /)[0] || slug.replace(/-/g, " ");
    return { platform: "cv", refField: "cv_url", ref: `https://www.crowdvolt.com/event/${slug}`, id: slug,
             name, date };
  }
  return null;
}

// Same link first, else same date + the most shared name words.
function findMatch(events, page) {
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

async function getCfg() {
  return Object.assign({ base: "https://kartis.homes", user: "", pass: "" },
    await chrome.storage.local.get(["base", "user", "pass"]));
}

async function api(path, body) {
  const cfg = await getCfg();
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
