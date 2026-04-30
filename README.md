# Kartis

Personal ticket-reseller toolkit, in two halves:

- **Dashboard** (Flask) — pulls listings/sales from Lysted + Viagogo +
  CrowdVolt, imports JeruJam exports, tracks expenses + maaser + profit
  per event. Runs on the main PC where you can keep a Chrome window
  logged in.
- **Ticketmaster.co.il drop checker** — polls sold-out shows for new
  seats and pings Discord + Gmail when they appear. Runs anywhere
  (works great on a headless home server) — no Chrome, no login.

The two halves share code (`db.py`, `notify.py`, etc.) but have
separate entry points so each can run independently.

---

## At a glance — which machine runs what

|  | **Main PC** (full mode) | **Home server** (watcher-only) |
|---|---|---|
| Entry point | `start.bat` → `login.py` → `app.py` | `start_supervised.bat` → `supervisor.py` → `watcher_only.py` |
| Flask dashboard at :5000 | ✅ | ❌ |
| Lysted/Viagogo/CrowdVolt scraper | ✅ hourly via APScheduler | ❌ skipped |
| Chrome window (logged-in `user_data/`) | ✅ required | ❌ not used |
| Ticketmaster drop checker | ✅ ticks every 60s | ✅ ticks every 60s — its only job |
| Discord + Gmail notifications | from this PC's `.env` | from server's `.env` |
| Auto-update from GitHub | ❌ | ✅ via `supervisor.py` |
| Database | full Kartis schema (sales, inventory, expenses, watchers…) | only the `tm_*` watcher tables get rows |
| Needs human attention? | re-login Chrome when sessions expire (~weeks) | none |
| Must be on for notifications? | nice-to-have | yes — this is the always-on one |

**Key rule:** add a Ticketmaster watcher on **only one** machine.
Otherwise both poll the same URL and you get duplicate Discord pings.

---

## How it works

### Ticketmaster drop checker

Pure HTTP. Calls the public ISM seat-plan endpoint that the
ticketmaster.co.il SPA uses internally:

```
GET https://www.ticketmaster.co.il/ismapi/api/v1/seatPlans/getAllSelectableSeats/INTERNET/{event}/{perf}
Headers: CHANNEL: INTERNET, CPU: 32100, Accept-Language: iw
→ JSON list of every seat currently buyable
```

Each tick computes the set of `(block, row, seat-num)` tuples,
diffs against the last known set, and notifies on additions.
Hebrew section names + ILS prices come from
`/wbtxapi/api/v1/bxcached/event/getPriceByProfiles/...`, cached on
disk under `tm_cache/`. No login, no captcha, no browser.

### Lysted / Viagogo / CrowdVolt login (full mode only)

There's **no login automation**. The whole thing piggybacks on a Chrome
window you've already logged into manually:

1. `login.py` launches Chrome with two flags that matter:
   - `--remote-debugging-port=9222` — exposes Chrome DevTools Protocol on
     `localhost:9222` for other processes to attach to.
   - `--user-data-dir=./user_data` — Chrome stores cookies + tokens here
     (gitignored).
2. **You** sign into Lysted, Viagogo, CrowdVolt manually in that
   window. You also pass the Cloudflare "verify you are human" check —
   that's the whole point: a human-controlled browser passes; an
   automated one is blocked.
3. `scraper.py` runs hourly, calls `chromium.connect_over_cdp(":9222")`,
   inherits your sessions, walks the pages.

If Chrome is closed, the scraper fails until you re-run `login.py`.
This pattern is why the dashboard needs a desktop with a real Chrome —
it can't run truly headless. The Ticketmaster watcher exists as a
separate code path precisely because it doesn't need any of this.

---

## Prerequisites (Windows)

1. **Python 3.11+** — https://www.python.org/downloads/windows/
   (tick "Add Python to PATH" during install).
2. **Git for Windows** — https://git-scm.com/download/win.
3. (Full mode only) **Google Chrome** — for the login flow.

```cmd
python --version
git --version
```

---

## Install

```cmd
cd %USERPROFILE%
git clone https://github.com/13pinny/2.0.git kartis
cd kartis
python -m venv .venv
.venv\Scripts\pip install --upgrade pip
.venv\Scripts\pip install -r requirements.txt
```

Full mode also needs the bundled Chromium for patchright (skip on the
home server):

```cmd
.venv\Scripts\python -m patchright install chromium
```

---

## Configure secrets

```cmd
copy .env.example .env
notepad .env
```

| Variable | What it is | Where to get it |
|---|---|---|
| `DISCORD_WEBHOOK_URL` | Channel webhook for drop notifications | Discord → Server Settings → Integrations → Webhooks → New Webhook → Copy URL |
| `GMAIL_USER` | Gmail address that sends the email | your Gmail |
| `GMAIL_APP_PASSWORD` | 16-char app password (NOT your normal Gmail password) | https://myaccount.google.com/apppasswords (requires 2FA on the account) |
| `NOTIFY_EMAIL_TO` | Where the drop emails should land | usually same as `GMAIL_USER` |
| `TM_CHECK_INTERVAL_SECONDS` | Drop-check cadence (default 60) | leave at 60 |
| `KARTIS_PULL_INTERVAL_SECONDS` | Supervisor `git pull` cadence (default 300) | 60 if you want fast deploys |
| `KARTIS_GIT_BRANCH` | Pin supervisor to a specific branch | optional |

For full mode, also keep the `LYSTED_*` URLs already in `.env.example`.

**Test the plumbing** (sends a real test message to Discord + your inbox):

```cmd
.venv\Scripts\python notify.py
```

