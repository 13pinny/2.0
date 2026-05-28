"""Discord + Gmail notifications for the Ticketmaster drop checker.

Reads credentials from the environment:
  DISCORD_WEBHOOK_URL  — full webhook URL from a Discord server's integrations page
  GMAIL_USER           — Gmail address to send from
  GMAIL_APP_PASSWORD   — 16-char app password (NOT the regular Gmail password)
  NOTIFY_EMAIL_TO      — destination address (defaults to GMAIL_USER if unset)

Both channels are best-effort: a failure on one does not stop the other. The
caller gets a result dict it can persist to the drop-event log.
"""
import json
import os
import smtplib
import ssl
import urllib.request
import urllib.error
from email.message import EmailMessage


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


def _format_seat_lines(seats, limit=40, labels=None):
    """Group seats by block (showing block name once, then row/seat pairs).
    Hebrew block names render right-to-left in Discord/Gmail naturally;
    Unicode bidi handles it without any explicit marker.

    When any seat carries a `_perf` field (event-level watchers aggregate
    across performances), seats are first grouped by performance code, with
    a `— Perf NNN —` separator between groups.
    """
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
    """
    discord_url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    gmail_user = os.environ.get("GMAIL_USER", "").strip()
    gmail_pwd = os.environ.get("GMAIL_APP_PASSWORD", "").strip()
    to_addr = (os.environ.get("NOTIFY_EMAIL_TO") or gmail_user).strip()
    if channels is None:
        channels = {"discord", "email"}
    else:
        channels = {c.strip().lower() for c in channels}

    n_added = len(added_seats)
    seat_lines = _format_seat_lines(added_seats, labels=labels)
    seat_block = "\n".join(seat_lines)
    meta = ((labels or {}).get("meta")) or {}
    event_name = meta.get("eventName") or ""
    venue = meta.get("venueName") or ""
    when = ""
    if meta.get("firstPerfMs"):
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
        title_bits.append(f"🎟️ {n_added} new seat{'s' if n_added != 1 else ''} dropped")
        if event_name:
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
        subj_bits = []
        if headline:
            subj_bits.append(headline)
        subj_bits.append(f"[Kartis] {n_added} new seat{'s' if n_added != 1 else ''}")
        if event_name:
            subj_bits.append(f"— {event_name}")
        subject = " ".join(subj_bits)[:200]
        body_lines = [f"{n_added} new seat{'s' if n_added != 1 else ''} available."]
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
    discord_url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
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


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    print(send_test("CLI smoke test"))
