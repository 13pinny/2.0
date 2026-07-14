"""Discord + Gmail notifications for the Ticketmaster drop checker.

Reads credentials from the environment:
  DISCORD_WEBHOOK_URL  — main/fallback webhook from a Discord server's
                         integrations page. Every category lands here unless
                         it has its own webhook below.
  DISCORD_WEBHOOK_DROPS / DISCORD_WEBHOOK_STATUS / DISCORD_WEBHOOK_NEW_EVENTS
  / DISCORD_WEBHOOK_PRICER / DISCORD_WEBHOOK_LISTINGS / DISCORD_WEBHOOK_TODOS
                       — optional per-category webhooks (one Discord channel
                         each). Unset categories fall back to
                         DISCORD_WEBHOOK_URL, never to silence.
  GMAIL_USER           — Gmail address to send from
  GMAIL_APP_PASSWORD   — 16-char app password (NOT the regular Gmail password)
  NOTIFY_EMAIL_TO      — destination address (defaults to GMAIL_USER if unset)

Both channels are best-effort: a failure on one does not stop the other. The
caller gets a result dict it can persist to the drop-event log.

CLI smoke test: `python notify.py` pings the main webhook + email, then one
test ping per distinct per-category webhook so a channel-routing typo shows
up here instead of on the first real alert.
"""
import json
import os
import smtplib
import ssl
import urllib.request
import urllib.error
from email.message import EmailMessage


# Public base URL for links inside notifications (Discord embeds, emails).
# The app serves on :5000/:8000 locally but is reached at kartis.homes.
BASE_URL = (os.environ.get("KARTIS_BASE_URL") or "https://kartis.homes").rstrip("/")


# Discord channel routing: notification category → env var holding that
# channel's webhook. _discord_webhook() falls back to DISCORD_WEBHOOK_URL
# for any category without its own webhook.
_WEBHOOK_ENV = {
    "drops":      "DISCORD_WEBHOOK_DROPS",       # buyable-seat alerts (notify_drop)
    "status":     "DISCORD_WEBHOOK_STATUS",      # sold-out / last-tickets signals (notify_drop)
    "new_events": "DISCORD_WEBHOOK_NEW_EVENTS",  # pacha + kupat + TM-IL monitors
    "pricer":     "DISCORD_WEBHOOK_PRICER",      # auto-pricer changes/pauses/caps
    "listings":   "DISCORD_WEBHOOK_LISTINGS",    # intake→viagogo push pipeline
    "todos":      "DISCORD_WEBHOOK_TODOS",       # daily to-do digest
}


def _discord_webhook(category=None):
    """Webhook URL for a notification category ('' when nothing is set)."""
    var = _WEBHOOK_ENV.get(category)
    url = os.environ.get(var, "").strip() if var else ""
    return url or os.environ.get("DISCORD_WEBHOOK_URL", "").strip()


def _seat_block(seat):
    """Pick the block/section code from a seat dict, accepting both the
    normalized `block` key (kupat + new ticketmaster) and the raw `b` key
    (older ticketmaster fixtures). Returns '' if neither is set."""
    return str(seat.get("block") or seat.get("b") or "")


def _seat_row(seat):
    return str(seat.get("row") or seat.get("r") or "?")


def _seat_num(seat):
    v = seat.get("seat")
    if v is None:
        v = seat.get("l")
    return str(v) if v is not None else "?"


def _block_info(labels, code):
    """Return (name_or_code, price_or_none) for a block code, given a labels
    payload from a source's get_labels(). Falls back to the raw code when
    the block isn't in the cached map."""
    blocks = ((labels or {}).get("blocks")) or {}
    entry = blocks.get(code) or blocks.get(str(code)) or {}
    name = entry.get("name") or code
    return name, entry.get("price")


def _short(seat, labels=None):
    code = _seat_block(seat) or "?"
    name, _ = _block_info(labels, code)
    return f"{name} · row {_seat_row(seat)} · seat {_seat_num(seat)}"


