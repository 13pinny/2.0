# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

Kartis is a personal ticket-reseller toolkit with two halves that share code but boot from separate entry points:

- **Full dashboard mode** (`app.py`) — Flask UI on :5000. Scrapes Lysted / Viagogo / CrowdVolt by attaching to a manually-logged-in Chrome instance over CDP; imports JeruJam Excel exports; tracks expenses, maaser, and profit; runs the Ticketmaster/Kupat/Tickchak drop checker. Requires a desktop with Chrome.
- **Watcher-only mode** (`watcher_only.py`, supervised by `supervisor.py`) — headless drop-checker daemon. No Flask, no Chrome, no browser deps. Designed for an always-on home server. The supervisor `git pull`s every 5 min and restarts the watcher when commits land.

Both share `kartis.db` (SQLite), `db.py`, `notify.py`, and the per-source drop-checker modules. **Only one machine should hold each watcher** — both modes poll independently and would produce duplicate Discord pings.

## Commands

All commands assume the venv at `.venv\Scripts\`. On Windows use `.venv\Scripts\python`; on the Bash tool use `.venv/Scripts/python`.

| What | Command |
|---|---|
| Install deps | `.venv\Scripts\pip install -r requirements.txt` |
| Install Chromium for `patchright` (full mode only) | `.venv\Scripts\python -m patchright install chromium` |
| Run full dashboard (interactive Chrome login first) | `start.bat` — or `python login.py` then `python app.py` |
| Run headless watcher (one-shot) | `.venv\Scripts\python watcher_only.py` |
| Run watcher with auto-update | `start_supervised.bat` (wraps `supervisor.py`) |
| Add a watcher from the CLI | `.venv\Scripts\python add_watcher.py "<URL or EVENT/PERF>"` |
| List watchers | `.venv\Scripts\python add_watcher.py --list` |
| Remove a watcher | `.venv\Scripts\python add_watcher.py --remove <id>` |
| Smoke-test Discord + Gmail | `.venv\Scripts\python notify.py` (expects `{'discord': 'ok (204)', 'email': 'ok'}`, then one line per configured per-category webhook) |
| Probe a single drop endpoint directly | `.venv\Scripts\python ticketmaster.py EVENT/PERF` (also `kupat.py`, `tickchak.py`, `barby.py <url-or-showId>`, `dice.py <dice.fm URL>`) |
| Extract Pacha NY ticket first-pages from Gmail | `.venv\Scripts\python pacha_tickets.py` (add `--dry-run`, `--days N`, `--folder`) |
| Probe the Pacha NYC events listing (the new-event monitor's fetcher) | `.venv\Scripts\python pacha_events.py` (add `--json`) |
| Probe the kupat / TM-IL / barby event listings (the IL new-event monitor's fetchers) | `.venv\Scripts\python kupat_events.py` / `.venv\Scripts\python tm_events.py` / `.venv\Scripts\python barby_events.py` (add `--json`; tm also `--onsale`) |
| Extract tickchak /n/ ticket links from Gmail | `.venv\Scripts\python tickchak_tickets.py` (add `--url URL`, `--dry-run`, `--debug`) |
| Probe a public viagogo event's competitor listings | `.venv\Scripts\python scripts\probe_viagogo_public.py <event_id or "artist name">` |
| Probe/exercise the viagogo listing price edit | `.venv\Scripts\python scripts\probe_viagogo_edit_price.py <listing_id> [new_price]` |
| Dry-run the market-wide tracker sweep (the /market page's fetchers) | `.venv\Scripts\python market.py --source kupat\|tm\|tickchak\|zappa\|all` (add `--json`; `--write` persists like a real tick) |
| Probe the zappa-club.co.il catalog (the market sweep's zappa fetcher) | `.venv\Scripts\python zappa_events.py` (add `--json`, `--category "<name>"`; opens a headful off-screen Chromium — headless is blocked) |
| Dry-run the viagogo market-sales tracker (the /vgsales page's fetcher) | `.venv\Scripts\python viagogo_market_sales.py` (add `--event <id>`, `--json`; `--write` persists like a real tick) |
| Probe the inv.viagogo magnifier popup's sales grid raw | `.venv\Scripts\python scripts\probe_viagogo_market_sales.py [event_id]` |

There is no test suite, no linter, and no build step — the project ships as plain Python scripts.

## Architecture

### Drop-checker source plugins

Each ticketing site is a module exposing a fixed interface; sources are registered in a `SOURCES` / `WATCHER_SOURCES` dict in `app.py:36`, `watcher_only.py:36`, and `add_watcher.py:27`. **To add a new site, write one module and add one row to those three dicts.** Required functions:

- `parse_url(url) -> (event_code, perf_code)` — also accepts shorthand `EVENT/PERF`
- `perf_url(event_code, perf_code) -> str`
- `fetch_selectable_seats(event_code, perf_code) -> list[dict]`
- `seat_key(seat) -> hashable` — what gets diffed between ticks
- `get_labels(event_code, perf_code, lang="iw", missing_block=None)` — Hebrew section names + ILS prices, cached under `tm_cache/`
- `event_summary(labels) -> str`

URL → source detection lives in `_detect_source` in `app.py:43` and `add_watcher.py:30` (identical logic; keep in sync). Bare numeric shorthand `1358/51596` routes to kupat; alpha-prefixed `MBP19/001` routes to ticketmaster; bare slug `mada26` routes to tickchak. Barby is matched on the `barby.co.il` host only — a bare show id like `5086` is indistinguishable from a tickchak slug and routes to tickchak, so paste the full URL.

`ticketmaster.py` and `kupat.py` are pure HTTP (anonymous JSON APIs, no login). `tickchak.py` is similar. None of these go through Chrome.

`kupat.py` is the exception that DOES need Chrome: its `seats-status` endpoint 403s any out-of-page fetch, so seats are harvested by capturing the booking SPA's own XHR (~5-10s/check). Two kupat traps are documented at length in its module docstring — the SPA ignores the `prsntId` URL param (you must click the date row), and `isGA` is an int, not a bool. Read it before touching that module.

`dice.py` (dice.fm, EDM/US) is pure HTTP: the anonymous `api.dice.fm/events/<24-hex id>/ticket_types` endpoint exposes per-type prices, status, and the current price TIER — but NO remaining counts. The internal id is resolved once from the event page's `dice://open/events/<id>` meta tag and cached in `tm_cache/dice_ids.json`. One pseudo-seat per on-sale type, key encodes (name, tier, price), so restocks/new types/tier jumps ping and sell-outs are silent removals. Also a manual /market source (paste a dice.fm URL on the page; currency USD, no velocity — like zappa).

