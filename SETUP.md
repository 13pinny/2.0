# Kartis — Setup

This repo runs in two modes:

- **Watcher-only** (recommended for an always-on home server) — just the
  Ticketmaster.co.il drop checker. Pings Discord + Gmail when new seats
  appear on a sold-out show. No Chrome, no login flow.
- **Full Kartis** — the dashboard plus the Lysted/Viagogo/CrowdVolt
  scrapers, which attach to a Chrome window you've manually logged into.
  Use this on your main PC.

Each install is independent: every machine has its own `kartis.db`, its
own watcher list, its own .env. Nothing is shared automatically.

---

## Prerequisites (Windows)

1. **Python 3.11 or newer** — https://www.python.org/downloads/windows/.
   During install, tick "Add Python to PATH".
2. **Git** — https://git-scm.com/download/win.
3. (Full mode only) **Google Chrome** — for the Lysted login flow.

Check both work:

```cmd
python --version
git --version
```

---

## Clone + install

```cmd
cd %USERPROFILE%
git clone https://github.com/13pinny/2.0.git kartis
cd kartis
python -m venv .venv
.venv\Scripts\pip install --upgrade pip
.venv\Scripts\pip install -r requirements.txt
```

(Full mode only: also install the bundled Chromium for patchright)

```cmd
.venv\Scripts\python -m patchright install chromium
```

Watcher-only mode skips that step — it never touches a browser.

---

## Configure secrets

Copy the template and fill in the values you want:

```cmd
copy .env.example .env
notepad .env
```

For the watcher (both modes need these):

| Variable | What it is | Where to get it |
|---|---|---|
| `DISCORD_WEBHOOK_URL` | Channel webhook for drop notifications | Discord → Server Settings → Integrations → Webhooks → New Webhook → Copy URL |
| `GMAIL_USER` | Gmail address that sends the email | your Gmail |
| `GMAIL_APP_PASSWORD` | 16-char app password (NOT your normal password) | https://myaccount.google.com/apppasswords (requires 2FA on the account) |
| `NOTIFY_EMAIL_TO` | Where the drop emails should land | usually same as `GMAIL_USER` |
| `TM_CHECK_INTERVAL_SECONDS` | How often to poll (default 60) | leave at 60 |

For full mode also set the existing `LYSTED_*` URLs (already in
`.env.example`).

---

## Watcher-only mode (home server)

### One-time: pick what to watch

```cmd
.venv\Scripts\python add_watcher.py "https://www.ticketmaster.co.il/performance/MBP19/001/ALL/iw"
.venv\Scripts\python add_watcher.py --list
```

The CLI accepts the full performance URL or the shorthand `EVENT/PERF`
(e.g. `MBP19/001`). It captures the current set of available seats as a
**baseline** so you don't get spam-pinged for seats that already existed.

### Run it

Double-click `start_watcher.bat`, or run from a terminal:

```cmd
.venv\Scripts\python watcher_only.py
```

Each tick it prints `checked=N drops=M errors=K`. When the seat set
changes, it sends a Discord embed + Gmail email with section name
(Hebrew) and ILS price.

### Auto-start on boot (Windows Task Scheduler)

1. Open **Task Scheduler** → Create Task.
2. **General** tab: name "Kartis Drop Checker", check "Run whether user is
   logged on or not", check "Run with highest privileges".
3. **Triggers**: New → Begin: At system startup.
4. **Actions**: New → Start a program →
   - Program: `C:\Users\13pin\kartis\start_watcher.bat`
   - Start in: `C:\Users\13pin\kartis`
5. **Settings**: uncheck "Stop if runs longer than..." (it's a daemon).
6. OK → enter your password.

Reboot to verify it auto-starts. Logs go to a console window if you
chose "user logged on" mode; for unattended mode, redirect inside the
.bat: `python watcher_only.py >> watcher.log 2>&1`.

### Send a test ping

To confirm Discord + Gmail are wired up before relying on it:

```cmd
.venv\Scripts\python notify.py
```

Should print `{'discord': 'ok (204)', 'email': 'ok'}`.

---

## Full mode (main PC, with dashboard)

Same install steps, then:

```cmd
.venv\Scripts\python -m patchright install chromium
start.bat
```

`start.bat` opens Chrome via `login.py`, asks you to log into Lysted +
Viagogo + CrowdVolt in the launched window, then starts Flask. Visit
http://localhost:5000.

The dashboard's `/watchers` page is a friendlier UI for the same
watchers — add by pasting a URL, pause/resume, delete, view drop
history. Both UI and CLI write to the same `kartis.db`, so on a single
machine they're interchangeable.

---

## Troubleshooting

**`ModuleNotFoundError: No module named 'dotenv'`** — you ran the wrong
Python. Use `.venv\Scripts\python` (or activate the venv first), not the
system `python`.

**Discord returns 401 / 404** — webhook URL was rotated or deleted.
Generate a new one and update `.env`.

**Gmail returns "auth failed"** — you used your regular Gmail password
instead of an App Password. Generate one at
https://myaccount.google.com/apppasswords (requires 2FA enabled on the
account first).

**Watcher reports `HTTP 403 from ...`** — Ticketmaster blocked the
request. Rare; try waiting a minute, then run `python ticketmaster.py
EVENT/PERF` directly to see the raw error.

**`db.OperationalError: database is locked`** — you're running both
`watcher_only.py` and `app.py` against the same `kartis.db`. Pick one.

---

## Files

| Module | Purpose |
|---|---|
| `app.py` | Flask app — full dashboard. Imports `scraper.py` and needs Chrome. |
| `watcher_only.py` | Headless drop-checker daemon. No Flask, no Chrome. |
| `add_watcher.py` | CLI to add/list/remove watchers without the dashboard. |
| `ticketmaster.py` | Drop-checker API client (parse URL, fetch seats, diff). |
| `notify.py` | Discord webhook + Gmail SMTP senders. |
| `labels.py` | Hebrew section names + ILS prices, cached at `tm_cache/`. |
| `db.py` | SQLite schema + helpers. |
| `kartis.db` | Local DB. **Not committed to git** — each install gets its own. |
| `.env` | Secrets. **Not committed to git.** Always start from `.env.example`. |