def _festival_status_line(seat):
    """One-line status for a festival/hub watcher's status-encoded seat."""
    st = seat.get("status")
    qty = seat.get("qty_available")
    base = {
        "soldout": "❌ Sold out",
        "lasttickets": "⚠️ Last tickets remaining",
        "available": "🎟️ Available",
        "closed": "⛔ Sales closed",
    }.get(st, "🎟️ Available")
    if qty is not None and st != "soldout":
        base += f" — {qty} tickets left"
    return base


def _format_seat_lines(seats, limit=40, labels=None):
    """Group seats by block (showing block name once, then row/seat pairs).
    Hebrew block names render right-to-left in Discord/Gmail naturally;
    Unicode bidi handles it without any explicit marker.

    When any seat carries a `_perf` field (event-level watchers aggregate
    across performances), seats are first grouped by performance code, with
    a `— Perf NNN —` separator between groups.

    Festival/hub watchers carry a single status-encoded seat (no per-seat
    granularity), so render one status line instead of a seat list.
    """
    if seats and all(s.get("festival") or s.get("ga") for s in seats):
        return [_festival_status_line(seats[0])]
    perf_groups = _group_by_perf(seats)
    lines = []
    shown = 0
    multi_perf = perf_groups is not None and len(perf_groups) > 1
    iterable = perf_groups.items() if perf_groups else [(None, seats)]
    for perf_code, perf_seats in iterable:
        if multi_perf:
            lines.append(f"— Perf {perf_code} —")
        by_block = {}
        for s in perf_seats:
            by_block.setdefault(_seat_block(s) or "?", []).append(s)
        for code, group in sorted(by_block.items(), key=lambda kv: (-len(kv[1]), kv[0])):
            name, price = _block_info(labels, code)
            header_bits = [str(name)]
            if str(name) != str(code):
                header_bits.append(f"({code})")
            if price is not None:
                header_bits.append(f"— ₪{price:.0f}")
            if len(group) > 1:
                header_bits.append(f"× {len(group)}")
            lines.append(f"**{' '.join(header_bits)}**")
            for seat in sorted(group, key=lambda s: (_seat_row(s), _seat_num(s))):
                if shown >= limit:
                    lines.append(f"... +{len(seats) - shown} more")
                    return lines
                lines.append(f"• row {_seat_row(seat)}, seat {_seat_num(seat)}")
                shown += 1
    return lines


def _group_by_perf(seats):
    """If any seat has a `_perf` field, return an ordered dict of perf_code
    -> seat list, preserving first-seen perf order. Returns None otherwise so
    the caller can fall back to the flat (single-perf) layout."""
    if not any(s.get("_perf") for s in seats):
        return None
    groups = {}
    for s in seats:
        pc = s.get("_perf") or "?"
        groups.setdefault(pc, []).append(s)
    return groups


def _total_price(seats, labels):
    """Sum of public prices for the listed seats. Returns None if any seat
    has an unknown block (we don't want to under-report)."""
    if not labels:
        return None
    total = 0.0
    for s in seats:
        _, price = _block_info(labels, _seat_block(s))
        if price is None:
            return None
        total += price
    return total


def _post_discord(webhook_url, payload):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        webhook_url, data=data, method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "kartis-tm-watcher/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return f"ok ({resp.status})"
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:200]
        return f"http {e.code}: {body}"
    except urllib.error.URLError as e:
        return f"net error: {e.reason}"
    except Exception as e:
        return f"{type(e).__name__}: {e}"


def _send_email(subject, body, gmail_user, gmail_pwd, to_addr):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = gmail_user
    msg["To"] = to_addr
    msg.set_content(body)
    ctx = ssl.create_default_context()
    try:
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=15) as server:
            server.starttls(context=ctx)
            # Gmail rejects spaces in app passwords occasionally — strip just in case.
            server.login(gmail_user, gmail_pwd.replace(" ", ""))
            server.send_message(msg)
        return "ok"
    except smtplib.SMTPAuthenticationError as e:
        return f"auth failed: {e.smtp_code} {e.smtp_error.decode('utf-8','replace')[:120] if isinstance(e.smtp_error, bytes) else e.smtp_error}"
    except Exception as e:
        return f"{type(e).__name__}: {e}"


