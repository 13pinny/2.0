"""Per-event Discord channel routing via a bot.

Drop pings can go to a dedicated channel per artist/event ("peer-tasi",
"ishay-ribo-amir-dadon") instead of the shared drops channel. Watchers carry
a `discord_channel` name; watchers for the same artist share the name (dates
are stripped from the auto-derived default), so all dates of a show land in
one channel.

Channels + webhooks are created lazily by the bot the first time a name is
used, then cached in the `discord_event_channels` table — one-time Discord
API cost per artist, plain webhook POSTs forever after.

Env (needed on every machine that sends drop pings — PC *and* VPS):
  DISCORD_BOT_TOKEN         — bot token; the bot must be in the guild with
                              Manage Channels + Manage Webhooks permissions.
  DISCORD_GUILD_ID          — the server to create channels in.
  DISCORD_DROPS_CATEGORY_ID — optional category to file new channels under.

If the bot isn't configured, or any Discord API call fails, webhook_for()
returns None and the caller falls back to the shared drops webhook — a bad
token can never lose a drop ping.

Smoke test: `python discord_bot.py <channel-name or watcher label>` prints
the resolved/created webhook URL.
"""

import json
import os
import re
import urllib.request
from datetime import datetime, timezone

import db

_API = "https://discord.com/api/v10"


def configured():
    return bool(os.environ.get("DISCORD_BOT_TOKEN", "").strip()
                and os.environ.get("DISCORD_GUILD_ID", "").strip())


# Date-ish tokens that watcher labels carry but channel names shouldn't:
# digits (dates, times, years), Hebrew month names, "all dates" suffixes.
_HEBREW_MONTHS = (
    "ינואר|פברואר|מרץ|אפריל|מאי|יוני|יולי|אוגוסט|ספטמבר|אוקטובר|נובמבר|דצמבר"
)
_STRIP_RE = re.compile(
    r"\d[\d./:\-]*|" + _HEBREW_MONTHS + r"|all dates|כל התאריכים",
    re.IGNORECASE,
)


def slugify_label(label):
    """Watcher label → Discord channel name, dates stripped.

    Takes only the first `·`-separated segment (artist name — the rest is
    venue + date), drops digit/month tokens, lowercases, and joins words
    with dashes. Discord allows Hebrew in channel names. Returns "" if
    nothing survives (caller should then skip per-event routing).
    """
    head = (label or "").split("·")[0]
    head = _STRIP_RE.sub(" ", head)
    words = re.findall(r"[\w֐-׿]+", head, re.UNICODE)
    return "-".join(w.lower() for w in words)[:90]


def _api(method, path, payload=None):
    token = os.environ["DISCORD_BOT_TOKEN"].strip()
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        _API + path, data=data, method=method,
        headers={
            "Authorization": f"Bot {token}",
            "Content-Type": "application/json",
            "User-Agent": "kartis (private, 1.0)",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def webhook_for(name):
    """Channel name → webhook URL, creating channel + webhook if needed.

    Returns None when unconfigured or on any API failure so drop pings fall
    back to the shared drops channel instead of being dropped.
    """
    name = (name or "").strip().lower()
    if not name:
        return None
    cached = db.discord_channel_get(name)
    if cached:
        return cached["webhook_url"]
    if not configured():
        return None
    guild = os.environ["DISCORD_GUILD_ID"].strip()
    try:
        # Reuse an existing channel with this name (e.g. made by hand, or by
        # the other machine before this one's cache row synced — the DB isn't
        # shared, so each machine may resolve once).
        chan = next(
            (c for c in _api("GET", f"/guilds/{guild}/channels")
             if c.get("type") == 0 and c.get("name") == name),
            None,
        )
        if chan is None:
            payload = {"name": name, "type": 0}
            cat = os.environ.get("DISCORD_DROPS_CATEGORY_ID", "").strip()
            if cat:
                payload["parent_id"] = cat
            chan = _api("POST", f"/guilds/{guild}/channels", payload)
        # Reuse the channel's existing Kartis webhook if one survives.
        hook = next(
            (h for h in _api("GET", f"/channels/{chan['id']}/webhooks")
             if h.get("name") == "Kartis" and h.get("token")),
            None,
        ) or _api("POST", f"/channels/{chan['id']}/webhooks", {"name": "Kartis"})
        url = f"https://discord.com/api/webhooks/{hook['id']}/{hook['token']}"
        db.discord_channel_put(name, str(chan["id"]), url,
                               datetime.now(timezone.utc).isoformat())
        return url
    except Exception as e:
        print(f"[discord_bot] webhook_for({name!r}) failed: {type(e).__name__}: {e}", flush=True)
        return None


if __name__ == "__main__":
    import sys
    arg = sys.argv[1] if len(sys.argv) > 1 else "kartis-test"
    slug = slugify_label(arg) if ("·" in arg or " " in arg) else arg.strip().lower()
    print(f"configured: {configured()}")
    print(f"channel name: {slug!r}")
    print(f"webhook: {webhook_for(slug)}")