`barby.py` (barby.co.il, Tel Aviv club) is pure HTTP and status-only: Barby publishes no seat map and no reliable tickets-left (`sold` can exceed `nochair_max_buy`), so a show is tracked as ONE status-encoded pseudo-seat driven by the `isSoldOut` bool from `/api/shows/show/<id>` — sold-out→on-sale adds a `GA available` seat (routes to `drops`), the reverse routes to `status`. perf_code is always `0`. Don't confuse it with `barby_events.py`, which watches the *catalog* for new shows.

### Drop-check tick

The per-watcher diff-and-notify cycle is implemented twice — once in `app.py` (as `_check_one_watcher` / `run_tm_check`) and once in `watcher_only.py` (`check_one` / `tick`). Logic is intentionally parallel: fetch seats, diff against `tm_seat_state` rows in the DB, store new state, apply user filters via `filters.py`, notify via `notify.py`, log a drop row. Baseline ticks (first check ever for a watcher) suppress notifications. **If you change the tick logic, update both implementations.**

Notification gating order (both implementations): per-watcher `paused` skips entirely → `master_paused` global skips entirely → filters (`filters.apply`) drop unmatched seats → `master_muted` / per-watcher `muted` / empty `notify_channels` skip sending but still log to `tm_drops`.

### Lysted / Viagogo / CrowdVolt scraping (full mode only)