def notify_drop(label, perf_url, added_seats, removed_count=0, total_now=None, labels=None, channels=None, headline=None):
    """Send a notification for added seats. Returns {'discord': str, 'email': str}.

    `label` — user-facing watcher name (e.g. "אגם בוחבוט · אמפי קיסריה …").
    `perf_url` — public URL of the performance.
    `added_seats` — list of seat dicts (any source's fetch_selectable_seats).
    `labels` — labels payload from the source's get_labels(); when provided,
        block codes are translated to human names and prices are shown.
    `channels` — iterable of {'discord', 'email'}. None = both. Empty = neither
        (the function still returns a result dict so the caller can record it).
    `headline` — optional banner prepended to the Discord embed title and email
        subject. Used by event-level watchers to flag a sold-out → available
        transition (e.g. "🎟️ MSP03 — tickets just opened").

    Discord channel routing: an alert whose added seats are ALL status
    pseudo-seats in a non-buyable state (sold out / last tickets / sales
    closed) is a market signal, not a buy-now drop — it routes to the
    'status' category. Anything with real seats, or a status seat saying
    "available" (tickets just opened), is actionable and routes to 'drops'.
    """
    status_only = bool(added_seats) and all(
        (s.get("festival") or s.get("ga"))
        and (s.get("status") or "") in ("soldout", "lasttickets", "closed")
        for s in added_seats
    )
    discord_url = _discord_webhook("status" if status_only else "drops")
    gmail_user = os.environ.get("GMAIL_USER", "").strip()
    gmail_pwd = os.environ.get("GMAIL_APP_PASSWORD", "").strip()
    to_addr = (os.environ.get("NOTIFY_EMAIL_TO") or gmail_user).strip()
    if channels is None:
        channels = {"discord", "email"}
    else:
        channels = {c.strip().lower() for c in channels}

    n_added = len(added_seats)
    is_festival = bool(added_seats) and all(s.get("festival") or s.get("ga") for s in added_seats)
    seat_lines = _format_seat_lines(added_seats, labels=labels)
    seat_block = "\n".join(seat_lines)
    meta = ((labels or {}).get("meta")) or {}
    event_name = meta.get("eventName") or ""
    venue = meta.get("venueName") or ""
    # Prefer the source's venue-local time string (kupat/tickchak) so we
    # don't shift it through UTC; fall back to the epoch for ticketmaster.
    when = meta.get("firstPerfText") or ""
    if not when and meta.get("firstPerfMs"):
        try:
            from datetime import datetime, timezone
            d = datetime.fromtimestamp(meta["firstPerfMs"] / 1000, tz=timezone.utc)
            when = d.strftime("%Y-%m-%d %H:%M")
        except Exception:
            when = ""
    total_price = _total_price(added_seats, labels)

    discord_result = "skipped (channel disabled)" if "discord" not in channels else "skipped (no DISCORD_WEBHOOK_URL)"
    if "discord" in channels and discord_url:
        title_bits = []
        if headline:
            title_bits.append(headline)
        if not is_festival:
            title_bits.append(f"🎟️ {n_added} new seat{'s' if n_added != 1 else ''} dropped")
        if event_name and not (is_festival and headline and event_name in headline):
            title_bits.append(f"— {event_name}")
        embed = {
            "title": " ".join(title_bits)[:256],  # Discord embed title limit
            "description": seat_block[:4000] if seat_block else "(empty)",
            "url": perf_url,
            "color": 0x57F287,
            "fields": [
                {"name": "Watcher", "value": label or "(unnamed)", "inline": True},
            ],
        }
        if venue:
            embed["fields"].append({"name": "Venue", "value": venue, "inline": True})
        if when:
            embed["fields"].append({"name": "When", "value": when, "inline": True})
        if total_now is not None:
            embed["fields"].append({"name": "Total available", "value": str(total_now), "inline": True})
        if total_price is not None:
            embed["fields"].append({"name": "List price", "value": f"₪{total_price:,.0f}", "inline": True})
        if removed_count:
            embed["fields"].append({"name": "Also removed", "value": str(removed_count), "inline": True})
        discord_result = _post_discord(discord_url, {"embeds": [embed]})

    email_result = "skipped (channel disabled)" if "email" not in channels else "skipped (no GMAIL creds)"
    if "email" in channels and gmail_user and gmail_pwd and to_addr:
        subj_bits = ["[Kartis]"]
        if headline:
            subj_bits.append(headline)
        if not is_festival:
            subj_bits.append(f"{n_added} new seat{'s' if n_added != 1 else ''}")
        if event_name and not (is_festival and headline and event_name in headline):
            subj_bits.append(f"— {event_name}")
        subject = " ".join(subj_bits)[:200]
        # Festival: the status line is appended below as seat_block; don't
        # prepend the misleading "N new seats available" line.
        body_lines = [] if is_festival else [f"{n_added} new seat{'s' if n_added != 1 else ''} available."]
        if event_name:
            body_lines.append(f"Event: {event_name}")
        if venue:
            body_lines.append(f"Venue: {venue}")
        if when:
            body_lines.append(f"When:  {when}")
        body_lines.append(f"Watcher: {label}")
        body_lines.append(f"Buy: {perf_url}")
        body_lines.append("")
        body_lines.append(seat_block or "(empty)")
        body_lines.append("")
        if total_price is not None:
            body_lines.append(f"List price total: ₪{total_price:,.0f}")
        if total_now is not None:
            body_lines.append(f"Total selectable seats now: {total_now}")
        if removed_count:
            body_lines.append(f"Seats that disappeared since last check: {removed_count}")
        email_result = _send_email(subject, "\n".join(body_lines), gmail_user, gmail_pwd, to_addr)

    return {"discord": discord_result, "email": email_result}


