"""DICE new-show watcher for followed ARTISTS and VENUES.

You follow a dice.fm artist or venue by pasting its page link (real
artist slugs carry an id suffix, e.g. /artist/adiel-6dxgq, so a typed name
only works when DICE happens to have a suffix-less page). Every
FETCH_MINUTES the page is fetched. Its Next.js __NEXT_DATA__ embeds the
upcoming shows (props.pageProps.initialProfile.sections[].items[].event:
id, name, status, price, venues[0], dates incl. sale_start_date), so one
page fetch per follow covers everything and NO api.dice.fm call is made --
which matters because api.dice.fm 403s the VPS's (Hetzner) IP whatever
the headers, while dice.fm pages still load from it (verified 2026-10-06).
A page without that embedded data falls back to collecting /event/<slug>
links and reading each new one through the ticket_types API, which only
works from a machine DICE doesn't block.

Pings (one Discord channel per followed artist/venue, `dice-<name>`, made
by the bot on first use; falls back to the shared new-events channel):
  new       a show we had never seen on the page
  reminder  ~1 hour before tickets go on sale
  live      at the sale-start minute (or when a show with no announced
            time flips to on-sale)
The first fetch of a newly followed page is a SILENT baseline -- its whole
listing is backfill, not news -- but sale reminders and go-live pings
still fire for its shows that aren't on sale yet.

Every upcoming show found is also added to the /dice price tracker
(market_manual, source dice), so its ladder starts recording on its own.

Guards against the page listing OTHER artists' shows (recommendations):
embedded shows are read from the "upcoming" section only when the page
labels one; on the API fallback an artist's show must mention the
artist's name somewhere in its payload. A venue's show must be AT that
venue either way. A show that fails the check is remembered as `foreign`.

CLI:
    python dice_follow.py --probe "<dice.fm artist/venue URL or name>"
        fetch and print what would be found, writing nothing
    python dice_follow.py            one tick (fetch due follows + pings)
"""
import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import db
import dice

FETCH_MINUTES = 10
REMINDER_MINUTES = 60
DETAIL_REFRESH_MINUTES = 30      # re-read a not-yet-on-sale show this often
LIVE_GRACE_HOURS = 6             # don't fire a go-live ping for an old sale
MAX_NEW_DETAILS_PER_FOLLOW = 40  # detail fetches per follow per tick

_EVENT_HREF_RE = re.compile(r'(?:dice\.fm)?/event/([a-z0-9][a-z0-9-]{3,200})')
_OG_TITLE_RE = re.compile(r'<meta[^>]+property="og:title"[^>]+content="([^"]+)"')
_TITLE_RE = re.compile(r"<title[^>]*>([^<]+)</title>", re.I)


class FollowError(RuntimeError):
    pass


def _norm(s):
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def _slugify(s):
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")


def _now():
    return datetime.now(timezone.utc)


# --- resolving what to follow ----------------------------------------------

def parse_target(text):
    """'https://dice.fm/artist/x' / 'dice.fm/venue/y' -> (kind, slug).
    Returns None for a plain name."""
    m = re.search(r"dice\.fm/(?:[a-z]{2}(?:-[a-z]{2})?/)?(artist|venue)s?/([A-Za-z0-9_.-]+)", text or "")
    if m:
        return m.group(1), m.group(2).lower()
    return None


def _page_url(kind, slug):
    return f"{dice.SITE_BASE}/{kind}/{slug}"


def _page_title(html):
    m = _OG_TITLE_RE.search(html) or _TITLE_RE.search(html)
    if not m:
        return None
    t = m.group(1).replace("&amp;", "&").replace("&#x27;", "'").replace("&#39;", "'")
    # "Sullivan King Tickets | DICE", "Knockdown Center events | DICE"
    t = re.split(r"\s+[|–-]\s+DICE", t)[0]
    t = re.sub(r"\s+(tickets|events|tour dates)(\s.*)?$", "", t, flags=re.I)
    return t.strip() or None


def _fetch_page(kind, slug):
    html = dice._http_get(_page_url(kind, slug), accept="text/html,application/xhtml+xml")
    if "/event/" not in html and not _page_title(html):
        raise FollowError(f"dice.fm/{kind}/{slug} doesn't look like a DICE {kind} page")
    return html


