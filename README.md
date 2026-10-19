# Tao Group price watch

Checks ticket prices for Tao Group events (Marquee, OMNIA, TAO, Hakkasan, etc.) every 30 minutes
in **GitHub Actions** and posts to Discord when a price goes up. Nothing runs on your
computer or in your browser.

## How it works

1. Finds upcoming events from taogroup.com's public events feed (by the `search` words in `config.json`),
   plus any ticket links you list in `ticket_urls`.
2. Opens each `tickets.taogroup.com` page in a headless browser in the cloud and reads the ticket tiers and prices.
3. Compares them to the last run (stored in `prices.json`, committed back to the repo) and alerts on increases.

## Set up Discord alerts (2 minutes)

1. In Discord, open the channel you want alerts in → **Edit Channel → Integrations → Webhooks → New Webhook** → **Copy Webhook URL**.
2. In this GitHub repo: **Settings → Secrets and variables → Actions → New repository secret**,
   name `DISCORD_WEBHOOK_URL`, value = the URL you copied.

(Optional: an `NTFY_TOPIC` secret also sends alerts to the ntfy phone app.)

## Choose what to watch

Edit `config.json` on GitHub:

| key | meaning |
| --- | --- |
| `search` | words matched against taogroup.com events, e.g. `["omnia"]`, `["marquee"]`, `["tao nightclub"]` |
| `ticket_urls` | specific `tickets.taogroup.com/e/.../tickets` links to always watch |
| `days_ahead` | only watch events within this many days |
| `max_events` | cap on pages checked per run |
| `alert_price_drops`, `alert_new_tiers`, `alert_sold_out` | extra alert types |

The schedule is in `.github/workflows/tao-watch.yml` (`cron`). To run it right now, go to
**Actions → Tao price watch → Run workflow** (tick *debug* to download the page text it saw).

## Note on Cloudflare

`tickets.taogroup.com` uses a Cloudflare bot check. If the run log says
`stuck on the Cloudflare check`, GitHub's servers are being blocked and prices can't be read from there.

---

# Shotgun new-event watch

Checks the [Nu Androids Shotgun page](https://shotgun.live/en/venues/nu-androids) every 30 minutes and pings when a new upcoming event is listed.

New-event alerts go to their own Discord channel, **#ai-warehouse-new-events**. To set it up, create a webhook in that channel the same way as above and save it as a repository secret named `SHOTGUN_DISCORD_WEBHOOK_URL`. Until that secret exists, alerts go to the shared `DISCORD_WEBHOOK_URL` channel instead. `NTFY_TOPIC` is shared with the price watch.

- `shotgun_watch.py` is the scraper and uses only the Python standard library.
- `.github/workflows/shotgun-watch.yml` runs it on a schedule.
- `seen_events.json` records events already seen. The workflow commits it back to the repo. The first run only records the current events and sends no pings.

To test notifications, go to **Actions → Shotgun watch → Run workflow** and tick *Send a test ping*.

**How it gets past the bot check:** shotgun.live is behind a Vercel checkpoint that blocks scripts and headless browsers. The scraper fetches the page through the [Jina reader](https://jina.ai/reader) proxy, which renders it in a real browser.

**The 24-event limit:** The page shows only the first 24 upcoming events, about a month out, behind a "See more" button. Add a free `JINA_API_KEY` secret from jina.ai and the scraper clicks "See more" until it has the full list. Without the key, an event announced further out triggers a ping once it moves into that window.

To run it locally: `python3 shotgun_watch.py --dry-run`. To watch another venue, use `--url https://shotgun.live/en/venues/<venue> --state other.json`.

---

# Frontgate stock watcher

A small web app for **kartis.homes/frontgate**. Paste a `*.frontgatetickets.com/event/...` link and it shows how many tickets were released and how many are left in each tier. It checks every 2 minutes, keeps a history chart, and pings Discord/ntfy when:

- a tier sells out (or every remaining ticket is sitting in carts)
- a sold-out tier comes back (expired carts or a new drop)
- a new tier is released, or a tier's capacity is raised
- the total left drops below the number you set for that event

**How it reads stock:** Frontgate's event pages don't show counts, but their seating-chart code calls a public endpoint, `/section/<eventHubId>/info`, that returns `totalSeats` and `availableSeats` per section. The watcher reads the event page once to find the `eventHubId`, then polls that endpoint. It never adds to a cart. "Left" excludes tickets in other people's carts, so it can tick back up when a 10-minute cart expires.

Everything is in `frontgate/`. It uses only the Python standard library and stores data in SQLite.

## Run it on the kartis.homes server

1. Copy this repo to the server, e.g. `git clone https://github.com/13pinny/2.0 /opt/2.0`.
2. Create `/opt/2.0/frontgate/.env` with your alert settings:
   ```
   DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
   NTFY_TOPIC=your-topic
   ```
3. Install the service: copy `frontgate/frontgate-watch.service` to `/etc/systemd/system/`, then run `sudo systemctl daemon-reload && sudo systemctl enable --now frontgate-watch`.
4. Add the `/frontgate` block from `frontgate/Caddyfile.example` to your kartis.homes site block, then run `sudo systemctl reload caddy`. Your existing Caddy login covers it. The app itself only listens on 127.0.0.1:8787.

Open https://kartis.homes/frontgate/ and use **Send test ping** to check your alerts.

| setting | default | meaning |
| --- | --- | --- |
| `FRONTGATE_INTERVAL` | `120` | seconds between checks (minimum 30) |
| `FRONTGATE_PORT` / `FRONTGATE_HOST` | `8787` / `127.0.0.1` | where the app listens |
| `FRONTGATE_DB` | `frontgate/frontgate.db` | SQLite file |

To check one event quickly from a terminal: `python3 frontgate/frontgate_watch.py --check <link>`.