def send_test(label="Kartis test"):
    """One-off test notification — used by the dashboard 'Test notify' button
    to confirm both channels are wired up before relying on them."""
    discord_url = _discord_webhook()
    gmail_user = os.environ.get("GMAIL_USER", "").strip()
    gmail_pwd = os.environ.get("GMAIL_APP_PASSWORD", "").strip()
    to_addr = (os.environ.get("NOTIFY_EMAIL_TO") or gmail_user).strip()

    discord_result = "skipped (no DISCORD_WEBHOOK_URL)"
    if discord_url:
        discord_result = _post_discord(discord_url, {
            "embeds": [{
                "title": "🟢 Kartis drop-checker test",
                "description": f"If you see this in Discord, the webhook is wired up correctly.\nLabel: **{label}**",
                "color": 0x5865F2,
            }]
        })

    email_result = "skipped (no GMAIL creds)"
    if gmail_user and gmail_pwd and to_addr:
        email_result = _send_email(
            f"[Kartis] test notification — {label}",
            f"This is a test from the Kartis drop checker.\nIf you got this, Gmail SMTP is wired up.\nLabel: {label}\n",
            gmail_user, gmail_pwd, to_addr,
        )

    return {"discord": discord_result, "email": email_result}


def send_todo_digest(due_today, overdue):
    """Daily reminder for the to-do page: one digest message per channel,
    not one per task. `due_today` and `overdue` are lists of todo dicts
    (from db.todo_due_open). Returns the same shape as notify_drop so the
    caller can log the result.
    """
    discord_url = _discord_webhook("todos")
    gmail_user = os.environ.get("GMAIL_USER", "").strip()
    gmail_pwd = os.environ.get("GMAIL_APP_PASSWORD", "").strip()
    to_addr = (os.environ.get("NOTIFY_EMAIL_TO") or gmail_user).strip()

    def _fmt_task(t):
        bits = [t.get("title") or "(untitled)"]
        urgency = (t.get("urgency") or "").upper()
        if urgency and urgency != "NORMAL":
            bits.append(f"[{urgency}]")
        return " ".join(bits)

    n_today = len(due_today)
    n_over = len(overdue)

    discord_result = "skipped (no DISCORD_WEBHOOK_URL)"
    if discord_url:
        desc_lines = []
        if overdue:
            desc_lines.append(f"**Overdue ({n_over}):**")
            for t in overdue[:20]:
                days_late = ""
                try:
                    from datetime import date, datetime
                    d = datetime.strptime((t.get("due_date_iso") or "")[:10], "%Y-%m-%d").date()
                    days_late = f" — {(date.today() - d).days}d late"
                except Exception:
                    pass
                desc_lines.append(f"• {_fmt_task(t)}{days_late}")
            if n_over > 20:
                desc_lines.append(f"... +{n_over - 20} more")
        if due_today:
            if desc_lines:
                desc_lines.append("")
            desc_lines.append(f"**Due today ({n_today}):**")
            for t in due_today[:20]:
                desc_lines.append(f"• {_fmt_task(t)}")
            if n_today > 20:
                desc_lines.append(f"... +{n_today - 20} more")
        embed = {
            "title": f"📋 {n_over + n_today} to-do reminder{'s' if (n_over + n_today) != 1 else ''}",
            "description": "\n".join(desc_lines)[:4000] or "(empty)",
            "url": BASE_URL + "/todos",
            "color": 0xF97316 if n_over else 0x58A6FF,
        }
        discord_result = _post_discord(discord_url, {"embeds": [embed]})

    email_result = "skipped (no GMAIL creds)"
    if gmail_user and gmail_pwd and to_addr:
        body_lines = [f"{n_over + n_today} to-do reminder(s):", ""]
        if overdue:
            body_lines.append(f"OVERDUE ({n_over}):")
            for t in overdue:
                body_lines.append(f"  • {_fmt_task(t)} — due {t.get('due_date_iso') or '?'}")
            body_lines.append("")
        if due_today:
            body_lines.append(f"DUE TODAY ({n_today}):")
            for t in due_today:
                body_lines.append(f"  • {_fmt_task(t)}")
            body_lines.append("")
        body_lines.append(f"Open the to-do page: {BASE_URL}/todos")
        subj_bits = ["[Kartis] To-Do reminders"]
        if n_over:
            subj_bits.append(f"— {n_over} overdue")
        if n_today:
            subj_bits.append(f"— {n_today} due today")
        email_result = _send_email(" ".join(subj_bits)[:200], "\n".join(body_lines),
                                   gmail_user, gmail_pwd, to_addr)

    return {"discord": discord_result, "email": email_result}