def resolve(text):
    """Text from the /dice box -> {kind, slug, name, url}. A link is taken
    as-is; a plain name is tried as an artist page, then a venue page."""
    text = (text or "").strip()
    if not text:
        raise FollowError("enter a dice.fm artist/venue link or a name")
    hit = parse_target(text)
    tries = [hit] if hit else [("artist", _slugify(text)), ("venue", _slugify(text))]
    last = None
    for kind, slug in tries:
        if not slug:
            continue
        try:
            html = _fetch_page(kind, slug)
        except Exception as e:
            last = e
            continue
        return {"kind": kind, "slug": slug,
                "name": _profile_name(html) or _page_title(html) or text,
                "url": _page_url(kind, slug)}
    if hit:
        raise FollowError(f"couldn't open {text}: {last}")
    raise FollowError(f"no DICE page found for \"{text}\" — DICE artist links carry an id "
                      "(e.g. dice.fm/artist/adiel-6dxgq), so paste the link from the artist's page")


# --- reading a followed page -----------------------------------------------

def event_slugs(html):
    seen = []
    for s in _EVENT_HREF_RE.findall(html or ""):
        s = s.lower().rstrip("-")
        if s not in seen:
            seen.append(s)
    return seen


_NEXT_DATA_RE = re.compile(
    r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)


def _next_data(html):
    m = _NEXT_DATA_RE.search(html or "")
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except ValueError:
        return None


def _profile(html):
    nd = _next_data(html) or {}
    prof = ((nd.get("props") or {}).get("pageProps") or {}).get("initialProfile")
    if isinstance(prof, str):
        try:
            prof = json.loads(prof)
        except ValueError:
            prof = None
    return prof if isinstance(prof, dict) else None


def _image_of(ev):
    imgs = ev.get("images")
    if isinstance(imgs, dict):
        for k in ("square", "portrait", "landscape", "brand"):
            if isinstance(imgs.get(k), str) and imgs[k]:
                return imgs[k].split("?", 1)[0]
    for k in ("image_url", "image"):
        if isinstance(ev.get(k), str) and ev[k].startswith("http"):
            return ev[k].split("?", 1)[0]
    return None


def _status_of(raw, sale_start):
    raw = str(raw or "").lower()
    if raw == "on-sale":
        return "onsale"
    sale = _parse_dt(sale_start)
    if sale and sale > _now():
        return "upcoming"
    if raw in ("sold-out", "off-sale", "locked"):
        return "soldout"
    return "unknown"


def _from_embedded(e):
    """One __NEXT_DATA__ profile event -> the same dict event_details
    returns. The 24-hex id doubles as the stored slug."""
    dates = e.get("dates") if isinstance(e.get("dates"), dict) else {}
    venues = e.get("venues") or []
    ven = venues[0] if venues and isinstance(venues[0], dict) else {}
    city = ven.get("city")
    city = (city.get("name") if isinstance(city, dict) else city) or ""
    eid = str(e.get("id") or "").lower()
    perm = e.get("perm_name")
    sale_start = dates.get("sale_start_date")
    return {
        "slug": eid,
        "dice_id": eid if re.fullmatch(r"[0-9a-f]{24}", eid) else None,
        "name": (e.get("name") or "").strip() or eid,
        "event_start": dates.get("event_start_date"),
        "sale_start": sale_start,
        "venue": (ven.get("name") or "").strip(),
        "city": str(city).strip(),
        "status": _status_of(e.get("status"), sale_start),
        "url": f"{dice.SITE_BASE}/event/{perm or eid}",
        "image": _image_of(e),
        "_embedded": True,
    }


def page_events(html):
    """Shows embedded in an artist/venue page, or None when the page has no
    such data (then the caller falls back to links + the API). Only the
    section(s) titled 'upcoming' are read when any is labelled so; else
    every section."""
    prof = _profile(html)
    if not prof or not isinstance(prof.get("sections"), list):
        return None
    secs = [x for x in prof["sections"] if isinstance(x, dict)]
    def label(x):
        return " ".join(str(x.get(k) or "") for k in ("title", "type", "name", "id")).lower()
    upcoming = [x for x in secs if "upcoming" in label(x)]
    out, seen = [], set()
    for sec in (upcoming or secs):
        for it in sec.get("items") or []:
            e = it.get("event") if isinstance(it, dict) else None
            if not isinstance(e, dict) or not e.get("id"):
                continue
            ev = _from_embedded(e)
            if ev["slug"] not in seen:
                seen.add(ev["slug"])
                out.append(ev)
    return out