Expect `{'discord': 'ok (204)', 'email': 'ok'}`.

---

## Watcher-only mode (home server)

### Add what to watch

```cmd
.venv\Scripts\python add_watcher.py "https://www.ticketmaster.co.il/performance/MBP19/001/ALL/iw"
.venv\Scripts\python add_watcher.py --list
```

The CLI accepts the full performance URL or the shorthand `EVENT/PERF`
(e.g. `MBP19/001`). It captures whatever's currently available as a
**baseline** so the first tick doesn't notify on seats that already
existed.

### Run it (with auto-update — recommended)

```cmd
git pull            # once, interactively, so credentials get cached
start_supervised.bat
```

`supervisor.py` runs `watcher_only.py` as a child and `git pull
--ff-only` every 5 minutes. When commits land, it terminates the child,
runs `pip install -r requirements.txt` (in case deps changed), and
starts a fresh watcher. Crash backoff is exponential up to 1h so a
broken commit doesn't burn CPU. Output goes to `supervisor.log`
(gitignored).

**Auto-update flow in one line:** edit code on main PC → `git push` →
home server's supervisor pulls within 5 min → restarts watcher with new
code. No SSH, no webhooks, no port-forwarding — just outbound HTTPS to
GitHub.

**Private repo?** The first manual `git pull` triggers Git Credential
Manager to authenticate via GitHub's OAuth flow and cache the token in
Windows Credential Manager. After that the supervisor's automated pulls
just work. For truly unattended deploys, prefer a deploy key over a
personal token.

### Run it (without auto-update)

If you'd rather pull manually:

```cmd
start_watcher.bat
```

…or the no-frills version:

```cmd
.venv\Scripts\python watcher_only.py
```

### Auto-start on Windows boot

1. Open **Task Scheduler** → **Create Task** (not Basic Task).
2. **General** tab: name `Kartis Drop Checker`, choose "Run whether user
   is logged on or not", check "Run with highest privileges".
3. **Triggers** → New → "At system startup".
4. **Actions** → New → "Start a program" →
   - Program: `C:\Users\<you>\kartis\start_supervised.bat`
   - Start in: `C:\Users\<you>\kartis`
5. **Settings**: uncheck "Stop if runs longer than..." (it's a daemon);
   check "If the task fails, restart every 1 minute" × 3 attempts so
   transient failures self-heal.
6. OK → enter your Windows password (required for unattended mode).

Reboot to verify it auto-launches. Tail with `type supervisor.log` from
the install dir.

---

## Full mode (main PC, with dashboard)

Same install + secrets, then:

```cmd
.venv\Scripts\python -m patchright install chromium
start.bat
```

`start.bat` runs `login.py` (opens Chrome, you log into the three
sites, press Enter), then starts Flask at http://localhost:5000.

The dashboard's `/watchers` page is the friendlier UI for the same
drop-checker — add by pasting a URL, pause/resume, delete, view drop
history. The UI and `add_watcher.py` write to the same `kartis.db`, so
on a single machine they're interchangeable.

Keep the Chrome window open (minimized is fine). The hourly scraper
attaches to it silently. If you close it, re-run `start.bat`.

---

## Troubleshooting

**`ModuleNotFoundError: No module named 'dotenv'`** — you ran the system
`python`, not the venv one. Use `.venv\Scripts\python` (or activate the
venv first with `.venv\Scripts\activate`).

**Discord returns 401 / 404** — webhook URL was rotated or deleted.
Generate a new one and update `.env`.

**Gmail returns "auth failed"** — you used your regular Gmail password
instead of an App Password. Generate one at
https://myaccount.google.com/apppasswords (requires 2FA enabled on the
account first).

**Watcher reports `HTTP 403 from ...`** — Ticketmaster blocked the
request. Rare; wait a minute, then run `python ticketmaster.py
EVENT/PERF` directly to see the raw response.

**`db.OperationalError: database is locked`** — `watcher_only.py` and
`app.py` are fighting over the same `kartis.db`. Run only one of them
on the same machine.

**Supervisor logs `git pull failed`** — usually means you have local
edits or the auth cache expired. SSH/RDP into the home server, run
`git status` and `git pull` interactively to resolve.

---

## Files

| Module | Purpose |
|---|---|
| `app.py` | Flask app — full dashboard. Imports `scraper.py` and needs Chrome. |
| `watcher_only.py` | Headless drop-checker daemon. No Flask, no Chrome. |
| `supervisor.py` | Wraps `watcher_only.py` with `git pull` auto-update + crash restart. |
| `add_watcher.py` | CLI to add/list/remove watchers without the dashboard. |
| `ticketmaster.py` | Drop-checker API client (parse URL, fetch seats, diff). |
| `notify.py` | Discord webhook + Gmail SMTP senders. |
| `labels.py` | Hebrew section names + ILS prices, cached at `tm_cache/`. |
| `scraper.py` | Lysted/Viagogo/CrowdVolt scraper (attaches to Chrome via CDP). |
| `login.py` | Launches Chrome with remote-debugging port for the manual login flow. |
| `import_jerujam.py` | Imports JeruJam Excel exports into the dashboard. |
| `matcher.py` | Pairs JeruJam tickets to Lysted/Viagogo sales. |
| `db.py` | SQLite schema + helpers. |
| `start.bat` | One-click main-PC launcher (login + Flask). |
| `start_watcher.bat` | One-click watcher launcher. |
| `start_supervised.bat` | One-click watcher + auto-update launcher. |
| `kartis.db` | Local DB. **Not committed to git** — each install gets its own. |
| `.env` | Secrets. **Not committed to git.** Always start from `.env.example`. |
