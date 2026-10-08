# Tao Group price watch

Checks ticket prices for Tao Group events (Marquee, OMNIA, TAO, Hakkasan, etc.) every 30 minutes
in **GitHub Actions** and sends a phone notification when a price goes up. Nothing runs on your
computer or in your browser.

## How it works

1. Finds upcoming events from taogroup.com's public events feed (by the `search` words in `config.json`),
   plus any ticket links you list in `ticket_urls`.
2. Opens each `tickets.taogroup.com` page in a headless browser in the cloud and reads the ticket tiers and prices.
3. Compares them to the last run (stored in `prices.json`, committed back to the repo) and alerts on increases.

## Set up phone alerts (2 minutes)

1. Install the free **ntfy** app (iOS / Android).
2. In the app, subscribe to a topic with a hard-to-guess name, e.g. `tao-prices-8f3k2q`.
3. In this GitHub repo: **Settings → Secrets and variables → Actions → New repository secret**,
   name `NTFY_TOPIC`, value = your topic name.

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