def _profile_name(html):
    prof = _profile(html) or {}
    for k in ("name", "title", "display_name"):
        if isinstance(prof.get(k), str) and prof[k].strip():
            return prof[k].strip()
    return None


def event_details(slug):
    """One show's facts from the ticket_types API (via the event page for
    its id). Raises on any failure so the caller can retry later."""
    internal_id = dice._resolve_internal_id(slug)
    data = dice._fetch_ticket_types(internal_id)
    venues = data.get("venues") or []
    ven = venues[0] if venues and isinstance(venues[0], dict) else {}
    city = ven.get("city") if isinstance(ven.get("city"), dict) else {}
    dates = data.get("dates") or {}
    statuses = {str(t.get("status") or "").lower()
                for t in (data.get("ticket_types") or []) if isinstance(t, dict)}
    if "on-sale" in statuses:
        status = "onsale"
    elif statuses or data.get("is_fully_locked"):
        status = "soldout"
    elif dice._sale_not_started(dates):
        status = "upcoming"
    else:
        status = "unknown"
    return {
        "slug": slug,
        "dice_id": internal_id,
        "name": (data.get("name") or "").strip() or slug,
        "event_start": dates.get("event_start_date"),
        "sale_start": dates.get("sale_start_date"),
        "venue": (ven.get("name") or "").strip(),
        "city": (city.get("name") or "").strip(),
        "status": status,
        "url": f"{dice.SITE_BASE}/event/{slug}",
        "_text": _norm(json.dumps(data, ensure_ascii=False)),
    }


def _belongs(follow, ev):
    """Is this show really the followed artist's / at the followed venue?"""
    name = _norm(follow.get("name"))
    if not name:
        return True
    if follow["kind"] == "venue":
        v = _norm(ev.get("venue"))
        return bool(v) and (name in v or v in name)
    if ev.get("_embedded"):
        return True   # listed on the artist's own profile
    return f" {name} " in f" {ev.get('_text', '')} "


def _parse_dt(v):
    if not v:
        return None
    try:
        dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _is_past(ev):
    start = _parse_dt(ev.get("event_start"))
    return bool(start) and start < _now() - timedelta(hours=12)


# --- Discord ---------------------------------------------------------------

def _channel_name(follow):
    return ("dice-" + _slugify(follow.get("name") or follow["slug"]))[:90]


def _webhook(follow):
    import notify
    try:
        import discord_bot
        url = discord_bot.webhook_for(_channel_name(follow))
        if url:
            return url
    except Exception as e:
        print(f"[dice-follow] channel for {follow.get('name')!r} failed: {e}")
    return notify._new_events_webhook(None)


def _ts(dt, style="F"):
    return f"<t:{int(dt.timestamp())}:{style}>"


def _ping(follow, kind, ev):
    import notify
    if db.setting_get_bool("master_muted", default=False):
        return "skipped (master muted)"
    url = _webhook(follow)
    if not url:
        return "skipped (no webhook)"
    who = follow.get("name") or follow["slug"]
    start = _parse_dt(ev.get("event_start"))
    sale = _parse_dt(ev.get("sale_start"))
    where = ", ".join(x for x in (ev.get("venue"), ev.get("city")) if x)
    lines = []
    if start:
        lines.append(f"**{start.strftime('%a %b %-d, %Y · %-I:%M %p')}**" + (f" · {where}" if where else ""))
    elif where:
        lines.append(f"**{where}**")
    if kind == "new":
        title = (f"🆕 {ev['name']} just announced" if _norm(who) in _norm(ev["name"])
                 else f"🆕 {who}: {ev['name']} just announced")
        color = 0xE91E63
        if ev.get("status") == "onsale":
            lines.append("Tickets are **on sale now**.")
        elif sale and sale > _now():
            lines.append(f"Tickets on sale {_ts(sale)} ({_ts(sale, 'R')}).")
        elif ev.get("status") == "soldout":
            lines.append("Already sold out / waitlist only.")
    elif kind == "reminder":
        title = f"⏰ {ev['name']} goes on sale in 1 hour"
        color = 0xFAA61A
        if sale:
            lines.append(f"Sale opens {_ts(sale, 't')} ({_ts(sale, 'R')}).")
    else:
        title = f"🟢 {ev['name']} is ON SALE now"
        color = 0x56D364
    lines.append(f"[Open on DICE]({ev['url']})")
    embed = {"title": title[:250], "description": "\n".join(lines)[:4000],
             "color": color, "url": ev["url"]}
    if ev.get("image"):
        embed["thumbnail"] = {"url": ev["image"] + "?w=300&h=300&fit=crop"}
    payload = {"embeds": [embed], "content": title[:1990]}
    return notify._post_discord(url, payload)


