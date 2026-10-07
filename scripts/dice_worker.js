// Cloudflare Worker: forwards the server's DICE ticket-price calls to
// api.dice.fm, which refuses the VPS's hosting network but not Cloudflare's.
//
// Setup (Cloudflare dashboard → Workers & Pages → Create → Worker):
//   1. Paste this file as the worker's code and Deploy.
//   2. Settings → Variables and Secrets → add a Secret named KEY (any long
//      random string).
//   3. On the server's .env:
//        KARTIS_DICE_API_BASE=https://<worker-name>.<account>.workers.dev
//        KARTIS_DICE_API_KEY=<the same KEY>
//      then restart kartis-flask.
// Only GET /events/<24-hex id>/ticket_types is forwarded, and only with the
// key, so it can't be used as an open proxy.
const PATH = /^\/events\/[0-9a-f]{24}\/ticket_types$/;
const PASS = ["accept", "accept-language", "user-agent", "x-client-platform", "x-api-timestamp"];

export default {
  async fetch(request, env) {
    if (!env.KEY || request.headers.get("x-kartis-key") !== env.KEY) {
      return new Response("forbidden", { status: 403 });
    }
    const url = new URL(request.url);
    if (request.method !== "GET" || !PATH.test(url.pathname)) {
      return new Response("not found", { status: 404 });
    }
    const headers = new Headers();
    for (const k of PASS) {
      const v = request.headers.get(k);
      if (v) headers.set(k, v);
    }
    const r = await fetch("https://api.dice.fm" + url.pathname, { headers });
    return new Response(r.body, {
      status: r.status,
      headers: { "content-type": r.headers.get("content-type") || "application/json" },
    });
  },
};
