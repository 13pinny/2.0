"""Fingerprint the kupat.co.il homepage + catalog endpoints.

Run this when the new-event monitor stops pinging (a site redesign, an API
move) — it dumps everything needed to fix `kupat_events.py` without guessing:

  * www.kupat.co.il — HTTP status, final URL, server headers, size, and
    which front-end framework the HTML looks like (WordPress / Next.js /
    Nuxt / plain).
  * Which tile-extraction strategy in kupat_events currently finds shows,
    and how many, plus the raw markup around the first show link so the
    card structure is visible.
  * tickets.kupat.co.il/api/features and /api/presentations — status, row
    counts, and the key set of the first row (an API reshape shows up here
    as missing keys, not as an error).

Raw responses are saved under tm_cache/probe_kupat/ so they can be zipped
or pasted somewhere.

    python scripts/probe_kupat_site.py                  # summary
    python scripts/probe_kupat_site.py --dump           # + the first 8 KB of HTML
    python scripts/probe_kupat_site.py --file page.html # analyse a saved page
                                                        # (Ctrl+S from Chrome),
                                                        # no network needed
    python scripts/probe_kupat_site.py --selftest       # exercise both parse
                                                        # strategies on fixtures
"""
import json
import pathlib
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import kupat            # noqa: E402
import kupat_events     # noqa: E402

OUT_DIR = Path(__file__).resolve().parent.parent / "tm_cache" / "probe_kupat"

_FRAMEWORKS = [
    ("WordPress", (r"/wp-content/", r"/wp-includes/", r"wp-json")),
    ("Next.js", (r"__NEXT_DATA__", r"self\.__next_f\.push", r"/_next/static/")),
    ("Nuxt/Vue", (r"window\.__NUXT__", r"/_nuxt/")),
    ("Angular", (r"<app-root", r"ng-version=")),
    ("React (generic)", (r"data-reactroot", r"react-dom")),
    ("Elementor", (r"elementor-", )),
]


def _save(name, blob):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / name
    path.write_bytes(blob if isinstance(blob, bytes) else blob.encode("utf-8"))
    return path


def probe_homepage(dump=False, from_file=None):
    if from_file:
        print(f"=== HOMEPAGE  (saved file) {from_file}")
        raw = pathlib.Path(from_file).read_bytes()
        html = raw.decode("utf-8", errors="replace")
        print(f"  {len(raw):,} bytes")
    else:
        print(f"=== HOMEPAGE  {kupat_events.HOME_URL}")
        try:
            raw, meta = kupat_events.fetch_home_html()
        except Exception as e:
            print(f"  FETCH FAILED: {type(e).__name__}: {e}")
            return
        html = raw.decode("utf-8", errors="replace")
        print(f"  HTTP {meta['status']}  {len(raw):,} bytes  final={meta['url']}")
        for h in ("server", "x-powered-by", "content-type", "cf-cache-status"):
            if meta["headers"].get(h):
                print(f"  {h}: {meta['headers'][h]}")
        print(f"  saved: {_save('home.html', raw)}")

    hits = [name for name, pats in _FRAMEWORKS
            if any(re.search(p, html, re.IGNORECASE) for p in pats)]
    print(f"  looks like: {', '.join(hits) or 'plain HTML / unknown'}")

    cards = re.findall(r'<(?:article|div|li)[^>]+class="[^"]*item-show[^"]*"', html)
    links = kupat_events._SHOW_HREF_RE.findall(html)
    slugs = sorted({m[1] for m in links})
    print(f"  '.item-show' card elements: {len(cards)}")
    print(f"  /show/<slug> links: {len(links)} ({len(slugs)} distinct slugs)")
    for tag in ("__NEXT_DATA__", "self.__next_f.push", "application/ld+json", "wp-json"):
        n = html.count(tag)
        if n:
            print(f"  contains {tag!r} x{n}")

    for label, fn in (("cards", kupat_events._parse_tiles_structured),
                      ("links", kupat_events._parse_tiles_anchors)):
        try:
            tiles = fn(html)
        except Exception as e:
            print(f"  strategy {label}: RAISED {type(e).__name__}: {e}")
            continue
        with_img = sum(1 for t in tiles if t.get("image"))
        print(f"  strategy {label}: {len(tiles)} tiles ({with_img} with an image)")
        for t in tiles[:8]:
            print(f"      {t['slug']:<28.28} {t['name']:<34.34} {'img' if t['image'] else '---'}")

    # The markup around the first show link — this is what a parser update
    # has to key off, so print it verbatim.
    m = kupat_events._SHOW_HREF_RE.search(html)
    if m:
        start, end = max(0, m.start() - 1200), min(len(html), m.end() + 1200)
        print("\n  --- markup around the first /show/ link ---")
        print(re.sub(r"\n\s*\n+", "\n", html[start:end]))
        print("  --- end markup ---")
    if dump:
        print("\n  --- first 8 KB of HTML ---")
        print(html[:8192])


def probe_json(label, url, timeout=None):
    print(f"\n=== {label}  {url}")
    try:
        raw = kupat_events._get(url, timeout=timeout)
    except Exception as e:
        print(f"  FETCH FAILED: {type(e).__name__}: {e}")
        return
    print(f"  {len(raw):,} bytes   saved: {_save(label.lower() + '.json', raw)}")
    try:
        body = json.loads(raw)
    except json.JSONDecodeError:
        print(f"  NOT JSON — first 300 bytes: {raw[:300]!r}")
        return
    rows = body if isinstance(body, list) else (
        body.get("presentations") if isinstance(body, dict) else None)
    if isinstance(body, dict) and rows is None:
        print(f"  dict with keys: {sorted(body)[:20]}")
        return
    print(f"  {len(rows or [])} rows")
    if rows:
        print(f"  first row keys: {sorted(rows[0]) if isinstance(rows[0], dict) else type(rows[0])}")
        print(f"  first row: {json.dumps(rows[0], ensure_ascii=False)[:600]}")