# --- the tick --------------------------------------------------------------

def _track(ev, now_iso):
    """Add a show to the /dice price tracker (idempotent)."""
    try:
        db.market_manual_add("dice", "event", ev["dice_id"], ev["url"], now_iso)
    except Exception as e:
        print(f"[dice-follow] couldn't track {ev.get('slug')}: {e}")


def _record_new(follow, ev, now_iso, baseline, notify_new, pings):
    foreign = not _belongs(follow, ev)
    past = _is_past(ev)
    sale = _parse_dt(ev.get("sale_start"))
    already_live = ev["status"] in ("onsale", "soldout") or bool(sale and sale <= _now())
    db.dice_follow_event_put(follow["id"], ev, now_iso, foreign=foreign or past,
                             live_sent=already_live)
    if foreign or past:
        return
    if ev.get("dice_id"):
        _track(ev, now_iso)
    if notify_new and not baseline:
        pings.append(("new", ev))
        if sale and sale - _now() <= timedelta(minutes=REMINDER_MINUTES):
            db.dice_follow_event_mark(follow["id"], ev["slug"], reminder=True)


def check_follow(follow, now_iso, notify_new=True):
    """Fetch one followed page, record new shows and refresh known ones.
    Returns (pings [(kind, ev)], embedded) -- embedded=False means the page
    carried no show data and refresh_pending must re-read shows through the
    API. Sale-time pings are handled separately in due_pings."""
    html = _fetch_page(follow["kind"], follow["slug"])
    known = db.dice_follow_events(follow["id"])
    baseline = not follow.get("baselined")
    pings = []
    embedded = page_events(html)
    if embedded is not None:
        for ev in embedded:
            row = known.get(ev["slug"])
            if row is None:
                _record_new(follow, ev, now_iso, baseline, notify_new, pings)
                continue
            if row["foreign"]:
                continue
            # Known show: keep its facts current (a moved sale time, or a
            # flip to on-sale with no announced time -> go-live ping).
            db.dice_follow_event_put(follow["id"], ev, now_iso, update=True)
            if (not row["live_sent"] and ev["status"] == "onsale"
                    and not _parse_dt(ev.get("sale_start"))):
                pings.append(("live", ev))
    else:
        fetched = 0
        for slug in event_slugs(html):
            if slug in known:
                continue
            if fetched >= MAX_NEW_DETAILS_PER_FOLLOW:
                break  # the rest next tick
            fetched += 1
            try:
                ev = event_details(slug)
            except Exception as e:
                print(f"[dice-follow] {slug}: {e}")
                continue  # not stored, so retried next tick
            info = dice.page_info_many([slug]).get(slug) or {}
            ev["image"] = info.get("image")
            _record_new(follow, ev, now_iso, baseline, notify_new, pings)
    db.dice_follow_checked(follow["id"], now_iso, error=None,
                           name=_profile_name(html) or _page_title(html), baselined=True)
    return pings, embedded is not None


