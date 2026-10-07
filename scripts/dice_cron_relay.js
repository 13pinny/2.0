// Cloudflare Worker (Cron Trigger): the DICE relay without the PC.
//
// api.dice.fm refuses the VPS's hosting network. A Worker answering a
// request from the VPS doesn't help -- the forwarded subrequest carries the
// caller's address and DICE refuses it the same (tested 2026-10-07). A
// cron-triggered Worker has no caller, so this one does what dice_relay.py
// does on the PC: ask kartis.homes which DICE events are tracked, read each
// one's ticket_types, and POST them to /api/dice/relay.
//
// The free plan allows 50 subrequests per run, so each run reads at most
// BATCH events, rotating through the list by minute: with ~80 events and a
// every-minute cron, each event refreshes every 2 minutes.
//
// Setup: deploy with a cron trigger "* * * * *" and these secrets:
//   BASE_URL       https://kartis.homes
//   WEB_USER/PASS  the Caddy basic-auth pair (the "relay" user)
//   SECRET         the server's KARTIS_CVAUTH_SECRET
//   npx wrangler deploy dice_cron_relay.js --name kartis-dice-relay \
//       --compatibility-date 2026-10-01 --triggers "* * * * *"
const BATCH = 45;
const UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36";
const DICE_HEADERS = {
  "User-Agent": UA,
  "Accept": "application/json",
  "Accept-Language": "en-US,en;q=0.8",
  "X-Client-Platform": "web",
  "X-Api-Timestamp": "2024-04-15",
};

function kartisHeaders(env) {
  return {
    "Authorization": "Basic " + btoa(`${env.WEB_USER}:${env.WEB_PASS}`),
    "X-Kartis-Secret": env.SECRET,
    "Content-Type": "application/json",
  };
}

async function runOnce(env, minute) {
  const base = env.BASE_URL.replace(/\/$/, "");
  const t = await fetch(`${base}/api/dice/relay/targets`, { headers: kartisHeaders(env) });
  if (!t.ok) return { error: `targets HTTP ${t.status}` };
  const targets = ((await t.json()).targets || []).filter(c => /^[0-9a-f]{24}$/.test(c));
  const slots = Math.max(1, Math.ceil(targets.length / BATCH));
  const slot = minute % slots;
  const batch = targets.filter((_, i) => i % slots === slot);
  const payloads = {};
  const failed = {};
  await Promise.all(batch.map(async code => {
    try {
      const r = await fetch(`https://api.dice.fm/events/${code}/ticket_types`, { headers: DICE_HEADERS });
      if (r.ok) payloads[code] = await r.json();
      else failed[code] = r.status;
    } catch (e) {
      failed[code] = String(e);
    }
  }));
  let sent = null;
  if (Object.keys(payloads).length) {
    const p = await fetch(`${base}/api/dice/relay`, {
      method: "POST", headers: kartisHeaders(env), body: JSON.stringify({ payloads }),
    });
    sent = p.status;
  }
  return { targets: targets.length, batch: batch.length, ok: Object.keys(payloads).length, failed, post: sent };
}

export default {
  async scheduled(event, env, ctx) {
    const minute = Math.floor(event.scheduledTime / 60000);
    console.log(JSON.stringify(await runOnce(env, minute)));
  },
  // Manual check: GET /?key=<SECRET> runs one pass and returns the summary.
  async fetch(request, env) {
    const url = new URL(request.url);
    if (!env.SECRET || url.searchParams.get("key") !== env.SECRET) {
      return new Response("forbidden", { status: 403 });
    }
    const out = await runOnce(env, Math.floor(Date.now() / 60000));
    return new Response(JSON.stringify(out, null, 2), { headers: { "content-type": "application/json" } });
  },
};