def notify_viagogo_match(push_row):
    """Notify that a Kupat purchase email has been matched (or not) to a
    viagogo event and priced, and is waiting on /listings for the user to
    approve before a draft listing gets created. Never creates anything
    itself — this is a heads-up, not an action.

    `push_row` is a viagogo_push DB row dict (db.viagogo_push_get /
    viagogo_push_insert's return value).
    """
    discord_url = _discord_webhook("listings")
    gmail_user = os.environ.get("GMAIL_USER", "").strip()
    gmail_pwd = os.environ.get("GMAIL_APP_PASSWORD", "").strip()
    to_addr = (os.environ.get("NOTIFY_EMAIL_TO") or gmail_user).strip()

    status = push_row.get("status") or ""
    no_match = status == "no_match"
    event_name = push_row.get("event_name") or "(unknown event)"
    venue = push_row.get("venue") or ""
    chosen_name = push_row.get("chosen_event_name") or ""
    chosen_venue = push_row.get("chosen_venue") or ""
    chosen_date = push_row.get("chosen_event_date") or ""
    qty = push_row.get("qty")
    cost_per_unit = push_row.get("cost_per_unit")
    website_price = push_row.get("website_price_usd")
    pending_url = BASE_URL + "/listings"

    title = "❓ Kupat ticket — no viagogo match found" if no_match else "🎟️ Kupat ticket matched on viagogo — needs approval"
    desc_lines = [f"**{event_name}**" + (f" — {venue}" if venue else "")]
    if not no_match and chosen_name:
        desc_lines.append(f"Matched: **{chosen_name}**" + (f" — {chosen_venue}" if chosen_venue else "") + (f" ({chosen_date})" if chosen_date else ""))
    if qty:
        desc_lines.append(f"Qty: {qty}")
    if cost_per_unit is not None:
        desc_lines.append(f"Kupat price: ₪{cost_per_unit:,.2f}/ticket")
    if website_price is not None:
        desc_lines.append(f"Proposed viagogo price: ${website_price:,.2f}/ticket (5x)")
    desc_lines.append("")
    desc_lines.append("Review and approve on the Listings page before any listing is created.")

    discord_result = "skipped (no DISCORD_WEBHOOK_URL)"
    if discord_url:
        embed = {
            "title": title,
            "description": "\n".join(desc_lines)[:4000],
            "url": pending_url,
            "color": 0xFAA61A if no_match else 0x5865F2,
        }
        discord_result = _post_discord(discord_url, {"embeds": [embed]})

    email_result = "skipped (no GMAIL creds)"
    if gmail_user and gmail_pwd and to_addr:
        subject = f"[Kartis] {title} — {event_name}"[:200]
        body = "\n".join(desc_lines) + f"\n\nOpen: {pending_url}\n"
        email_result = _send_email(subject, body, gmail_user, gmail_pwd, to_addr)

    return {"discord": discord_result, "email": email_result}