def refresh_pending(follow, now_iso):
    """Re-read shows that aren't on sale yet so a moved or newly announced
    sale time is picked up. Returns go-live pings for shows that flipped to
    on-sale without a known sale time."""
    pings = []
    cutoff = _now() - timedelta(minutes=DETAIL_REFRESH_MINUTES)
    for row in db.dice_follow_events(follow["id"]).values():
        if row["foreign"] or row["live_sent"]:
            continue
        checked = _parse_dt(row.get("checked_at"))
        if checked and checked > cutoff:
            continue
        try:
            ev = event_details(row["slug"])
        except Exception:
            continue
        ev["image"] = row.get("image")
        db.dice_follow_event_put(follow["id"], ev, now_iso, update=True)
        if ev["status"] == "onsale" and not _parse_dt(ev.get("sale_start")):
            pings.append(("live", ev))
    return pings


def due_pings(now=None):
    """Time-based reminders and go-live pings across every follow."""
    now = now or _now()
    out = []
    for follow in db.dice_follows_all():
        if follow.get("paused"):
            continue
        for row in db.dice_follow_events(follow["id"]).values():
            if row["foreign"]:
                continue
            sale = _parse_dt(row.get("sale_start"))
            if not sale:
                continue
            if not row["live_sent"] and sale <= now and now - sale <= timedelta(hours=LIVE_GRACE_HOURS):
                out.append((follow, "live", row))
            elif (not row["reminder_sent"] and not row["live_sent"]
                  and timedelta(0) < sale - now <= timedelta(minutes=REMINDER_MINUTES)):
                out.append((follow, "reminder", row))
    return out


def run_tick(force=False):
    """Fetch every follow that's due, then send new / reminder / live pings.
    Returns a summary dict for /api status."""
    now_iso = _now().isoformat()
    summary = {"checked": 0, "new": 0, "reminders": 0, "live": 0, "errors": 0}
    for follow in db.dice_follows_all():
        if follow.get("paused"):
            continue
        last = _parse_dt(follow.get("last_checked_at"))
        if not force and last and _now() - last < timedelta(minutes=FETCH_MINUTES):
            continue
        summary["checked"] += 1
        try:
            pings, embedded = check_follow(follow, now_iso)
            if not embedded:
                pings += refresh_pending(follow, now_iso)
        except Exception as e:
            summary["errors"] += 1
            db.dice_follow_checked(follow["id"], now_iso, error=f"{type(e).__name__}: {e}")
            continue
        for kind, ev in pings:
            _ping(follow, kind, ev)
            if kind == "live":
                db.dice_follow_event_mark(follow["id"], ev["slug"], live=True)
            summary["new" if kind == "new" else kind] += 1
    for follow, kind, row in due_pings():
        _ping(follow, kind, row)
        db.dice_follow_event_mark(follow["id"], row["slug"],
                                  reminder=kind == "reminder", live=kind == "live")
        summary["reminders" if kind == "reminder" else "live"] += 1
    return summary


def probe(text):
    t = resolve(text)
    html = _fetch_page(t["kind"], t["slug"])
    follow = {"kind": t["kind"], "name": t["name"], "slug": t["slug"]}
    embedded = page_events(html)
    if embedded is not None:
        print(f"{t['kind']}: {t['name']}  ({t['url']})  — {len(embedded)} shows embedded in the page")
        for ev in embedded:
            flag = "" if _belongs(follow, ev) else "  [ignored: not this " + t["kind"] + "]"
            if _is_past(ev):
                flag += "  [past]"
            print(f"  {ev['event_start'] or '?':25} {ev['name'][:50]:50} {ev['venue']}, {ev['city']}"
                  f"  status={ev['status']} sale={ev['sale_start']}{flag}")
        return
    slugs = event_slugs(html)
    print(f"{t['kind']}: {t['name']}  ({t['url']})  — no embedded shows; {len(slugs)} event links (API path)")
    for s in slugs[:25]:
        try:
            ev = event_details(s)
        except Exception as e:
            print(f"  ! {s}: {e}")
            continue
        flag = "" if _belongs(follow, ev) else "  [ignored: not this " + t["kind"] + "]"
        if _is_past(ev):
            flag += "  [past]"
        print(f"  {ev['event_start'] or '?':25} {ev['name'][:50]:50} {ev['venue']}, {ev['city']}"
              f"  status={ev['status']} sale={ev['sale_start']}{flag}")


if __name__ == "__main__":
    db.init()
    if len(sys.argv) > 2 and sys.argv[1] == "--probe":
        probe(" ".join(sys.argv[2:]))
    else:
        print(run_tick(force="--force" in sys.argv))