`scraper.py` uses `patchright.sync_api` to `connect_over_cdp("http://localhost:9222")` against the Chrome window launched by `login.py`. **There is no login automation by design** — Cloudflare blocks headless browsers, so a human signs in once and the scraper inherits the session. Chrome's user profile is stored in `./user_data/` (gitignored). If Chrome is closed, all scraping fails until `login.py` runs again.

### Viagogo auto-pricer (full mode only)

`viagogo_pricer.py` keeps opted-in listings just under the cheapest competitor for the same event + section (default undercut $0.04, setting `pricer_undercut`). Runs every 15 min from `app.py`'s scheduler (`run_pricer`; `KARTIS_PRICER_INTERVAL_MINUTES`) — **never add it to watcher_only.py** (needs the CDP Chrome). Controls live on the /sources dashboard (per-listing AUTO + FLOOR, master + dry-run toggles); config/audit in `viagogo_pricer_config` / `viagogo_pricer_log` (the scrape mirror `viagogo_listings` stays pure). All prices USD. Key empirical facts (public URL discovery via homepage search → groupedsearch JSON, the `index-data` script blob on event pages, buyer-currency `rawPrice` + per-event fx calibration from our own listing, price writes via the row's edit modal — the raw SaveListing form-replay fails validation) are documented in the module docstring — read it before touching pricing or edit logic. Safety: master kill switch + dry-run default ON, per-listing floors, lower-only (raise is double-flagged off), a sliding-window drop cap (`pricer_max_drop_pct`, default 15% per `pricer_drop_window_hours` 12h — holds + pings when hit), and manual price changes on inv.viagogo pause that listing until resumed from the dashboard. UI lives on /pricer (mobile-friendly; add-to-home-screen works on iPhone).

### Flask + APScheduler