def notify_pricer_change(info):
    """Auto-pricer changed (or, dry-run, would change) a listing's price.
    Discord-only — these fire up to every 15 minutes and email would be
    noise. `info`: event_name, section, listing_id, old_price, new_price,
    competitor_price, floor, dry_run."""
    discord_url = _discord_webhook("pricer")
    if not discord_url:
        return {"discord": "skipped (no DISCORD_WEBHOOK_URL)"}

    dry = info.get("dry_run")
    title = ("🧪 [DRY RUN] Auto-pricer would reprice" if dry
             else "💸 Auto-pricer changed a listing price")
    lines = [
        f"**{info.get('event_name') or '(unknown event)'}** — "
        f"{(info.get('section') or '').splitlines()[0]}",
        f"${info.get('old_price'):,.2f} → **${info.get('new_price'):,.2f}**",
    ]
    if info.get("competitor_price") is not None:
        lines.append(f"Cheapest competitor: ${info['competitor_price']:,.2f}")
    if info.get("floor") is not None:
        lines.append(f"Floor: ${info['floor']:,.2f}")
    embed = {
        "title": title,
        "description": "\n".join(lines)[:4000],
        "url": "https://kartis.homes/pricer",
        "color": 0xFAA61A if dry else 0x56D364,
    }
    return {"discord": _post_discord(discord_url, {"embeds": [embed]})}


def _pacha_tier_lines(ev, limit=5):
    """'• General Access - 20th Release: 18 of 20 left — $185' per tier.
    Tiers without an available count are skipped."""
    out = []
    for t in (ev.get("tiers") or [])[:limit]:
        if t.get("available") is None:
            continue
        qty = f" of {t['quantity']}" if t.get("quantity") is not None else ""
        price = f" — ${t['price']:,.0f}" if t.get("price") is not None else ""
        out.append(f"• {t['name'] or 'Tickets'}: **{t['available']}**{qty} left{price}")
    return out


def notify_pacha_event(kind, ev, old=None):
    """Pacha NYC event-monitor ping. Discord-only — fires up to every 10
    minutes and speed matters more than a paper trail. `kind` is one of
    'new' (event just appeared on pacha-nyc.com/events), 'onsale'
    (waitlist/hidden-GA event became buyable), 'price_up' (headline GA
    price climbed a release), 'low_stock' (the GA release's tickets-left
    count crossed below the alert threshold — the price is about to jump).
    `ev` is a normalized dict from pacha_events.fetch_events(); `old` is
    the previously stored DB row (price_up uses its old price, low_stock
    its old count)."""
    discord_url = _discord_webhook("new_events")
    if not discord_url:
        return {"discord": "skipped (no DISCORD_WEBHOOK_URL)"}

    name = ev.get("name") or "(unknown event)"
    ga = ev.get("ga_price")
    vip = ev.get("vip_price")
    lines = [f"**{ev.get('date_text') or ev.get('start_date') or ''}**"]

    if kind == "onsale":
        title = f"🎟️ Pacha NYC: {name} is now ON SALE"
        color = 0x56D364
        if ga:
            lines.append(f"GA from **${ga:,.0f}**")
    elif kind == "price_up":
        old_price = (old or {}).get("ga_price")
        title = f"📈 Pacha NYC: {name} GA ${old_price:,.0f} → ${ga:,.0f}"
        color = 0xFAA61A
        lines.append("Selling through — price climbs with each release.")
    elif kind == "low_stock":
        left = ev.get("ga_available")
        release = ev.get("ga_release") or "current GA release"
        title = f"⏳ Pacha NYC: {name} — only {left} left in {release}"
        color = 0xED4245
        was = (old or {}).get("ga_available")
        if was is not None:
            lines.append(f"Was {was} earlier — the next release usually costs more.")
        else:
            lines.append("The next release usually costs more.")
    else:
        title = f"🪩 New Pacha NYC event — {name}"
        color = 0xE91E63
        if ga:
            lines.append(f"GA from **${ga:,.0f}**")
        if vip:
            lines.append(f"VIP from ${vip:,.0f}")
        if ev.get("iswaitlist"):
            lines.append("⏳ Waitlist only for now — not yet buyable.")

    lines.extend(_pacha_tier_lines(ev))
    # Customer-facing checkout is the event page's #tickets section — the
    # FourVenues iframe URL also sells but adds tax there.
    buy = f"{ev['page_url']}#tickets" if ev.get("page_url") else ev.get("buy_url")
    if buy:
        lines.append(f"[Buy tickets]({buy})")
    embed = {
        "title": title[:250],
        "description": "\n".join(lines)[:4000],
        "url": ev.get("page_url") or "https://pacha-nyc.com/events",
        "color": color,
    }
    return {"discord": _post_discord(discord_url, {"embeds": [embed]})}


