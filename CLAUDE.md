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
| Smoke-test Discord + Gmail | `.venv\Scripts\python notify.py` (expects `{'discord': 'ok (204)', 'email': 'ok'}`) |
| Probe a single drop endpoint directly | `.venv\Scripts\python ticketmaster.py EVENT/PERF` (also `kupat.py`, `tickchak.py`) |

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

URL → source detection lives in `_detect_source` in `app.py:43` and `add_watcher.py:30` (identical logic; keep in sync). Bare numeric shorthand `1358/51596` routes to kupat; alpha-prefixed `MBP19/001` routes to ticketmaster; bare slug `mada26` routes to tickchak.

`ticketmaster.py` and `kupat.py` are pure HTTP (anonymous JSON APIs, no login). `tickchak.py` is similar. None of these go through Chrome.

### Drop-check tick

The per-watcher diff-and-notify cycle is implemented twice — once in `app.py` (as `_check_one_watcher` / `run_tm_check`) and once in `watcher_only.py` (`check_one` / `tick`). Logic is intentionally parallel: fetch seats, diff against `tm_seat_state` rows in the DB, store new state, apply user filters via `filters.py`, notify via `notify.py`, log a drop row. Baseline ticks (first check ever for a watcher) suppress notifications. **If you change the tick logic, update both implementations.**

Notification gating order (both implementations): per-watcher `paused` skips entirely → `master_paused` global skips entirely → filters (`filters.apply`) drop unmatched seats → `master_muted` / per-watcher `muted` / empty `notify_channels` skip sending but still log to `tm_drops`.

### Lysted / Viagogo / CrowdVolt scraping (full mode only)

`scraper.py` uses `patchright.sync_api` to `connect_over_cdp("http://localhost:9222")` against the Chrome window launched by `login.py`. **There is no login automation by design** — Cloudflare blocks headless browsers, so a human signs in once and the scraper inherits the session. Chrome's user profile is stored in `./user_data/` (gitignored). If Chrome is closed, all scraping fails until `login.py` runs again.

### Flask + APScheduler

`app.py` is a single ~3500-line Flask module. The scheduler is started at import time (`app.py:3540-3556`) with four jobs:
- `scrape` — hourly Lysted/Viagogo/CrowdVolt pull
- `tm_check` — every `TM_CHECK_INTERVAL_SECONDS` (default 60) drop check
- `mail_intake` — every `KARTIS_INTAKE_INTERVAL_MINUTES` (default 10) Gmail poll for purchase-confirmation emails
- `backup` — daily 03:00 sqlite copy to `KARTIS_BACKUP_DIR`

Each runner uses a threading lock as a poor-man's mutex so the scheduler can't stack overlapping runs. State dicts (`_last_run`, `_last_backup`, `_last_jerujam`, etc.) at the top of `app.py` back the `/api/*/status` endpoints. `app.run` uses `use_reloader=False` because the scheduler would double-fire under the reloader.

### Database

`db.py` defines the full schema and helpers. SQLite file is `kartis.db` in the repo root, not committed. Tables fall into three buckets:

- **Inventory + sales** — `inventory`, `lysted_purchases`, `viagogo_listings`, `inventory_hidden`, `inventory_unsold`, `sales_hidden`, `sales_canceled`, `event_group_merges`. Populated by `scraper.py` and `import_jerujam.py`. Only `app.py` touches these.
- **Drop watchers** — `tm_watchers`, `tm_seat_state`, `tm_drops`. Both `app.py` and `watcher_only.py` write here. **If both run against the same file you'll hit `database is locked` errors** — run only one per machine.
- **Settings** — key/value via `setting_get_bool` etc.; flags like `master_paused`, `master_muted`.

### Auto-update flow (watcher-only mode)

`supervisor.py` is the single binary Task Scheduler launches at boot. It spawns `watcher_only.py` as a child, runs `git pull --ff-only` every `KARTIS_PULL_INTERVAL_SECONDS` (default 300), and on new commits: terminates the child → `pip install -r requirements.txt` → restarts. Crash backoff is exponential up to 1h. The remote needs to be set up so the first manual `git pull` succeeded — after that Git Credential Manager handles automated pulls.

## Conventions worth knowing

- Seat dicts use mixed key naming: normalized (`block`, `row`, `seat`) for kupat and newer ticketmaster code; raw (`b`, `r`, `l`) for older ticketmaster fixtures. Helpers in `notify.py` (`_seat_block`, `_seat_row`, `_seat_num`) accept both — if you add code that reads seat fields, accept both forms.
- All persisted timestamps are UTC ISO-8601 (`datetime.now(timezone.utc).isoformat()`).
- Watcher IDs are `tmw-<12 hex chars>` (`add_watcher.py:63`).
- `KARTIS_ATTACHMENTS_DIR` defaults to `./attachments` inside the repo — and the repo lives in OneDrive on the main PC, which is the intended cloud backup for ticket PDFs.
- The repo's working directory itself is inside OneDrive (`C:\Users\13pin\OneDrive\Documents\GitHub\2.0`), so writes can be picked up by OneDrive sync.