`app.py` is a single ~3500-line Flask module. The scheduler is started at import time (`app.py:3540-3556`) with four jobs:
- `scrape` — hourly Lysted/Viagogo/CrowdVolt pull
- `tm_check` — every `TM_CHECK_INTERVAL_SECONDS` (default 60) drop check
- `mail_intake` — every `KARTIS_INTAKE_INTERVAL_MINUTES` (default 10) Gmail poll for purchase-confirmation emails
- `pacha_events` — every `KARTIS_PACHA_MONITOR_INTERVAL_MINUTES` (default 10) poll of pacha-nyc.com/events (`pacha_events.py`, pure HTTP); Discord-pings new events, waitlist→on-sale flips, GA price climbs, and low-stock crossings (GA release's tickets-left drops to ≤ `KARTIS_PACHA_LOW_STOCK_THRESHOLD`, default 20; re-arms per release); pings include per-tier "N of M left" counts. State in `pacha_seen_events` (incl. `ga_release`/`ga_available` for the crossing detection). Each tick also feeds /market: a `market_events` row (source `pacha`, `currency` USD — the column exists because every other source is ILS) + an availability snapshot into `tickchak_sales_snapshots`, so Pacha rows get the same sold-per-window velocity; note pacha tier counts are per-RELEASE, so a new release opening raises `available` (the velocity clamp ignores increases). Runs on the VPS — exactly one machine may enable it (`KARTIS_PACHA_MONITOR_ENABLED=0` on the PC) or Discord double-pings. Manual tick: `POST /api/pacha-events/run-now`; status: `GET /api/pacha-events/status`. Dedicated dashboard: **/pacha** (`templates/pacha.html` + `GET /api/pacha`) — tiles, per-release GA counts, click-to-expand tier breakdowns (stored per tick in `pacha_seen_events.tiers_json`), velocity windows, Σ sold.
- `il_events` — every `KARTIS_IL_EVENTS_INTERVAL_MINUTES` (default 10) poll of the kupat.co.il, ticketmaster.co.il, and barby.co.il event listings (`kupat_events.py` / `tm_events.py` / `barby_events.py`, pure HTTP); Discord-pings new events and, for TM, listed→on-sale flips (kupat and barby have no pre-sale listing state); state in `site_seen_events`. Barby's feed is `barby.co.il/api/shows/find` (whole catalog; Cloudflare blocks curl's TLS fingerprint but plain urllib with browser headers passes — a CF challenge surfaces as a JSON-parse error, never as an empty catalog). Kupat's feed is `tickets.kupat.co.il/api/features` (the whole catalog; unlike the seats endpoints it answers plain urllib). TM's feeds are the homepage `getAllTopEvent`/`getSpotLightData` wbtxapi endpoints; sale status is resolved lazily per event from the perf-list status strings (**not** the `active` flag, which lags — see `tm_events._SALE_OPENED_STATUSES`; sold-out counts as "sale opened" so this monitor never overlaps the drop checker's soldout→selling pings). Same single-machine rule as pacha (`KARTIS_IL_EVENTS_ENABLED=0` on the PC). Manual tick: `POST /api/il-events/run-now`; status: `GET /api/il-events/status`.
- `market_sweep` — every `KARTIS_MARKET_INTERVAL_MINUTES` (default 60) market-wide availability sweep (`market.py`) feeding the /market page: every kupat presentation (site-wide `/api/presentations/` catalog + parallel in-page detail fetches through one `kupat.BrowserSession` — the CDN only challenges page loads, so in-page `fetch()` is cheap), every TM-IL homepage event's performances (buyability gated on the perf-list status strings, NOT `getPerformanceDetail.salesOptions`, which returns 0 for everything as of 2026-07), manually-added tickchak events/hubs (`market_manual` table; no global tickchak catalog exists), and the whole zappa-club.co.il catalog (`zappa_events.py` — Eventim-IL white-label; Akamai blocks direct HTTP *and* headless Chromium, so it runs a headful off-screen window; Eventim publishes no ticket counts, so zappa rows are status + from-price only). Because only this machine can fetch zappa, its new-event/on-sale Discord pings fire from the sweep itself (`site_seen_events` source `zappa`), NOT from il_events. Latest state in `market_events`; availability history goes into `tickchak_sales_snapshots` under the same `(source, event_code, perf_code)` keys the watcher-fed `sales_sync` uses, so watched events share one denser series and the page reuses `_sales_windows`. **The sweep never writes `site_seen_events` for kupat/tm** — pre-seeding would swallow the il_events new-event pings. Needs patchright Chromium (kupat), so it runs on the dashboard machine; `KARTIS_MARKET_ENABLED=0` elsewhere. No notifications. Manual tick: `POST /api/market/run-now` (synchronous, ~2 min); status: `GET /api/market/status`; manual tickchak add: `POST /api/market/add {"url": ...}`.
- `vgsales` — every `KARTIS_VGSALES_INTERVAL_MINUTES` (default 30) viagogo market-SALES tracker (`viagogo_market_sales.py`) feeding the **/vgsales** page: one `MarketDataV3` POST per tracked event (every event we hold a fresh listing on + the `viagogo_sales_watchlist`) captures the magnifier popup's "past ten sales" grid — ALL sellers' sales, USD per ticket, works for events we're not listed on. The rows carry NO dates and NO ids (identical rows are common), so newness is decided by aligning the ordered window against the previous tick's (`viagogo_sales_events.window_json`); an event's first-ever window is stored as `baseline=1` rows that velocity windows ignore (age unknown); a sale's `observed_at` = the tick that first saw it. Sales land in `viagogo_market_sales` (90d prune, `KARTIS_VGSALES_RETENTION_DAYS`). The pricer's own MarketDataV3 fetch piggybacks into the same ingest (`viagogo_market_sales.ingest_window`), giving pricer-enabled events 15-min resolution, and the tick skips events fetched within interval/2 — the run-now button forces past that. Needs the CDP Chrome, so dashboard machine only (`KARTIS_VGSALES_ENABLED=0` elsewhere). No notifications in v1. Manual tick: `POST /api/vgsales/run-now` (synchronous, forced); status: `GET /api/vgsales/status`; watch/unwatch: `POST /api/vgsales/watch {"url": ...}` / `POST /api/vgsales/unwatch {"event_id": ...}`.
- `backup` — daily 03:00 sqlite copy to `KARTIS_BACKUP_DIR`

Each runner uses a threading lock as a poor-man's mutex so the scheduler can't stack overlapping runs. State dicts (`_last_run`, `_last_backup`, `_last_jerujam`, etc.) at the top of `app.py` back the `/api/*/status` endpoints. `app.run` uses `use_reloader=False` because the scheduler would double-fire under the reloader.

### Database

`db.py` defines the full schema and helpers. SQLite file is `kartis.db` in the repo root, not committed. Tables fall into three buckets:

- **Inventory + sales** — `inventory`, `lysted_purchases`, `viagogo_listings`, `inventory_hidden`, `inventory_unsold`, `sales_hidden`, `sales_canceled`, `event_group_merges`. Populated by `scraper.py` and `import_jerujam.py`. Only `app.py` touches these. (Market-wide viagogo sales — other sellers' sales, not ours — live separately in `viagogo_market_sales` / `viagogo_sales_events` / `viagogo_sales_watchlist`, fed by `viagogo_market_sales.py`.)
- **Drop watchers** — `tm_watchers`, `tm_seat_state`, `tm_drops`. Both `app.py` and `watcher_only.py` write here. **If both run against the same file you'll hit `database is locked` errors** — run only one per machine.
- **Settings** — key/value via `setting_get_bool` etc.; flags like `master_paused`, `master_muted`.

### Auto-update flow (watcher-only mode)

`supervisor.py` is the single binary Task Scheduler launches at boot. It spawns `watcher_only.py` as a child, runs `git pull --ff-only` every `KARTIS_PULL_INTERVAL_SECONDS` (default 300), and on new commits: terminates the child → `pip install -r requirements.txt` → restarts. Crash backoff is exponential up to 1h. The remote needs to be set up so the first manual `git pull` succeeded — after that Git Credential Manager handles automated pulls.

## Conventions worth knowing

- Seat dicts use mixed key naming: normalized (`block`, `row`, `seat`) for kupat and newer ticketmaster code; raw (`b`, `r`, `l`) for older ticketmaster fixtures. Helpers in `notify.py` (`_seat_block`, `_seat_row`, `_seat_num`) accept both — if you add code that reads seat fields, accept both forms.
- Discord notifications route per category to separate channels via optional `DISCORD_WEBHOOK_DROPS` / `_STATUS` / `_NEW_EVENTS` / `_PRICER` / `_LISTINGS` / `_TODOS` env vars (see `_WEBHOOK_ENV` in `notify.py`); any category without its own webhook falls back to `DISCORD_WEBHOOK_URL`. New notification kinds should pick a category (or add one) rather than reading `DISCORD_WEBHOOK_URL` directly. Within `notify_drop`, alerts whose added seats are all non-buyable status pseudo-seats (sold out / last tickets / closed) route to `status`; anything actually buyable routes to `drops`. Additionally, a watcher with a `discord_channel` name sends its buyable-drop pings to a per-event channel instead: `discord_bot.py` lazily creates the channel + webhook via a bot (`DISCORD_BOT_TOKEN` + `DISCORD_GUILD_ID`, optional `DISCORD_DROPS_CATEGORY_ID`; cache in `discord_event_channels`), new watchers default to a dateless slug of their label (`slugify_label`) so multi-date shows share one channel, and any bot failure falls back to the shared drops webhook. Status pings never use per-event channels.
- All persisted timestamps are UTC ISO-8601 (`datetime.now(timezone.utc).isoformat()`).
- Watcher IDs are `tmw-<12 hex chars>` (`add_watcher.py:63`).
- `KARTIS_ATTACHMENTS_DIR` defaults to `./attachments` inside the repo — and the repo lives in OneDrive on the main PC, which is the intended cloud backup for ticket PDFs.
- The repo's working directory itself is inside OneDrive (`C:\Users\13pin\OneDrive\Documents\GitHub\2.0`), so writes can be picked up by OneDrive sync.