_SITE_EVENT_LABELS = {"kupat": "Kupat", "tm": "Ticketmaster IL", "zappa": "Zappa",
                      "barby": "Barby"}


def notify_site_event(kind, ev, old=None):
    """Israeli-sites event-monitor ping (kupat.co.il / ticketmaster.co.il).
    Discord-only — fires up to every 10 minutes and speed matters more than
    a paper trail. `kind` is 'new' (event just appeared on the site's
    listing feed) or 'onsale' (a listed-but-not-selling event's sale
    opened; TM only — kupat has no pre-sale listing state). `ev` is a
    normalized dict from kupat_events/tm_events fetch_events(); `old` is
    the previously stored DB row (unused, kept for parity with
    notify_pacha_event)."""
    discord_url = _discord_webhook("new_events")
    if not discord_url:
        return {"discord": "skipped (no DISCORD_WEBHOOK_URL)"}

    label = _SITE_EVENT_LABELS.get(ev.get("source"), ev.get("source") or "?")
    name = ev.get("name") or "(unknown event)"
    lines = []
    if ev.get("date_text"):
        lines.append(f"**{ev['date_text']}**")
    if ev.get("venue"):
        lines.append(ev["venue"])
    if ev.get("price_text"):
        lines.append(f"From {ev['price_text']}")

    if kind == "onsale":
        title = f"🎟️ {label}: {name} is now ON SALE"
        color = 0x56D364
    else:
        title = f"🎫 New {label} event — {name}"
        color = 0xE91E63
        if ev.get("on_sale") is False:
            lines.append("⏳ Not on sale yet — listed only.")

    embed = {
        "title": title[:250],
        "description": "\n".join(lines)[:4000],
        "url": ev.get("url") or "",
        "color": color,
    }
    return {"discord": _post_discord(discord_url, {"embeds": [embed]})}


def notify_pricer_paused(info):
    """Auto-pricer paused a listing because its price on inv.viagogo differs
    from the last price the pricer set (i.e. a manual change). Discord +
    email — the pricer stays off for this listing until the user resumes it
    from the dashboard, so this one shouldn't be missed. `info`: event_name,
    section, listing_id, expected, seen."""
    discord_url = _discord_webhook("pricer")
    gmail_user = os.environ.get("GMAIL_USER", "").strip()
    gmail_pwd = os.environ.get("GMAIL_APP_PASSWORD", "").strip()
    to_addr = (os.environ.get("NOTIFY_EMAIL_TO") or gmail_user).strip()

    title = "⏸️ Auto-pricer paused — manual price change detected"
    lines = [
        f"**{info.get('event_name') or '(unknown event)'}** — "
        f"{(info.get('section') or '').splitlines()[0]}",
        f"Pricer last set: ${info.get('expected'):,.2f}",
        f"inv.viagogo now shows: ${info.get('seen'):,.2f}",
        "",
        "Auto-pricing is paused for this listing. Resume it from the "
        "dashboard when ready.",
    ]
    body = "\n".join(lines)

    discord_result = "skipped (no DISCORD_WEBHOOK_URL)"
    if discord_url:
        embed = {
            "title": title,
            "description": body[:4000],
            "url": "https://kartis.homes/pricer",
            "color": 0xFAA61A,
        }
        discord_result = _post_discord(discord_url, {"embeds": [embed]})

    email_result = "skipped (no GMAIL creds)"
    if gmail_user and gmail_pwd and to_addr:
        subject = f"[Kartis] {title} — {info.get('event_name') or ''}"[:200]
        email_result = _send_email(
            subject, body + "\n\nOpen: https://kartis.homes/pricer\n",
            gmail_user, gmail_pwd, to_addr,
        )

    return {"discord": discord_result, "email": email_result}