# Fixtures for --selftest: the layout the 'cards' strategy was written for,
# and a generic redesign that only the 'links' fallback can read. Run this
# after editing either parser — the live site can't be relied on to still
# contain the edge cases (a percent-encoded Hebrew slug, a lazy-loaded
# banner, a bare nav link that must NOT become a show).
_FIXTURE_CARDS = """<html><body><section>
<article class="item-show show_artist-peertasi" aria-label="Peer Tasi">
  <a href="https://www.kupat.co.il/show/peer-tasi"><img class="item-image"
     src="https://www.kupat.co.il/wp-content/uploads/peer.jpg" alt="peer"></a>
  <h2 class="item-title"><span>Peer Tasi</span></h2></article>
<article class="item-show show_artist-rita" aria-label="Rita">
  <a href="https://www.kupat.co.il/show/rita"><img class="item-image"
     src="https://www.kupat.co.il/wp-content/uploads/rita.jpg" alt="rita"></a>
  <h2 class="item-title"><span>Rita</span></h2></article>
</section></body></html>"""

_FIXTURE_LINKS = """<html><body>
<nav><a href="/show/terms-page">About our shows</a></nav>
<main><div class="grid">
 <div class="card"><a href="/show/hanan-ben-ari" aria-label="Hanan Ben Ari">
   <img src="data:image/gif;base64,AA" data-src="/media/hanan.jpg" alt="H"/></a></div>
 <div class="card"><a href="/show/echoes"><img src="/media/echoes.jpg" alt="Echoes"></a>
   <a href="/show/echoes">ECHOES - PINK FLOYD THE WALL LIVE</a></div>
 <div class="card"><a href="https://2207.kupat.co.il/show/%D7%A9%D7%9C%D7%95%D7%9D">
   <img srcset="/media/shalom.jpg 1x" alt="Shalom"/></a><h3>Shalom Hanoch</h3></div>
</div></main>
<footer><a href="/show/contact-us">Contact</a></footer></body></html>"""


def selftest():
    """Assert both strategies still read their fixture. Returns an exit code."""
    ok = True

    def check(label, cond, detail=""):
        nonlocal ok
        ok = ok and cond
        print(f"  {'PASS' if cond else 'FAIL'}  {label}{'  ' + detail if detail and not cond else ''}")

    cards = kupat_events._parse_tiles_structured(_FIXTURE_CARDS)
    by = {t["slug"]: t for t in cards}
    check("cards: both tiles found", [t["slug"] for t in cards] == ["peer-tasi", "rita"], str(cards))
    check("cards: title read from .item-title",
          by.get("rita", {}).get("name") == "Rita")
    check("cards: banner read from .item-image",
          by.get("peer-tasi", {}).get("image", "").endswith("peer.jpg"))
    check("cards: artist class is a match key",
          "peertasi" in by.get("peer-tasi", {}).get("keys", set()))

    links = kupat_events._parse_tiles_anchors(_FIXTURE_LINKS)
    by = {t["slug"]: t for t in links}
    check("links: nav link is not given a banner",
          by.get("terms-page", {}).get("image") == "")
    check("links: footer link is not given a banner",
          by.get("contact-us", {}).get("image") == "")
    check("links: nav link keeps its own text, not a neighbour's",
          by.get("terms-page", {}).get("name") == "About our shows",
          repr(by.get("terms-page", {}).get("name")))
    check("links: lazy-loaded banner beats the data: placeholder",
          by.get("hanan-ben-ari", {}).get("image", "").endswith("/media/hanan.jpg"),
          by.get("hanan-ben-ari", {}).get("image", ""))
    check("links: aria-label wins as the name",
          by.get("hanan-ben-ari", {}).get("name") == "Hanan Ben Ari")
    check("links: duplicate anchors merge banner + longest name",
          by.get("echoes", {}).get("name") == "ECHOES - PINK FLOYD THE WALL LIVE"
          and by.get("echoes", {}).get("image", "").endswith("/media/echoes.jpg"))
    check("links: trailing <h3> title is picked up",
          by.get("%D7%A9%D7%9C%D7%95%D7%9D", {}).get("name") == "Shalom Hanoch")
    check("links: percent-encoded Hebrew slug decodes into a match key",
          kupat_events._norm("שלום") in by.get("%D7%A9%D7%9C%D7%95%D7%9D", {}).get("keys", set()))
    check("links: cross-subdomain href absolutised",
          by.get("%D7%A9%D7%9C%D7%95%D7%9D", {}).get("url", "").startswith("https://2207.kupat.co.il/"))

    check("cards strategy finds nothing on the redesigned fixture",
          kupat_events._parse_tiles_structured(_FIXTURE_LINKS) == [])
    print("\nselftest: " + ("all good" if ok else "FAILURES ABOVE"))
    return 0 if ok else 1


def main(argv):
    sys.stdout.reconfigure(encoding="utf-8")
    if "--selftest" in argv:
        return selftest()
    from_file = None
    if "--file" in argv:
        from_file = argv[argv.index("--file") + 1]
    probe_homepage(dump="--dump" in argv, from_file=from_file)
    if from_file:
        return 0
    probe_json("FEATURES", kupat_events.FEATURES_URL)
    probe_json("PRESENTATIONS", kupat_events.PRESENTATIONS_URL, timeout=120)
    print(f"\nRaw artifacts in {OUT_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