def notify_pricer_adopted(info):
    """Auto-pricer noticed an external price change (user's manual move on
    inv.viagogo) and adopted it as the new baseline — pricing continues,
    and a raise holds until a competitor undercuts it. Only sent for
    dollar-scale moves; viagogo's own cent-drift adopts silently.
    `info`: event_name, section, listing_id, expected, seen."""
    discord_url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not discord_url:
        return {"discord": "skipped (no DISCORD_WEBHOOK_URL)"}
    lines = [
        f"**{info.get('event_name') or '(unknown event)'}** — "
        f"{(info.get('section') or '').splitlines()[0]}",
        f"Pricer had set ${info.get('expected'):,.2f}; inv now shows "
        f"**${info.get('seen'):,.2f}** — adopted as the new baseline.",
        "",
        "Auto-pricing stays ON: this price holds while you're cheapest, "
        "and the pricer undercuts again the moment a competitor goes below it.",
    ]
    embed = {
        "title": "🔄 Auto-pricer adopted your price change",
        "description": "\n".join(lines)[:4000],
        "url": "https://kartis.homes/pricer",
        "color": 0x58A6FF,
    }
    return {"discord": _post_discord(discord_url, {"embeds": [embed]})}


def notify_pricer_rate_limited(info):
    """Auto-pricer hit the sliding-window drop cap on a listing (e.g. 15% in
    12h) and stopped lowering. Sent once per episode (not every tick).
    `info`: event_name, section, listing_id, current, wanted, baseline,
    pct, hours."""
    discord_url = _discord_webhook("pricer")
    if not discord_url:
        return {"discord": "skipped (no DISCORD_WEBHOOK_URL)"}
    lines = [
        f"**{info.get('event_name') or '(unknown event)'}** — "
        f"{(info.get('section') or '').splitlines()[0]}",
        f"Already down {info.get('pct'):g}% from ${info.get('baseline'):,.2f} "
        f"in the last {info.get('hours'):g}h — holding at ${info.get('current'):,.2f}.",
        f"Competitors would call for ${info.get('wanted'):,.2f}.",
        "",
        "Auto-lowering resumes as the window slides; take over manually "
        "from the Pricer page if you want to chase the market down.",
    ]
    embed = {
        "title": "🛑 Auto-pricer drop cap reached",
        "description": "\n".join(lines)[:4000],
        "url": "https://kartis.homes/pricer",
        "color": 0xF85149,
    }
    return {"discord": _post_discord(discord_url, {"embeds": [embed]})}


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    print(send_test("CLI smoke test"))
    # One test ping per *distinct* per-category webhook so a channel-routing
    # typo shows up here, not on the first real alert.
    _by_url = {}
    for _cat, _var in _WEBHOOK_ENV.items():
        _url = os.environ.get(_var, "").strip()
        if _url:
            _by_url.setdefault(_url, []).append(_cat)
    _main_url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not _by_url:
        print("no per-category webhooks configured — everything goes to the main channel")
    for _url, _cats in _by_url.items():
        if _url == _main_url:
            print(f"{'+'.join(_cats)}: same as main webhook")
            continue
        _res = _post_discord(_url, {"embeds": [{
            "title": f"🟢 Kartis channel test — {', '.join(_cats)}",
            "description": "This channel receives: **" + ", ".join(_cats) + "**",
            "color": 0x5865F2,
        }]})
        print(f"{'+'.join(_cats)}: {_res}")
