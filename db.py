import datetime as _dt
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(__file__).parent / "kartis.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS inventory (
    id TEXT PRIMARY KEY,
    event_name TEXT,
    event_date TEXT,
    event_time TEXT,
    event_date_iso TEXT,
    venue TEXT,
    listings_count INTEGER,
    tickets_count INTEGER,
    total_cost REAL,
    total_list REAL,
    stubhub_url TEXT,
    last_seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS lysted_purchases (
    id TEXT PRIMARY KEY,
    order_id TEXT,
    order_date TEXT,
    event_name TEXT,
    event_date TEXT,
    event_date_iso TEXT,
    venue TEXT,
    section TEXT,
    row_label TEXT,
    qty INTEGER,
    seats TEXT,
    delivery_type TEXT,
    account_email TEXT,
    transaction_id TEXT,
    total_cost REAL,
    cost_per_unit REAL,
    status TEXT,
    last_seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS viagogo_listings (
    id TEXT PRIMARY KEY,
    event_id TEXT,
    event_name TEXT,
    event_date TEXT,
    event_date_iso TEXT,
    venue TEXT,
    section TEXT,
    ticket_type TEXT,
    visibility TEXT,
    face_value REAL,
    price REAL,
    proceeds REAL,
    available INTEGER,
    sold INTEGER,
    published INTEGER,
    list_state TEXT,
    last_seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS viagogo_pricer_config (
    listing_id TEXT PRIMARY KEY,
    enabled INTEGER NOT NULL DEFAULT 0,
    floor_price REAL,
    allow_raise INTEGER NOT NULL DEFAULT 0,
    paused INTEGER NOT NULL DEFAULT 0,
    paused_reason TEXT,
    paused_at TEXT,
    last_set_price REAL,
    last_set_at TEXT,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS viagogo_pricer_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id TEXT NOT NULL,
    event_id TEXT,
    section TEXT,
    old_price REAL,
    new_price REAL,
    competitor_price REAL,
    action TEXT NOT NULL,
    detail TEXT,
    dry_run INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pricer_log_listing
    ON viagogo_pricer_log(listing_id, created_at);
CREATE TABLE IF NOT EXISTS viagogo_market_snapshots (
    event_id TEXT PRIMARY KEY,
    fetched_at TEXT NOT NULL,
    rows_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS inventory_hidden (
    source TEXT NOT NULL,
    source_id TEXT NOT NULL,
    hidden_at TEXT NOT NULL,
    PRIMARY KEY (source, source_id)
);
CREATE TABLE IF NOT EXISTS inventory_unsold (
    fingerprint TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    source_id TEXT,
    event_name TEXT,
    event_date TEXT,
    event_date_iso TEXT,
    venue TEXT,
    section TEXT,
    row_label TEXT,
    seats TEXT,
    qty INTEGER,
    cost REAL,
    cost_per_unit REAL,
    list_price REAL,
    delivery_type TEXT,
    marked_at TEXT NOT NULL,
    note TEXT
);
CREATE TABLE IF NOT EXISTS sales_hidden (
    source TEXT NOT NULL,
    sale_id TEXT NOT NULL,
    hidden_at TEXT NOT NULL,
    PRIMARY KEY (source, sale_id)
);
CREATE TABLE IF NOT EXISTS sales_canceled (
    source TEXT NOT NULL,
    sale_id TEXT NOT NULL,
    canceled_at TEXT NOT NULL,
    reason TEXT,
    PRIMARY KEY (source, sale_id)
);
CREATE TABLE IF NOT EXISTS event_group_merges (
    -- raw_group_key is the auto-clustered group key Kartis would assign on
    -- its own (norm_name|iso|norm_venue). When the user merges two or more
    -- groups, every raw key in that merge gets a row pointing to the
    -- canonical_group_key — a single shared key across all merged groups.
    raw_group_key TEXT PRIMARY KEY,
    canonical_group_key TEXT NOT NULL,
    canonical_event_name TEXT NOT NULL,
    canonical_event_date TEXT,
    canonical_event_date_iso TEXT,
    canonical_venue TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_event_group_merges_canonical
    ON event_group_merges(canonical_group_key);
CREATE TABLE IF NOT EXISTS inventory_overrides (
    source TEXT NOT NULL,
    source_id TEXT NOT NULL,
    field TEXT NOT NULL,
    value TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (source, source_id, field)
);
CREATE TABLE IF NOT EXISTS sales_overrides (
    source TEXT NOT NULL,
    sale_id TEXT NOT NULL,
    field TEXT NOT NULL,
    value TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (source, sale_id, field)
);
CREATE TABLE IF NOT EXISTS manual_inventory (
    id TEXT PRIMARY KEY,
    event_name TEXT,
    event_date TEXT,
    event_date_iso TEXT,
    venue TEXT,
    section TEXT,
    row_label TEXT,
    seats TEXT,
    qty INTEGER,
    cost_per_unit REAL,
    note TEXT,
    matched_source TEXT,
    matched_source_id TEXT,
    matched_at TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS manual_sales (
    id TEXT PRIMARY KEY,
    inv_source TEXT,
    inv_source_id TEXT,
    event_name TEXT,
    event_date TEXT,
    event_date_iso TEXT,
    venue TEXT,
    section TEXT,
    row_label TEXT,
    seats TEXT,
    qty INTEGER,
    sale_price REAL,
    cost REAL,
    sale_date TEXT,
    sale_date_iso TEXT,
    platform TEXT,
    is_loss INTEGER DEFAULT 0,
    note TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS inventory_matches (
    sale_source TEXT NOT NULL,
    sale_id TEXT NOT NULL,
    inv_source TEXT NOT NULL,
    inv_source_id TEXT NOT NULL,
    qty_matched INTEGER,
    matched_at TEXT NOT NULL,
    match_reason TEXT,
    PRIMARY KEY (sale_source, sale_id)
);
CREATE TABLE IF NOT EXISTS match_blocklist (
    sale_source TEXT NOT NULL,
    sale_id TEXT NOT NULL,
    blocked_at TEXT NOT NULL,
    PRIMARY KEY (sale_source, sale_id)
);
CREATE TABLE IF NOT EXISTS lysted_sales (
    id TEXT PRIMARY KEY,
    order_id TEXT,
    sale_date TEXT,
    sale_date_iso TEXT,
    event_name TEXT,
    event_date TEXT,
    event_date_iso TEXT,
    venue TEXT,
    section TEXT,
    row_label TEXT,
    qty INTEGER,
    seats TEXT,
    delivery_type TEXT,
    sale_price REAL,
    payout REAL,
    fees REAL,
    cost REAL,
    status TEXT,
    raw_cells TEXT,
    last_seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS viagogo_sales (
    id TEXT PRIMARY KEY,
    order_id TEXT,
    sale_date TEXT,
    sale_date_iso TEXT,
    event_name TEXT,
    event_date TEXT,
    event_date_iso TEXT,
    venue TEXT,
    section TEXT,
    row_label TEXT,
    seats TEXT,
    qty INTEGER,
    ticket_type TEXT,
    sale_price REAL,
    upload_deadline TEXT,
    tab TEXT,
    status TEXT,
    raw_cells TEXT,
    last_seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS crowdvolt_sales (
    id TEXT PRIMARY KEY,
    order_id TEXT,
    sale_date TEXT,
    sale_date_iso TEXT,
    event_name TEXT,
    event_date TEXT,
    event_date_iso TEXT,
    venue TEXT,
    qty INTEGER,
    ticket_type TEXT,
    price_per_ticket REAL,
    sale_price REAL,
    status TEXT,
    raw_cells TEXT,
    last_seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS jerujam_tickets (
    id TEXT PRIMARY KEY,
    event_name TEXT,
    event_date TEXT,
    event_date_iso TEXT,
    venue TEXT,
    city TEXT,
    section TEXT,
    row_label TEXT,
    seat_numbers TEXT,
    quantity INTEGER,
    cost_per_ticket REAL,
    total_purchase_cost REAL,
    purchase_platform TEXT,
    purchase_account TEXT,
    purchase_date TEXT,
    listing_price REAL,
    listing_platform TEXT,
    sold_price REAL,
    sold_count INTEGER,
    sale_date TEXT,
    seller_fees REAL,
    status TEXT,
    notes TEXT,
    credit_card TEXT,
    created_at TEXT,
    updated_at TEXT,
    last_seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS jerujam_sales (
    id TEXT PRIMARY KEY,
    ticket_id TEXT NOT NULL,
    quantity INTEGER,
    sale_price REAL,
    sale_date TEXT,
    platform TEXT,
    created_at TEXT,
    last_seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS expense_subscriptions (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    category TEXT,
    amount REAL NOT NULL,
    started_at_iso TEXT NOT NULL,
    ended_at_iso TEXT,
    notes TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS expenses (
    id TEXT PRIMARY KEY,
    date TEXT,
    date_iso TEXT NOT NULL,
    vendor TEXT,
    category TEXT,
    amount REAL NOT NULL,
    subscription_id TEXT,
    month_key TEXT,
    notes TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(subscription_id, month_key)
);
CREATE TABLE IF NOT EXISTS owed_items (
    id TEXT PRIMARY KEY,
    date_iso TEXT NOT NULL,
    amount REAL NOT NULL,
    description TEXT,
    card_account TEXT,
    direction TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cashback_entries (
    id TEXT PRIMARY KEY,
    date_iso TEXT NOT NULL,
    amount REAL NOT NULL,
    card_name TEXT NOT NULL,
    source_ref TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cashback_date ON cashback_entries(date_iso);
CREATE TABLE IF NOT EXISTS kupat_credits (
    id TEXT PRIMARY KEY,
    issued_date TEXT NOT NULL,
    ils_amount REAL NOT NULL,
    original_usd_cost REAL,
    note TEXT DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_kupat_credits_date ON kupat_credits(issued_date);
CREATE TABLE IF NOT EXISTS kupat_credit_spends (
    id TEXT PRIMARY KEY,
    credit_id TEXT NOT NULL,
    spend_date TEXT NOT NULL,
    ils_amount REAL NOT NULL,
    fx_rate REAL,
    note TEXT DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_kupat_spends_credit ON kupat_credit_spends(credit_id);
CREATE TABLE IF NOT EXISTS maaser_payments (
    id TEXT PRIMARY KEY,
    date TEXT,
    date_iso TEXT NOT NULL,
    recipient TEXT,
    amount REAL NOT NULL,
    notes TEXT,
    tax_deductible INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS jerujam_expenses (
    id TEXT PRIMARY KEY,
    date TEXT,
    vendor TEXT,
    category TEXT,
    description TEXT,
    amount REAL,
    notes TEXT,
    created_at TEXT,
    last_seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pending_intake (
    id TEXT PRIMARY KEY,
    message_id TEXT UNIQUE,
    provider TEXT,
    email_from TEXT,
    email_subject TEXT,
    email_received_at TEXT,
    event_name TEXT,
    event_date_iso TEXT,
    venue TEXT,
    section TEXT,
    row_label TEXT,
    seats TEXT,
    qty INTEGER,
    cost REAL,
    cost_per_unit REAL,
    raw_text TEXT,
    parse_warnings TEXT,
    ticket_url TEXT,
    buyer_email TEXT,
    status TEXT NOT NULL DEFAULT 'new',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pending_intake_status ON pending_intake(status, created_at);
CREATE TABLE IF NOT EXISTS attachments (
    id TEXT PRIMARY KEY,
    owner_type TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    filename TEXT NOT NULL,
    stored_path TEXT NOT NULL,
    size_bytes INTEGER,
    content_type TEXT,
    uploaded_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_attachments_owner ON attachments(owner_type, owner_id);
CREATE TABLE IF NOT EXISTS tm_watchers (
    id TEXT PRIMARY KEY,
    label TEXT,
    event_code TEXT NOT NULL,
    perf_code TEXT NOT NULL,
    paused INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    last_check_at TEXT,
    last_check_error TEXT,
    last_seat_count INTEGER,
    UNIQUE(event_code, perf_code)
);
CREATE TABLE IF NOT EXISTS tm_seat_state (
    watcher_id TEXT NOT NULL,
    seat_key TEXT NOT NULL,
    block TEXT,
    row_label TEXT,
    seat_num TEXT,
    PRIMARY KEY (watcher_id, seat_key)
);
CREATE TABLE IF NOT EXISTS tm_drops (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    watcher_id TEXT NOT NULL,
    detected_at TEXT NOT NULL,
    added_count INTEGER NOT NULL,
    removed_count INTEGER NOT NULL,
    seats_json TEXT,
    notify_result TEXT
);
CREATE INDEX IF NOT EXISTS idx_tm_drops_watcher ON tm_drops(watcher_id, detected_at DESC);
-- Per-seat notification cool-down. Some venues (kupat VIP seats especially)
-- flap a hot seat in and out of the buyable feed every few minutes as it
-- cycles through carts/holds, so the plain add/remove diff would re-ping the
-- same seat all day. We stamp the last time each (watcher, seat) actually
-- pinged and suppress re-pings inside the cool-down window. Physical seats
-- only — status pseudo-seats (GA/festival flips) are never cooled down.
CREATE TABLE IF NOT EXISTS tm_seat_cooldown (
    watcher_id TEXT NOT NULL,
    seat_key TEXT NOT NULL,
    last_notified_at TEXT NOT NULL,
    PRIMARY KEY (watcher_id, seat_key)
);
-- Periodic snapshots of GA / count-tracked events' availability (tickchak
-- festival hub events AND kupat GA events). Lets the Festival / GA Tracker
-- pages compute "sold in the last hour / 6h / 24h / 3d / 7d" as deltas in
-- `available` between snapshots (the DB otherwise only holds current state).
-- capacity/sold are NULL for sources that only expose tickets-left (kupat GA).
CREATE TABLE IF NOT EXISTS tickchak_sales_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT,
    event_code TEXT NOT NULL,
    perf_code TEXT,
    captured_at TEXT NOT NULL,
    capacity INTEGER,
    available INTEGER,
    sold INTEGER
);
CREATE INDEX IF NOT EXISTS idx_tcsnap ON tickchak_sales_snapshots(event_code, captured_at);
CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TEXT NOT NULL
);
-- Pacha NYC new-event monitor (pacha_events.py + app.py run_pacha_events).
-- One row per event ever seen on pacha-nyc.com/events; rows persist after
-- the event drops off the page (history) and simply stop being diffed.
CREATE TABLE IF NOT EXISTS pacha_seen_events (
    event_id     TEXT PRIMARY KEY,
    name         TEXT,
    slug         TEXT,
    date_text    TEXT,
    start_date   TEXT,
    on_sale      INTEGER,
    ga_price     REAL,
    ga_sold_out  INTEGER,
    buy_url      TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL,
    ga_release   TEXT,
    ga_available INTEGER,
    ga_quantity  INTEGER,
    total_available INTEGER,          -- sum across currently-open tiers
    sold_cum     INTEGER,             -- lifetime sold SINCE sold_cum_since:
                                      -- accumulated availability drops between
                                      -- ticks (release flips add inventory and
                                      -- are clamped out, same as _sales_windows)
    sold_cum_since TEXT,
    tiers_json   TEXT,                -- current tier breakdown as of last tick
    vip_price    REAL                 -- headline VIP price (price-drop shocks
                                      -- diff it alongside ga_price)
);
-- One row per release (ticket tier) per pacha event, from first sighting to
-- sell-out/removal — the supply-side history the live page discards. Keyed
-- on the tier NAME (carries the release number); releases already open when
-- logging started have first_seen_at = that deploy's first tick.
CREATE TABLE IF NOT EXISTS pacha_release_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id     TEXT NOT NULL,
    tier_name    TEXT NOT NULL,
    price        REAL,
    quantity     INTEGER,              -- latest allocation seen
    available_last INTEGER,
    used_last    INTEGER,
    first_seen_at TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL,
    sold_out_at  TEXT,                 -- first tick at 0 left, or when the
                                       -- tier vanished (available_last > 0
                                       -- then means removed, not sold out)
    UNIQUE(event_id, tier_name)
);
-- DICE.fm tier/price change log (dice.py + market.py sweep + /dice page).
-- Append-only: one row per STATE a ticket type has been in (tier index,
-- price, status). A new row is inserted only when the state differs from
-- the type's latest row, so the table doubles as the change history the
-- API itself never exposes (DICE shows only the current tier). ended_at
-- is stamped on the previous row when it's superseded or the type
-- disappears from the API response.
CREATE TABLE IF NOT EXISTS dice_tier_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_code   TEXT NOT NULL,          -- 24-hex internal DICE id
    type_name    TEXT NOT NULL,          -- "General Admission"
    tier_index   INTEGER,
    tier_name    TEXT,                   -- "Second Release" (often null)
    price        REAL,                   -- per ticket, event currency
    currency     TEXT,
    status       TEXT,                   -- on-sale / off-sale / sold-out…
    first_seen_at TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL,
    ended_at     TEXT                    -- null = this is the current state
);
CREATE INDEX IF NOT EXISTS idx_dice_tier_log_event ON dice_tier_log(event_code, type_name, id);
-- Israeli-sites new-event monitor (kupat_events.py / tm_events.py +
-- app.py run_il_events). One row per (source, event) ever seen on the
-- site's listing feed; rows persist after the event drops off the feed
-- (history) and simply stop being diffed. on_sale records whether the
-- sale had opened when last seen — the 0→1 transition is the ping.
CREATE TABLE IF NOT EXISTS site_seen_events (
    source        TEXT NOT NULL,
    event_key     TEXT NOT NULL,
    name          TEXT,
    venue         TEXT,
    date_text     TEXT,
    first_date_ms INTEGER,
    on_sale       INTEGER,
    url           TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL,
    -- JSON list of every performance/date key ever seen under this event
    -- (kupat only; NULL elsewhere). Grows monotonically — a new key under
    -- a known event is the 'newdate' ping. NULL = no baseline yet, so the
    -- first tick after deploy stores silently instead of ping-flooding.
    perfs_json    TEXT,
    PRIMARY KEY (source, event_key)
);
-- Market-wide tracker (market.py + app.py run_market_sweep). One row per
-- (source, event, performance) the hourly sweep has ever seen. Latest
-- availability / min-price are denormalized here; the availability history
-- that powers the sold-per-window columns lives in tickchak_sales_snapshots
-- under the same (source, event_code, perf_code) key.
CREATE TABLE IF NOT EXISTS market_events (
    source        TEXT NOT NULL,          -- kupat | tm | tickchak | zappa | pacha
    event_code    TEXT NOT NULL,
    perf_code     TEXT NOT NULL DEFAULT '0',
    name          TEXT,
    venue         TEXT,
    date_text     TEXT,
    first_date_ms INTEGER,
    url           TEXT,
    status        TEXT,                   -- available|lasttickets|soldout|closed|past|waitlist|unknown
    capacity      INTEGER,                -- NULL where the site exposes no total
    available     INTEGER,
    min_price     REAL,                   -- cheapest currently-buyable tier, in `currency`
    manual        INTEGER NOT NULL DEFAULT 0,
    first_seen_at TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL,
    last_error    TEXT,
    currency      TEXT DEFAULT 'ILS',     -- ISO code; pacha rows are USD
    PRIMARY KEY (source, event_code, perf_code)
);
-- Tickchak has no global catalog — the user pastes event/hub URLs on the
-- /market page and the sweep expands hubs into their member events.
CREATE TABLE IF NOT EXISTS market_manual (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source   TEXT NOT NULL DEFAULT 'tickchak',
    kind     TEXT NOT NULL DEFAULT 'event',   -- 'event' | 'hub'
    code     TEXT NOT NULL,                   -- event slug/id, or hub slug
    label    TEXT,
    added_at TEXT NOT NULL,
    UNIQUE(source, kind, code)
);
CREATE INDEX IF NOT EXISTS idx_tcsnap_full
    ON tickchak_sales_snapshots(source, event_code, perf_code, captured_at);
-- /market hide list: noise events the user snoozed off the page (water
-- parks, kids' attractions, …). Display-only — the sweep keeps snapshotting
-- them, so history is intact if they're unhidden. Keyed per EVENT so one
-- hide covers all of that event's dates. hidden_until NULL = forever;
-- expired rows are pruned lazily by market_hidden_active().
CREATE TABLE IF NOT EXISTS market_hidden (
    source       TEXT NOT NULL,
    event_code   TEXT NOT NULL,
    name         TEXT,
    hidden_until TEXT,
    created_at   TEXT NOT NULL,
    PRIMARY KEY (source, event_code)
);
-- /vault account manager. *_enc columns are Fernet blobs (KARTIS_VAULT_KEY
-- in .env — see vault.py); everything else is plaintext so the list view
-- needs no decryption.
CREATE TABLE IF NOT EXISTS vault_accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform     TEXT NOT NULL,            -- free text; UI seeds the known sites
    label        TEXT DEFAULT '',          -- nickname, e.g. "main", "backup #2"
    username     TEXT DEFAULT '',          -- email/username (copyable)
    phone        TEXT DEFAULT '',          -- phone number on the account (copyable)
    password_enc TEXT DEFAULT '',
    login_url    TEXT DEFAULT '',
    status       TEXT DEFAULT 'active',    -- active | limited | banned | unused
    notes_enc    TEXT DEFAULT '',
    address_enc  TEXT DEFAULT '',
    card_enc     TEXT DEFAULT '',          -- free-text card details blob
    created_at   TEXT,
    updated_at   TEXT
);
-- /tm page: Ticketmaster buying accounts, one row per account with its
-- dedicated proxy + card. Same crypto/PIN regime as vault_accounts.
CREATE TABLE IF NOT EXISTS tm_accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_no     TEXT DEFAULT '',        -- user's own numbering
    email          TEXT DEFAULT '',
    password_enc   TEXT DEFAULT '',
    acct_source    TEXT DEFAULT '',        -- where the account came from
    proxy_provider TEXT DEFAULT '',
    proxy_enc      TEXT DEFAULT '',        -- host:port:user:pass etc.
    card_provider  TEXT DEFAULT '',
    cc_enc         TEXT DEFAULT '',        -- card number
    cc_exp_enc     TEXT DEFAULT '',        -- mm/yy
    cc_cvv_enc     TEXT DEFAULT '',
    address_enc    TEXT DEFAULT '',
    notes          TEXT DEFAULT '',
    created_at     TEXT,
    updated_at     TEXT
);
CREATE TABLE IF NOT EXISTS todos (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    notes TEXT,
    due_date_iso TEXT,
    urgency TEXT NOT NULL DEFAULT 'normal',
    status TEXT NOT NULL DEFAULT 'open',
    linked_source TEXT,
    linked_source_id TEXT,
    tags TEXT,
    amount REAL,
    amount_currency TEXT,
    recurrence TEXT,
    auto_source TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    notified_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_todos_status_due ON todos(status, due_date_iso);
CREATE INDEX IF NOT EXISTS idx_todos_auto_source ON todos(auto_source);
CREATE TABLE IF NOT EXISTS todo_subtasks (
    id TEXT PRIMARY KEY,
    todo_id TEXT NOT NULL,
    title TEXT NOT NULL,
    done INTEGER NOT NULL DEFAULT 0,
    order_idx INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_todo_subtasks_parent ON todo_subtasks(todo_id, order_idx);
CREATE TABLE IF NOT EXISTS todo_suggestion_dismissals (
    suggestion_key TEXT PRIMARY KEY,
    dismissed_until TEXT NOT NULL,
    created_at TEXT NOT NULL
);
-- Kupat section name (Hebrew) -> viagogo section name, per venue. Not a
-- literal translation (e.g. Caesarea's "יציע תחתון 1" lists as viagogo's
-- "Middle Tier 1") so this is a taught lookup, not a hardcoded formula.
CREATE TABLE IF NOT EXISTS viagogo_section_map (
    venue TEXT NOT NULL,
    kupat_section TEXT NOT NULL,
    viagogo_section TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (venue, kupat_section)
);
-- Hebrew Kupat artist/venue name → English viagogo search term.
-- Looked up before every search_event() call so Hebrew-named events match.
-- Auto-populated on approve; user can add rows via the /pending UI.
CREATE TABLE IF NOT EXISTS kupat_name_map (
    hebrew_name TEXT PRIMARY KEY,
    english_name TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
-- One row per Kupat purchase-confirmation email we're trying to push into a
-- draft viagogo listing. status lifecycle:
--   searching -> awaiting_approval -> approved -> creating -> created
--                                  -> rejected
--                       (any stage) -> error
--   no_match  (no candidate viagogo event found)
CREATE TABLE IF NOT EXISTS viagogo_push (
    id TEXT PRIMARY KEY,
    intake_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'searching',
    event_name TEXT,
    venue TEXT,
    event_date_iso TEXT,
    section TEXT,
    row_label TEXT,
    seats TEXT,
    qty INTEGER,
    cost REAL,
    cost_per_unit REAL,
    candidates_json TEXT,
    chosen_event_id TEXT,
    chosen_event_name TEXT,
    chosen_venue TEXT,
    chosen_event_date TEXT,
    viagogo_section TEXT,
    fx_rate REAL,
    cost_usd_per_ticket REAL,
    website_price_usd REAL,
    ticket_url TEXT,
    buyer_email TEXT,
    listing_id TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_viagogo_push_status ON viagogo_push(status, created_at);
CREATE INDEX IF NOT EXISTS idx_viagogo_push_intake ON viagogo_push(intake_id);
-- Market-wide viagogo sales tracker (viagogo_market_sales.py + /vgsales).
-- One row per sale observed in the MarketDataV3 "Sales" grid (all sellers,
-- not just us). The feed shows the last 10 sales with NO dates and NO ids,
-- so observed_at (the tick that first saw the row) is the effective sale
-- time (accurate to the poll interval) and newness is decided by aligning
-- the ordered window against the previous one (viagogo_sales_events.
-- window_json) — content hashing can't work, identical sales are common.
-- baseline=1 marks rows from an event's first-ever window: real sales, but
-- of unknown age, so velocity windows exclude them.
CREATE TABLE IF NOT EXISTS viagogo_market_sales (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id    TEXT NOT NULL,
    section     TEXT,
    row_label   TEXT,
    seats       TEXT,
    qty         INTEGER,
    price       REAL,                    -- USD per ticket
    is_ours     INTEGER NOT NULL DEFAULT 0,  -- tr class owned/current
    baseline    INTEGER NOT NULL DEFAULT 0,
    observed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_vgms_event ON viagogo_market_sales(event_id, observed_at);
-- Events the user pasted on /vgsales to track without holding a listing.
CREATE TABLE IF NOT EXISTS viagogo_sales_watchlist (
    event_id TEXT PRIMARY KEY,
    label    TEXT,
    url      TEXT,
    added_at TEXT NOT NULL
);
-- Per-event fetch bookkeeping + display metadata (name/venue/date parsed
-- from the MarketDataV3 header — works for events we hold no listing on).
CREATE TABLE IF NOT EXISTS viagogo_sales_events (
    event_id      TEXT PRIMARY KEY,
    name          TEXT,
    venue         TEXT,
    event_date    TEXT,
    window_json   TEXT,                  -- last seen ordered sales window
    last_fetch_at TEXT,
    last_error    TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL
);
-- DICE (dice.fm) purchases auto-recorded from forwarded confirmation
-- emails (mail_intake dice branch; parser in dice_email.py). One row per
-- purchase email; held = qty - qty_transferred (derived, never stored).
CREATE TABLE IF NOT EXISTS dice_purchases (
    id              TEXT PRIMARY KEY,          -- dcp-<hex>
    message_id      TEXT UNIQUE,
    account_email   TEXT,                      -- which dice account bought
    event_name      TEXT,
    event_slug      TEXT,                      -- dice.fm/event/<slug> key
    event_date_iso  TEXT,
    venue           TEXT,
    ticket_type     TEXT,
    qty             INTEGER,
    qty_transferred INTEGER NOT NULL DEFAULT 0,
    price_total     REAL,
    price_per_unit  REAL,
    currency        TEXT,
    email_date      TEXT,
    subject         TEXT,
    raw_text        TEXT,
    warnings        TEXT,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_dice_purchases_acct ON dice_purchases(account_email, event_slug);
-- DICE transfer-out confirmations ("Ticket sent to <name>"). purchase_id
-- points at the (first) matched purchase; match_status records how the
-- FIFO matcher fared: matched / unmatched / overflow.
-- Scraped resale-platform sales matched to DICE purchases by the user on
-- /dice ("this viagogo order sold tickets from that purchase"). avail on
-- the page = purchase qty - SUM(linked qty); transfers deliberately play
-- no part in that math (delivery info is often missing). sale_source +
-- sale_id point into viagogo_sales / lysted_sales / crowdvolt_sales.
CREATE TABLE IF NOT EXISTS dice_sale_links (
    id           TEXT PRIMARY KEY,             -- dsl-<hex>
    purchase_id  TEXT NOT NULL,
    sale_source  TEXT NOT NULL,
    sale_id      TEXT NOT NULL,
    qty          INTEGER NOT NULL,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_dice_sale_links_purchase ON dice_sale_links(purchase_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_dice_sale_links_sale
    ON dice_sale_links(sale_source, sale_id, purchase_id);
CREATE TABLE IF NOT EXISTS dice_transfers (
    id             TEXT PRIMARY KEY,           -- dct-<hex>
    message_id     TEXT UNIQUE,
    account_email  TEXT,
    event_name     TEXT,
    event_slug     TEXT,
    event_date_iso TEXT,
    qty            INTEGER,
    recipient      TEXT,
    purchase_id    TEXT,
    match_status   TEXT,
    email_date     TEXT,
    created_at     TEXT NOT NULL
);
-- CrowdVolt auto-pricer (crowdvolt_pricer.py). Mirrors the viagogo pricer
-- trio: a mirror of our active asks (rewritten every tick from the sell_active
-- API), per-ask config, and an audit log. ask_uqid is CrowdVolt's listing
-- id; "section" duty is played by ticket_type (CV has no seat map).
CREATE TABLE IF NOT EXISTS cv_active_listings (
    ask_uqid       TEXT PRIMARY KEY,
    event_uqid     TEXT NOT NULL,
    event_name     TEXT,
    event_date     TEXT,
    event_location TEXT,
    ticket_type    TEXT,
    price          REAL,
    qty            INTEGER,
    all_in_price   REAL,
    last_seen_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cv_pricer_config (
    ask_uqid       TEXT PRIMARY KEY,
    enabled        INTEGER NOT NULL DEFAULT 0,
    floor_price    REAL,
    allow_raise    INTEGER NOT NULL DEFAULT 0,
    paused         INTEGER NOT NULL DEFAULT 0,
    paused_reason  TEXT,
    paused_at      TEXT,
    last_set_price REAL,
    last_set_at    TEXT,
    no_drop_cap    INTEGER NOT NULL DEFAULT 0,
    updated_at     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cv_pricer_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ask_uqid TEXT NOT NULL,
    event_uqid TEXT,
    ticket_type TEXT,
    old_price REAL,
    new_price REAL,
    competitor_price REAL,
    action TEXT NOT NULL,
    detail TEXT,
    dry_run INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cv_pricer_log_ask
    ON cv_pricer_log(ask_uqid, created_at);
CREATE TABLE IF NOT EXISTS cv_market_snapshots (
    event_uqid TEXT PRIMARY KEY,
    fetched_at TEXT NOT NULL,
    rows_json  TEXT NOT NULL
);
"""


@contextmanager
def connect():
    # timeout: with many writers (60s watcher tick, 1-min pacha, pricers,
    # user requests) the sqlite default of 5s loses races against a big
    # commit and surfaces as "database is locked" 500s (2026-08-12 outage).
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        # WAL lets readers and writers proceed concurrently instead of
        # queueing on one rollback-journal lock. Persistent in the DB file —
        # after the first flip this pragma is a cheap no-op read.
        # synchronous=NORMAL is the recommended WAL pairing (durable through
        # app crashes; only an OS crash can lose the last commits).
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
    except sqlite3.OperationalError:
        pass  # flipping WAL needs a quiet instant; reads still work — retry next connect
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init():
    with connect() as conn:
        conn.executescript(SCHEMA)
        for _sql in (
            "ALTER TABLE pending_intake ADD COLUMN ticket_url TEXT",
            "ALTER TABLE viagogo_push ADD COLUMN ticket_url TEXT",
            "ALTER TABLE pending_intake ADD COLUMN buyer_email TEXT",
            "ALTER TABLE viagogo_push ADD COLUMN buyer_email TEXT",
        ):
            try:
                conn.executescript(_sql)
            except Exception:
                pass
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(inventory)").fetchall()}
        if "event_date_iso" not in cols:
            conn.execute("ALTER TABLE inventory ADD COLUMN event_date_iso TEXT")
        if "stubhub_url" not in cols:
            conn.execute("ALTER TABLE inventory ADD COLUMN stubhub_url TEXT")
        vg_cols = {row["name"] for row in conn.execute("PRAGMA table_info(viagogo_listings)").fetchall()}
        if "published" not in vg_cols:
            conn.execute("ALTER TABLE viagogo_listings ADD COLUMN published INTEGER")
        if "list_state" not in vg_cols:
            conn.execute("ALTER TABLE viagogo_listings ADD COLUMN list_state TEXT")
        ls_cols = {row["name"] for row in conn.execute("PRAGMA table_info(lysted_sales)").fetchall()}
        if "cost" not in ls_cols:
            conn.execute("ALTER TABLE lysted_sales ADD COLUMN cost REAL")
        va_cols = {row["name"] for row in conn.execute("PRAGMA table_info(vault_accounts)").fetchall()}
        if "phone" not in va_cols:
            conn.execute("ALTER TABLE vault_accounts ADD COLUMN phone TEXT DEFAULT ''")
        tma_cols = {row["name"] for row in conn.execute("PRAGMA table_info(tm_accounts)").fetchall()}
        for _c in ("cc_exp_enc", "cc_cvv_enc"):
            if _c not in tma_cols:
                conn.execute(f"ALTER TABLE tm_accounts ADD COLUMN {_c} TEXT DEFAULT ''")
        mi_cols = {row["name"] for row in conn.execute("PRAGMA table_info(manual_inventory)").fetchall()}
        if "email" not in mi_cols:
            conn.execute("ALTER TABLE manual_inventory ADD COLUMN email TEXT")
        # tm_watchers grew multi-source + per-watcher mute + channel routing.
        # SQLite can't add NOT NULL with default to existing rows in one shot,
        # so we add nullable columns and backfill.
        tw_cols = {row["name"] for row in conn.execute("PRAGMA table_info(tm_watchers)").fetchall()}
        if "source" not in tw_cols:
            conn.execute("ALTER TABLE tm_watchers ADD COLUMN source TEXT")
            conn.execute("UPDATE tm_watchers SET source = 'ticketmaster' WHERE source IS NULL")
        if "muted" not in tw_cols:
            conn.execute("ALTER TABLE tm_watchers ADD COLUMN muted INTEGER NOT NULL DEFAULT 0")
        if "notify_channels" not in tw_cols:
            conn.execute("ALTER TABLE tm_watchers ADD COLUMN notify_channels TEXT")
            conn.execute("UPDATE tm_watchers SET notify_channels = 'discord,email' WHERE notify_channels IS NULL")
        if "filters" not in tw_cols:
            # JSON blob: {min_group_size, exclude_sections, min_price, max_price}.
            # Existing watchers stay unfiltered (NULL) so behavior doesn't
            # change under them; new watchers get min_group_size=2 from the
            # insert path's default.
            conn.execute("ALTER TABLE tm_watchers ADD COLUMN filters TEXT")
        if "discord_channel" not in tw_cols:
            # Per-event Discord routing: name of the (auto-created) channel
            # this watcher's DROP pings go to. NULL = shared drops channel.
            # Watchers for the same artist share a name → share a channel.
            conn.execute("ALTER TABLE tm_watchers ADD COLUMN discord_channel TEXT")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS discord_event_channels (
                name TEXT PRIMARY KEY,
                channel_id TEXT,
                webhook_url TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        td_cols = {row["name"] for row in conn.execute("PRAGMA table_info(tm_drops)").fetchall()}
        if "notify_count" not in td_cols:
            conn.execute("ALTER TABLE tm_drops ADD COLUMN notify_count INTEGER")
        # Pricer cross-section competition: JSON array of normalized section
        # names this listing undercuts. NULL = same-section only.
        sse_cols = {row["name"] for row in conn.execute("PRAGMA table_info(site_seen_events)").fetchall()}
        if "perfs_json" not in sse_cols:
            conn.execute("ALTER TABLE site_seen_events ADD COLUMN perfs_json TEXT")
        pc_cols = {row["name"] for row in conn.execute("PRAGMA table_info(viagogo_pricer_config)").fetchall()}
        if "compete_sections" not in pc_cols:
            conn.execute("ALTER TABLE viagogo_pricer_config ADD COLUMN compete_sections TEXT")
        # Per-listing picks: JSON arrays of {s: section, r: row, q: qty}
        # fingerprints (competitor rows carry no id in MarketDataV3).
        # include = individually ticked listings outside the selected
        # sections; exclude = unticked listings inside them.
        if "compete_include" not in pc_cols:
            conn.execute("ALTER TABLE viagogo_pricer_config ADD COLUMN compete_include TEXT")
        if "compete_exclude" not in pc_cols:
            conn.execute("ALTER TABLE viagogo_pricer_config ADD COLUMN compete_exclude TEXT")
        if "no_drop_cap" not in pc_cols:
            conn.execute("ALTER TABLE viagogo_pricer_config ADD COLUMN no_drop_cap INTEGER NOT NULL DEFAULT 0")
        # Sales snapshots grew multi-source (kupat GA alongside tickchak
        # festival): add source + perf_code and backfill the tickchak rows
        # (perf_code is always '0' for tickchak).
        ss_cols = {row["name"] for row in conn.execute("PRAGMA table_info(tickchak_sales_snapshots)").fetchall()}
        if "source" not in ss_cols:
            conn.execute("ALTER TABLE tickchak_sales_snapshots ADD COLUMN source TEXT")
            conn.execute("UPDATE tickchak_sales_snapshots SET source = 'tickchak' WHERE source IS NULL")
        if "perf_code" not in ss_cols:
            conn.execute("ALTER TABLE tickchak_sales_snapshots ADD COLUMN perf_code TEXT")
            conn.execute("UPDATE tickchak_sales_snapshots SET perf_code = '0' WHERE perf_code IS NULL")
        # Composite index created here (not in SCHEMA) so it lands AFTER the
        # source/perf_code columns exist on pre-existing tables.
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tcsnap2 ON "
            "tickchak_sales_snapshots(source, event_code, perf_code, captured_at)"
        )
        # market_events grew a currency column when pacha (USD) joined the
        # ILS-only sources.
        me_cols = {row["name"] for row in conn.execute("PRAGMA table_info(market_events)").fetchall()}
        if "currency" not in me_cols:
            conn.execute("ALTER TABLE market_events ADD COLUMN currency TEXT DEFAULT 'ILS'")
            conn.execute("UPDATE market_events SET currency = 'ILS' WHERE currency IS NULL")
        # Pacha monitor grew GA-release availability tracking (low-stock
        # alerts): the current release's name + tickets-left counts.
        pe_cols = {row["name"] for row in conn.execute("PRAGMA table_info(pacha_seen_events)").fetchall()}
        if "ga_release" not in pe_cols:
            conn.execute("ALTER TABLE pacha_seen_events ADD COLUMN ga_release TEXT")
        if "ga_available" not in pe_cols:
            conn.execute("ALTER TABLE pacha_seen_events ADD COLUMN ga_available INTEGER")
        if "ga_quantity" not in pe_cols:
            conn.execute("ALTER TABLE pacha_seen_events ADD COLUMN ga_quantity INTEGER")
        # Cumulative sold-since-tracking (accumulated availability drops).
        if "total_available" not in pe_cols:
            conn.execute("ALTER TABLE pacha_seen_events ADD COLUMN total_available INTEGER")
        if "sold_cum" not in pe_cols:
            conn.execute("ALTER TABLE pacha_seen_events ADD COLUMN sold_cum INTEGER")
        if "sold_cum_since" not in pe_cols:
            conn.execute("ALTER TABLE pacha_seen_events ADD COLUMN sold_cum_since TEXT")
        if "tiers_json" not in pe_cols:
            conn.execute("ALTER TABLE pacha_seen_events ADD COLUMN tiers_json TEXT")
        # Price-drop shocks diff the VIP headline too, so it's now persisted
        # alongside ga_price (NULL until the first post-deploy tick = silent
        # VIP baseline, same as GA's truthiness guard).
        if "vip_price" not in pe_cols:
            conn.execute("ALTER TABLE pacha_seen_events ADD COLUMN vip_price REAL")
        # Maaser tax-deductible flag — existing rows default to 0 (not
        # claimed) which is a safe default; user can edit any back-history
        # entries to flip them on.
        mp_cols = {row["name"] for row in conn.execute("PRAGMA table_info(maaser_payments)").fetchall()}
        if "tax_deductible" not in mp_cols:
            conn.execute("ALTER TABLE maaser_payments ADD COLUMN tax_deductible INTEGER NOT NULL DEFAULT 0")
        # Cash-back entries auto-captured from Capital One emails carry a
        # source_ref (the rewards order number) so re-polls don't duplicate
        # them. Manual entries leave it NULL; the partial unique index ignores
        # NULLs so it never conflicts with hand-added rows.
        # Where each DICE purchase's tickets are listed for resale — JSON
        # {"viagogo": 2, "crowdvolt": 4, "other": 0}, user-edited on /dice.
        dp_cols = {row["name"] for row in conn.execute("PRAGMA table_info(dice_purchases)").fetchall()}
        if "listed_json" not in dp_cols:
            conn.execute("ALTER TABLE dice_purchases ADD COLUMN listed_json TEXT")
        cb_cols = {row["name"] for row in conn.execute("PRAGMA table_info(cashback_entries)").fetchall()}
        if "source_ref" not in cb_cols:
            conn.execute("ALTER TABLE cashback_entries ADD COLUMN source_ref TEXT")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_cashback_source_ref "
            "ON cashback_entries(source_ref) WHERE source_ref IS NOT NULL"
        )


def upsert_lysted_purchases(rows, now_iso):
    with connect() as conn:
        for r in rows:
            conn.execute(
                """
                INSERT INTO lysted_purchases (id, order_id, order_date, event_name,
                                              event_date, event_date_iso, venue,
                                              section, row_label, qty, seats,
                                              delivery_type, account_email,
                                              transaction_id, total_cost,
                                              cost_per_unit, status, last_seen_at)
                VALUES (:id, :order_id, :order_date, :event_name,
                        :event_date, :event_date_iso, :venue,
                        :section, :row_label, :qty, :seats,
                        :delivery_type, :account_email,
                        :transaction_id, :total_cost,
                        :cost_per_unit, :status, :last_seen_at)
                ON CONFLICT(id) DO UPDATE SET
                    order_id=excluded.order_id,
                    order_date=excluded.order_date,
                    event_name=excluded.event_name,
                    event_date=excluded.event_date,
                    event_date_iso=excluded.event_date_iso,
                    venue=excluded.venue,
                    section=excluded.section,
                    row_label=excluded.row_label,
                    qty=excluded.qty,
                    seats=excluded.seats,
                    delivery_type=excluded.delivery_type,
                    account_email=excluded.account_email,
                    transaction_id=excluded.transaction_id,
                    total_cost=excluded.total_cost,
                    cost_per_unit=excluded.cost_per_unit,
                    status=excluded.status,
                    last_seen_at=excluded.last_seen_at
                """,
                {**r, "last_seen_at": now_iso},
            )


def all_lysted_purchases():
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM lysted_purchases "
            "ORDER BY event_date_iso IS NULL, event_date_iso, event_name"
        ).fetchall()
    return [dict(r) for r in rows]


def has_dice_message(message_id):
    """Message-ID dedup for the dice intake branch — covers both tables so a
    re-poll never double-records a purchase OR re-applies a transfer."""
    if not message_id:
        return False
    with connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM dice_purchases WHERE message_id = ? "
            "UNION SELECT 1 FROM dice_transfers WHERE message_id = ? LIMIT 1",
            (message_id, message_id),
        ).fetchone()
    return row is not None


def insert_dice_purchase(row, now_iso):
    with connect() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO dice_purchases (id, message_id, account_email,
                event_name, event_slug, event_date_iso, venue, ticket_type,
                qty, price_total, price_per_unit, currency, email_date,
                subject, raw_text, warnings, created_at)
            VALUES (:id, :message_id, :account_email, :event_name, :event_slug,
                    :event_date_iso, :venue, :ticket_type, :qty, :price_total,
                    :price_per_unit, :currency, :email_date, :subject,
                    :raw_text, :warnings, :created_at)
            """,
            {**row, "created_at": now_iso},
        )


def insert_dice_transfer(row, now_iso):
    with connect() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO dice_transfers (id, message_id, account_email,
                event_name, event_slug, event_date_iso, qty, recipient,
                purchase_id, match_status, email_date, created_at)
            VALUES (:id, :message_id, :account_email, :event_name, :event_slug,
                    :event_date_iso, :qty, :recipient, :purchase_id,
                    :match_status, :email_date, :created_at)
            """,
            {**row, "created_at": now_iso},
        )


def dice_purchases_all():
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM dice_purchases "
            "ORDER BY event_date_iso IS NULL, event_date_iso, event_name, email_date"
        ).fetchall()
    return [dict(r) for r in rows]


def dice_transfers_all():
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM dice_transfers ORDER BY email_date DESC, created_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def dice_purchases_open(account_email):
    """Purchases for one account with tickets still held, oldest first —
    the FIFO candidate list for transfer matching."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM dice_purchases WHERE account_email = ? "
            "AND qty IS NOT NULL AND qty_transferred < qty "
            "ORDER BY email_date, created_at",
            ((account_email or "").lower(),),
        ).fetchall()
    return [dict(r) for r in rows]


_DICE_SALE_TABLES = {
    "viagogo": "viagogo_sales",
    "lysted": "lysted_sales",
    "crowdvolt": "crowdvolt_sales",
}


def dice_sale_candidates(days=180):
    """Recent scraped sales from every resale platform, newest first, for
    the /dice match-a-sale picker. Sales already fully linked still appear
    (the UI shows how much of them is used) — qty bookkeeping is the user's
    call, matching is just a pointer."""
    cutoff = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=days)).date().isoformat()
    out = []
    with connect() as conn:
        for source, table in _DICE_SALE_TABLES.items():
            for r in conn.execute(
                f"SELECT id, order_id, event_name, event_date_iso, qty, "
                f"sale_price, sale_date_iso FROM {table} "
                f"WHERE COALESCE(sale_date_iso, '') = '' OR sale_date_iso >= ? "
                f"ORDER BY sale_date_iso DESC", (cutoff,)
            ):
                out.append({**dict(r), "source": source})
        linked = {}
        for r in conn.execute(
            "SELECT sale_source, sale_id, SUM(qty) AS n FROM dice_sale_links "
            "GROUP BY sale_source, sale_id"
        ):
            linked[(r["sale_source"], r["sale_id"])] = r["n"]
    for s in out:
        s["qty_linked"] = linked.get((s["source"], s["id"]), 0)
    out.sort(key=lambda s: s.get("sale_date_iso") or "", reverse=True)
    return out


def dice_linked_qty_by_purchase():
    """{purchase_id: total qty linked to resale-platform sales}. This is the
    "sold" count for a DICE holding on the inventory page: a linked
    Viagogo/CrowdVolt/Lysted sale IS the sale, even before the DICE transfer
    goes out — so transfers deliberately play no part here (same rule the
    /dice availability math uses)."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT purchase_id, SUM(qty) AS n FROM dice_sale_links GROUP BY purchase_id"
        ).fetchall()
    return {r["purchase_id"]: (r["n"] or 0) for r in rows}


def dice_sale_links_by_purchase():
    """{purchase_id: [link rows + sale detail]} for the /dice page."""
    out = {}
    with connect() as conn:
        links = [dict(r) for r in conn.execute(
            "SELECT * FROM dice_sale_links ORDER BY created_at")]
        for l in links:
            table = _DICE_SALE_TABLES.get(l["sale_source"])
            if table:
                sale = conn.execute(
                    f"SELECT order_id, event_name, sale_price, sale_date_iso "
                    f"FROM {table} WHERE id = ?", (l["sale_id"],)
                ).fetchone()
                if sale:
                    l.update(dict(sale))
            out.setdefault(l["purchase_id"], []).append(l)
    return out


def dice_sale_link_add(purchase_id, sale_source, sale_id, qty, now_iso):
    import uuid
    with connect() as conn:
        conn.execute(
            "INSERT INTO dice_sale_links (id, purchase_id, sale_source, sale_id, qty, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("dsl-" + uuid.uuid4().hex[:12], purchase_id, sale_source, sale_id, qty, now_iso),
        )


def dice_sale_link_delete(link_id):
    with connect() as conn:
        cur = conn.execute("DELETE FROM dice_sale_links WHERE id = ?", (link_id,))
        return cur.rowcount > 0


def dice_purchase_set_listed(purchase_id, listed_json):
    """listed_json is a pre-validated JSON string like
    '{"viagogo": 2, "crowdvolt": 4}' (or None to clear). Returns True if a
    row was updated."""
    with connect() as conn:
        cur = conn.execute(
            "UPDATE dice_purchases SET listed_json = ? WHERE id = ?",
            (listed_json, purchase_id),
        )
        return cur.rowcount > 0


def dice_purchase_add_transferred(purchase_id, n):
    with connect() as conn:
        conn.execute(
            "UPDATE dice_purchases SET qty_transferred = qty_transferred + ? WHERE id = ?",
            (n, purchase_id),
        )


def upsert_viagogo(rows, now_iso):
    with connect() as conn:
        for r in rows:
            conn.execute(
                """
                INSERT INTO viagogo_listings (id, event_id, event_name, event_date,
                                              event_date_iso, venue, section,
                                              ticket_type, visibility,
                                              face_value, price, proceeds,
                                              available, sold, published,
                                              list_state, last_seen_at)
                VALUES (:id, :event_id, :event_name, :event_date,
                        :event_date_iso, :venue, :section,
                        :ticket_type, :visibility,
                        :face_value, :price, :proceeds,
                        :available, :sold, :published,
                        :list_state, :last_seen_at)
                ON CONFLICT(id) DO UPDATE SET
                    event_id=excluded.event_id,
                    event_name=excluded.event_name,
                    event_date=excluded.event_date,
                    event_date_iso=excluded.event_date_iso,
                    venue=excluded.venue,
                    section=excluded.section,
                    ticket_type=excluded.ticket_type,
                    visibility=excluded.visibility,
                    face_value=excluded.face_value,
                    price=excluded.price,
                    proceeds=excluded.proceeds,
                    available=excluded.available,
                    sold=excluded.sold,
                    published=excluded.published,
                    list_state=excluded.list_state,
                    last_seen_at=excluded.last_seen_at
                """,
                {**r, "last_seen_at": now_iso},
            )


def all_viagogo():
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM viagogo_listings "
            "ORDER BY event_date_iso IS NULL, event_date_iso, event_name, section"
        ).fetchall()
    return [dict(r) for r in rows]


def viagogo_get(listing_id):
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM viagogo_listings WHERE id = ?", (str(listing_id),)
        ).fetchone()
    return dict(row) if row else None


# --- Viagogo auto-pricer -------------------------------------------------
# Config + audit tables live apart from viagogo_listings so the scrape
# mirror stays a pure rewrite target. listing_id is an FK by convention to
# viagogo_listings.id.

def pricer_config_get(listing_id):
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM viagogo_pricer_config WHERE listing_id = ?",
            (str(listing_id),),
        ).fetchone()
    return dict(row) if row else None


def pricer_config_all():
    with connect() as conn:
        rows = conn.execute("SELECT * FROM viagogo_pricer_config").fetchall()
    return {r["listing_id"]: dict(r) for r in rows}


_PRICER_CONFIG_FIELDS = (
    "enabled", "floor_price", "allow_raise", "paused", "paused_reason",
    "paused_at", "last_set_price", "last_set_at", "compete_sections",
    "compete_include", "compete_exclude", "no_drop_cap",
)


def pricer_config_set(listing_id, fields, now_iso):
    """Upsert a partial field dict for one listing's pricer config."""
    fields = {k: v for k, v in fields.items() if k in _PRICER_CONFIG_FIELDS}
    with connect() as conn:
        existing = conn.execute(
            "SELECT listing_id FROM viagogo_pricer_config WHERE listing_id = ?",
            (str(listing_id),),
        ).fetchone()
        if existing:
            sets = ", ".join(f"{k} = :{k}" for k in fields)
            conn.execute(
                f"UPDATE viagogo_pricer_config SET {sets}, updated_at = :updated_at "
                "WHERE listing_id = :listing_id",
                {**fields, "updated_at": now_iso, "listing_id": str(listing_id)},
            )
        else:
            cols = ["listing_id", *fields.keys(), "updated_at"]
            conn.execute(
                f"INSERT INTO viagogo_pricer_config ({', '.join(cols)}) "
                f"VALUES ({', '.join(':' + c for c in cols)})",
                {**fields, "listing_id": str(listing_id), "updated_at": now_iso},
            )


def pricer_log_add(row, now_iso):
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO viagogo_pricer_log (listing_id, event_id, section,
                                            old_price, new_price, competitor_price,
                                            action, detail, dry_run, created_at)
            VALUES (:listing_id, :event_id, :section,
                    :old_price, :new_price, :competitor_price,
                    :action, :detail, :dry_run, :created_at)
            """,
            {
                "event_id": None, "section": None, "old_price": None,
                "new_price": None, "competitor_price": None, "detail": None,
                "dry_run": 0,
                **row,
                "created_at": now_iso,
            },
        )


def market_snapshot_set(event_id, rows_json, now_iso):
    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO viagogo_market_snapshots "
            "(event_id, fetched_at, rows_json) VALUES (?, ?, ?)",
            (str(event_id), now_iso, rows_json),
        )


def market_snapshot_get(event_id):
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM viagogo_market_snapshots WHERE event_id = ?",
            (str(event_id),),
        ).fetchone()
    return dict(row) if row else None


def pricer_writes_since(listing_id, since_iso):
    """Real (non-dry-run) price changes for one listing since `since_iso`,
    oldest first — the drop guard reconstructs the window baseline from
    these. Includes manual_adopt rows: a user's own price change resets
    the guard's baseline (their cut isn't pricer risk)."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT action, old_price, new_price, created_at FROM viagogo_pricer_log "
            "WHERE listing_id = ? AND dry_run = 0 "
            "AND action IN ('lower', 'floor_clamp', 'rate_clamp', 'raise', 'manual_adopt') "
            "AND created_at >= ? ORDER BY id",
            (str(listing_id), since_iso),
        ).fetchall()
    return [dict(r) for r in rows]


def pricer_log_recent(limit=100, listing_id=None):
    with connect() as conn:
        if listing_id:
            rows = conn.execute(
                "SELECT * FROM viagogo_pricer_log WHERE listing_id = ? "
                "ORDER BY id DESC LIMIT ?",
                (str(listing_id), limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM viagogo_pricer_log ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
    return [dict(r) for r in rows]


def upsert_inventory(rows, now_iso):
    with connect() as conn:
        for r in rows:
            conn.execute(
                """
                INSERT INTO inventory (id, event_name, event_date, event_time,
                                       event_date_iso, venue,
                                       listings_count, tickets_count,
                                       total_cost, total_list, stubhub_url, last_seen_at)
                VALUES (:id, :event_name, :event_date, :event_time,
                        :event_date_iso, :venue,
                        :listings_count, :tickets_count,
                        :total_cost, :total_list, :stubhub_url, :last_seen_at)
                ON CONFLICT(id) DO UPDATE SET
                    event_name=excluded.event_name,
                    event_date=excluded.event_date,
                    event_time=excluded.event_time,
                    event_date_iso=excluded.event_date_iso,
                    venue=excluded.venue,
                    listings_count=excluded.listings_count,
                    tickets_count=excluded.tickets_count,
                    total_cost=excluded.total_cost,
                    total_list=excluded.total_list,
                    stubhub_url=COALESCE(excluded.stubhub_url, inventory.stubhub_url),
                    last_seen_at=excluded.last_seen_at
                """,
                {**r, "stubhub_url": r.get("stubhub_url"), "last_seen_at": now_iso},
            )


def all_inventory():
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM inventory "
            "ORDER BY event_date_iso IS NULL, event_date_iso, event_name"
        ).fetchall()
    return [dict(r) for r in rows]


def hide_inventory(source, source_id, now_iso):
    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO inventory_hidden (source, source_id, hidden_at) VALUES (?, ?, ?)",
            (source, source_id, now_iso),
        )


def unhide_inventory(source, source_id):
    with connect() as conn:
        conn.execute(
            "DELETE FROM inventory_hidden WHERE source = ? AND source_id = ?",
            (source, source_id),
        )


def all_hidden_keys():
    with connect() as conn:
        rows = conn.execute("SELECT source, source_id FROM inventory_hidden").fetchall()
    return {(r["source"], r["source_id"]) for r in rows}


# --- "Didn't Sell" archive ---
# Inventory rows whose status is set to "not sold" (or a variant) get
# archived into inventory_unsold and filtered out of the active inventory
# view. Keyed by a content fingerprint (event + section + row + seats + qty)
# rather than (source, source_id) so the tombstone survives Lysted text
# drift and Viagogo listing-id rotation across resyncs.
import re as _re

_WS_RE = _re.compile(r"\s+")


def _unsold_norm(s):
    return _WS_RE.sub(" ", str(s or "").strip().lower())


def unsold_fingerprint(source, event_name, event_date_iso, section, row_label, seats, qty):
    """Stable across resyncs — no source_id, just content."""
    parts = [
        _unsold_norm(source),
        _unsold_norm(event_name),
        (event_date_iso or "").strip()[:10],
        _unsold_norm(section),
        _unsold_norm(row_label),
        _unsold_norm(seats),
        str(int(qty or 0)),
    ]
    return "|".join(parts)


def mark_inventory_unsold(snap, now_iso):
    """snap should already include 'fingerprint'."""
    with connect() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO inventory_unsold
                (fingerprint, source, source_id, event_name, event_date,
                 event_date_iso, venue, section, row_label, seats, qty,
                 cost, cost_per_unit, list_price, delivery_type, marked_at, note)
            VALUES (:fingerprint, :source, :source_id, :event_name, :event_date,
                    :event_date_iso, :venue, :section, :row_label, :seats, :qty,
                    :cost, :cost_per_unit, :list_price, :delivery_type, :marked_at, :note)
            """,
            {**snap, "marked_at": now_iso},
        )


def unmark_inventory_unsold(fingerprint):
    with connect() as conn:
        conn.execute("DELETE FROM inventory_unsold WHERE fingerprint = ?", (fingerprint,))


def all_unsold():
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM inventory_unsold "
            "ORDER BY event_date_iso IS NULL, event_date_iso, event_name"
        ).fetchall()
    return [dict(r) for r in rows]


def all_unsold_fingerprints():
    with connect() as conn:
        rows = conn.execute("SELECT fingerprint FROM inventory_unsold").fetchall()
    return {r["fingerprint"] for r in rows}


def hide_sale(source, sale_id, now_iso):
    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO sales_hidden (source, sale_id, hidden_at) VALUES (?, ?, ?)",
            (source, sale_id, now_iso),
        )


def unhide_sale(source, sale_id):
    with connect() as conn:
        conn.execute(
            "DELETE FROM sales_hidden WHERE source = ? AND sale_id = ?",
            (source, sale_id),
        )


def all_hidden_sale_keys():
    with connect() as conn:
        rows = conn.execute("SELECT source, sale_id FROM sales_hidden").fetchall()
    return {(r["source"], r["sale_id"]) for r in rows}


# --- Canceled sales archive ---
# Distinct from sales_hidden ("× delete from view"). Canceled means the sale
# was reversed by the platform — the ticket goes back to inventory (which the
# platform's own scrape will pick up again on relist). Excluded from totals
# and surfaced in a separate archive on the sales page.

def cancel_sale(source, sale_id, now_iso, reason=None):
    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO sales_canceled (source, sale_id, canceled_at, reason) "
            "VALUES (?, ?, ?, ?)",
            (source, sale_id, now_iso, reason),
        )


def uncancel_sale(source, sale_id):
    with connect() as conn:
        conn.execute(
            "DELETE FROM sales_canceled WHERE source = ? AND sale_id = ?",
            (source, sale_id),
        )


def all_canceled_sale_keys():
    with connect() as conn:
        rows = conn.execute("SELECT source, sale_id FROM sales_canceled").fetchall()
    return {(r["source"], r["sale_id"]) for r in rows}


def all_canceled_sales():
    with connect() as conn:
        rows = conn.execute(
            "SELECT source, sale_id, canceled_at, reason FROM sales_canceled "
            "ORDER BY canceled_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


# --- Event-group merges ---
# When the user manually merges two or more event groups (different names
# for the same show), each raw group_key gets a row pointing to the same
# canonical group_key + display fields. The grouping logic in app.py
# rewrites raw_group_key → canonical_group_key and overrides the row's
# event_name/date/venue display fields.

def merge_event_groups(raw_group_keys, canonical_group_key,
                      canonical_event_name, canonical_event_date,
                      canonical_event_date_iso, canonical_venue, now_iso):
    with connect() as conn:
        for raw in raw_group_keys:
            conn.execute(
                """
                INSERT OR REPLACE INTO event_group_merges
                    (raw_group_key, canonical_group_key,
                     canonical_event_name, canonical_event_date,
                     canonical_event_date_iso, canonical_venue, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (raw, canonical_group_key, canonical_event_name,
                 canonical_event_date, canonical_event_date_iso,
                 canonical_venue, now_iso),
            )


def unmerge_event_group(raw_group_key):
    """Remove a single raw_group_key from its merge."""
    with connect() as conn:
        conn.execute(
            "DELETE FROM event_group_merges WHERE raw_group_key = ?",
            (raw_group_key,),
        )


def unmerge_canonical(canonical_group_key):
    """Drop the entire merge for a canonical group (all raw keys split apart)."""
    with connect() as conn:
        conn.execute(
            "DELETE FROM event_group_merges WHERE canonical_group_key = ?",
            (canonical_group_key,),
        )


def all_event_group_merges():
    """Returns {raw_group_key: dict_of_canonical_fields}."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT raw_group_key, canonical_group_key, canonical_event_name, "
            "canonical_event_date, canonical_event_date_iso, canonical_venue, "
            "created_at FROM event_group_merges"
        ).fetchall()
    return {r["raw_group_key"]: dict(r) for r in rows}


def set_inv_override(source, source_id, field, value, now_iso):
    with connect() as conn:
        if value in (None, ""):
            conn.execute(
                "DELETE FROM inventory_overrides WHERE source=? AND source_id=? AND field=?",
                (source, source_id, field),
            )
        else:
            conn.execute(
                "INSERT OR REPLACE INTO inventory_overrides (source, source_id, field, value, updated_at) VALUES (?, ?, ?, ?, ?)",
                (source, source_id, field, str(value), now_iso),
            )


def all_inv_overrides():
    with connect() as conn:
        rows = conn.execute("SELECT source, source_id, field, value FROM inventory_overrides").fetchall()
    out = {}
    for r in rows:
        out.setdefault((r["source"], r["source_id"]), {})[r["field"]] = r["value"]
    return out


def set_sale_override(source, sale_id, field, value, now_iso):
    with connect() as conn:
        if value in (None, ""):
            conn.execute(
                "DELETE FROM sales_overrides WHERE source=? AND sale_id=? AND field=?",
                (source, sale_id, field),
            )
        else:
            conn.execute(
                "INSERT OR REPLACE INTO sales_overrides (source, sale_id, field, value, updated_at) VALUES (?, ?, ?, ?, ?)",
                (source, sale_id, field, str(value), now_iso),
            )


def insert_manual_inventory(row, now_iso):
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO manual_inventory (id, event_name, event_date, event_date_iso,
                venue, section, row_label, seats, qty, cost_per_unit, note, email,
                matched_source, matched_source_id, matched_at, created_at)
            VALUES (:id, :event_name, :event_date, :event_date_iso,
                :venue, :section, :row_label, :seats, :qty, :cost_per_unit, :note, :email,
                NULL, NULL, NULL, :created_at)
            """,
            {**row, "created_at": now_iso},
        )


def all_manual_inventory():
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM manual_inventory "
            "ORDER BY event_date_iso IS NULL, event_date_iso, event_name"
        ).fetchall()
    return [dict(r) for r in rows]


def update_manual_inventory(id_, fields):
    if not fields:
        return
    sets = ", ".join(f"{k}=:{k}" for k in fields)
    with connect() as conn:
        conn.execute(f"UPDATE manual_inventory SET {sets} WHERE id=:_id", {**fields, "_id": id_})


def delete_manual_inventory(id_):
    with connect() as conn:
        conn.execute("DELETE FROM manual_inventory WHERE id = ?", (id_,))


def mark_manual_inventory_listed(id_, source, source_id, now_iso):
    with connect() as conn:
        conn.execute(
            "UPDATE manual_inventory SET matched_source=?, matched_source_id=?, matched_at=? WHERE id=?",
            (source, source_id, now_iso, id_),
        )


def insert_manual_sale(row, now_iso):
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO manual_sales (id, inv_source, inv_source_id, event_name,
                event_date, event_date_iso, venue, section, row_label, seats,
                qty, sale_price, cost, sale_date, sale_date_iso, platform,
                is_loss, note, created_at)
            VALUES (:id, :inv_source, :inv_source_id, :event_name,
                :event_date, :event_date_iso, :venue, :section, :row_label, :seats,
                :qty, :sale_price, :cost, :sale_date, :sale_date_iso, :platform,
                :is_loss, :note, :created_at)
            """,
            {**row, "created_at": now_iso},
        )


def all_manual_sales():
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM manual_sales ORDER BY sale_date_iso IS NULL, sale_date_iso DESC, created_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def delete_manual_sale(sale_id):
    with connect() as conn:
        conn.execute("DELETE FROM manual_sales WHERE id = ?", (sale_id,))


def seats_sold_by_inv():
    """Map (inv_source, inv_source_id) -> combined seats string from manual_sales,
    so the pending modal can hide seats that were already recorded as sold."""
    out = {}
    with connect() as conn:
        rows = conn.execute(
            "SELECT inv_source, inv_source_id, seats FROM manual_sales "
            "WHERE seats IS NOT NULL AND seats <> ''"
        ).fetchall()
    for r in rows:
        key = (r["inv_source"], str(r["inv_source_id"]))
        out.setdefault(key, []).append(r["seats"])
    return {k: ", ".join(v) for k, v in out.items()}


def all_sale_overrides():
    with connect() as conn:
        rows = conn.execute("SELECT source, sale_id, field, value FROM sales_overrides").fetchall()
    out = {}
    for r in rows:
        out.setdefault((r["source"], r["sale_id"]), {})[r["field"]] = r["value"]
    return out


def record_match(sale_source, sale_id, inv_source, inv_source_id, qty, reason, now_iso):
    with connect() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO inventory_matches
               (sale_source, sale_id, inv_source, inv_source_id, qty_matched, matched_at, match_reason)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (sale_source, sale_id, inv_source, inv_source_id, qty, now_iso, reason),
        )


def all_matches():
    with connect() as conn:
        rows = conn.execute("SELECT * FROM inventory_matches").fetchall()
    return [dict(r) for r in rows]


def find_match_for_inv(inv_source, inv_source_id):
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM inventory_matches WHERE inv_source = ? AND inv_source_id = ? LIMIT 1",
            (inv_source, inv_source_id),
        ).fetchone()
    return dict(row) if row else None


def delete_match(sale_source, sale_id):
    with connect() as conn:
        conn.execute(
            "DELETE FROM inventory_matches WHERE sale_source = ? AND sale_id = ?",
            (sale_source, sale_id),
        )


def add_blocklist(sale_source, sale_id, now_iso):
    with connect() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO match_blocklist (sale_source, sale_id, blocked_at)
               VALUES (?, ?, ?)""",
            (sale_source, sale_id, now_iso),
        )


def all_blocklist_keys():
    with connect() as conn:
        rows = conn.execute("SELECT sale_source, sale_id FROM match_blocklist").fetchall()
    return {(r["sale_source"], r["sale_id"]) for r in rows}


def upsert_lysted_sales(rows, now_iso):
    with connect() as conn:
        for r in rows:
            conn.execute(
                """
                INSERT INTO lysted_sales (id, order_id, sale_date, sale_date_iso,
                    event_name, event_date, event_date_iso, venue, section,
                    row_label, qty, seats, delivery_type, sale_price, payout,
                    fees, cost, status, raw_cells, last_seen_at)
                VALUES (:id, :order_id, :sale_date, :sale_date_iso,
                    :event_name, :event_date, :event_date_iso, :venue, :section,
                    :row_label, :qty, :seats, :delivery_type, :sale_price, :payout,
                    :fees, :cost, :status, :raw_cells, :last_seen_at)
                ON CONFLICT(id) DO UPDATE SET
                    order_id=excluded.order_id,
                    sale_date=excluded.sale_date,
                    sale_date_iso=excluded.sale_date_iso,
                    event_name=excluded.event_name,
                    event_date=excluded.event_date,
                    event_date_iso=excluded.event_date_iso,
                    venue=excluded.venue,
                    section=excluded.section,
                    row_label=excluded.row_label,
                    qty=excluded.qty,
                    seats=excluded.seats,
                    delivery_type=excluded.delivery_type,
                    sale_price=excluded.sale_price,
                    payout=excluded.payout,
                    fees=excluded.fees,
                    cost=excluded.cost,
                    status=excluded.status,
                    raw_cells=excluded.raw_cells,
                    last_seen_at=excluded.last_seen_at
                """,
                {**r, "last_seen_at": now_iso},
            )


def all_lysted_sales():
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM lysted_sales "
            "ORDER BY sale_date_iso IS NULL, sale_date_iso DESC, event_name"
        ).fetchall()
    return [dict(r) for r in rows]


def upsert_viagogo_sales(rows, now_iso):
    with connect() as conn:
        for r in rows:
            conn.execute(
                """
                INSERT INTO viagogo_sales (id, order_id, sale_date, sale_date_iso,
                    event_name, event_date, event_date_iso, venue, section,
                    row_label, seats, qty, ticket_type, sale_price,
                    upload_deadline, tab, status, raw_cells, last_seen_at)
                VALUES (:id, :order_id, :sale_date, :sale_date_iso,
                    :event_name, :event_date, :event_date_iso, :venue, :section,
                    :row_label, :seats, :qty, :ticket_type, :sale_price,
                    :upload_deadline, :tab, :status, :raw_cells, :last_seen_at)
                ON CONFLICT(id) DO UPDATE SET
                    order_id=excluded.order_id,
                    sale_date=excluded.sale_date,
                    sale_date_iso=excluded.sale_date_iso,
                    event_name=excluded.event_name,
                    event_date=excluded.event_date,
                    event_date_iso=excluded.event_date_iso,
                    venue=excluded.venue,
                    section=excluded.section,
                    row_label=excluded.row_label,
                    seats=excluded.seats,
                    qty=excluded.qty,
                    ticket_type=excluded.ticket_type,
                    sale_price=excluded.sale_price,
                    upload_deadline=excluded.upload_deadline,
                    tab=excluded.tab,
                    status=excluded.status,
                    raw_cells=excluded.raw_cells,
                    last_seen_at=excluded.last_seen_at
                """,
                {**r, "last_seen_at": now_iso},
            )


def all_viagogo_sales():
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM viagogo_sales "
            "ORDER BY sale_date_iso IS NULL, sale_date_iso DESC, event_name"
        ).fetchall()
    return [dict(r) for r in rows]


def upsert_crowdvolt_sales(rows, now_iso):
    with connect() as conn:
        for r in rows:
            conn.execute(
                """
                INSERT INTO crowdvolt_sales (id, order_id, sale_date, sale_date_iso,
                    event_name, event_date, event_date_iso, venue, qty, ticket_type,
                    price_per_ticket, sale_price, status, raw_cells, last_seen_at)
                VALUES (:id, :order_id, :sale_date, :sale_date_iso,
                    :event_name, :event_date, :event_date_iso, :venue, :qty, :ticket_type,
                    :price_per_ticket, :sale_price, :status, :raw_cells, :last_seen_at)
                ON CONFLICT(id) DO UPDATE SET
                    order_id=excluded.order_id,
                    sale_date=excluded.sale_date,
                    sale_date_iso=excluded.sale_date_iso,
                    event_name=excluded.event_name,
                    event_date=excluded.event_date,
                    event_date_iso=excluded.event_date_iso,
                    venue=excluded.venue,
                    qty=excluded.qty,
                    ticket_type=excluded.ticket_type,
                    price_per_ticket=excluded.price_per_ticket,
                    sale_price=excluded.sale_price,
                    status=excluded.status,
                    raw_cells=excluded.raw_cells,
                    last_seen_at=excluded.last_seen_at
                """,
                {**r, "last_seen_at": now_iso},
            )


def all_crowdvolt_sales():
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM crowdvolt_sales "
            "ORDER BY sale_date_iso IS NULL, sale_date_iso DESC, event_name"
        ).fetchall()
    return [dict(r) for r in rows]


def replace_jerujam(tickets, sales, expenses, now_iso):
    with connect() as conn:
        conn.execute("DELETE FROM jerujam_tickets")
        conn.execute("DELETE FROM jerujam_sales")
        conn.execute("DELETE FROM jerujam_expenses")
        for t in tickets:
            conn.execute(
                """
                INSERT INTO jerujam_tickets (id, event_name, event_date, event_date_iso,
                    venue, city, section, row_label, seat_numbers, quantity,
                    cost_per_ticket, total_purchase_cost, purchase_platform,
                    purchase_account, purchase_date, listing_price, listing_platform,
                    sold_price, sold_count, sale_date, seller_fees, status,
                    notes, credit_card, created_at, updated_at, last_seen_at)
                VALUES (:id, :event_name, :event_date, :event_date_iso,
                    :venue, :city, :section, :row_label, :seat_numbers, :quantity,
                    :cost_per_ticket, :total_purchase_cost, :purchase_platform,
                    :purchase_account, :purchase_date, :listing_price, :listing_platform,
                    :sold_price, :sold_count, :sale_date, :seller_fees, :status,
                    :notes, :credit_card, :created_at, :updated_at, :last_seen_at)
                """,
                {**t, "last_seen_at": now_iso},
            )
        for s in sales:
            conn.execute(
                """
                INSERT INTO jerujam_sales (id, ticket_id, quantity, sale_price,
                    sale_date, platform, created_at, last_seen_at)
                VALUES (:id, :ticket_id, :quantity, :sale_price,
                    :sale_date, :platform, :created_at, :last_seen_at)
                """,
                {**s, "last_seen_at": now_iso},
            )
        for e in expenses:
            conn.execute(
                """
                INSERT INTO jerujam_expenses (id, date, vendor, category,
                    description, amount, notes, created_at, last_seen_at)
                VALUES (:id, :date, :vendor, :category,
                    :description, :amount, :notes, :created_at, :last_seen_at)
                """,
                {**e, "last_seen_at": now_iso},
            )


def all_jerujam_tickets():
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM jerujam_tickets "
            "ORDER BY event_date_iso IS NULL, event_date_iso, event_name"
        ).fetchall()
    return [dict(r) for r in rows]


def all_jerujam_sales():
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM jerujam_sales ORDER BY sale_date DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def all_jerujam_expenses():
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM jerujam_expenses ORDER BY date DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def backup(dest_path):
    # SQLite online backup API: safe to run while writes are in flight.
    # To restore: stop Kartis, replace kartis.db with the dated snapshot, restart.
    dest_path = Path(dest_path)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(DB_PATH)
    dst = sqlite3.connect(dest_path)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


def insert_expense_subscription(row, now_iso):
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO expense_subscriptions (id, name, category, amount,
                started_at_iso, ended_at_iso, notes, created_at)
            VALUES (:id, :name, :category, :amount,
                :started_at_iso, :ended_at_iso, :notes, :created_at)
            """,
            {**row, "created_at": now_iso},
        )


def all_expense_subscriptions():
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM expense_subscriptions ORDER BY ended_at_iso IS NOT NULL, started_at_iso, name"
        ).fetchall()
    return [dict(r) for r in rows]


def update_expense_subscription(id_, fields):
    if not fields:
        return
    sets = ", ".join(f"{k}=:{k}" for k in fields)
    with connect() as conn:
        conn.execute(f"UPDATE expense_subscriptions SET {sets} WHERE id=:_id", {**fields, "_id": id_})


def delete_expense_subscription(id_):
    with connect() as conn:
        conn.execute("DELETE FROM expense_subscriptions WHERE id = ?", (id_,))


def insert_expense(row, now_iso):
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO expenses (id, date, date_iso, vendor, category, amount,
                subscription_id, month_key, notes, created_at)
            VALUES (:id, :date, :date_iso, :vendor, :category, :amount,
                :subscription_id, :month_key, :notes, :created_at)
            """,
            {**row, "created_at": now_iso},
        )


def all_expenses():
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM expenses ORDER BY date_iso DESC, created_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def update_expense(id_, fields):
    if not fields:
        return
    sets = ", ".join(f"{k}=:{k}" for k in fields)
    with connect() as conn:
        conn.execute(f"UPDATE expenses SET {sets} WHERE id=:_id", {**fields, "_id": id_})


def delete_expense(id_):
    with connect() as conn:
        conn.execute("DELETE FROM expenses WHERE id = ?", (id_,))


def upsert_subscription_instance(sub_id, month_key, row, now_iso):
    """Idempotent: relies on UNIQUE(subscription_id, month_key) — if a row already
    exists for this sub+month, the INSERT is silently ignored. The user can still
    edit/delete the existing row through normal expense endpoints."""
    with connect() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO expenses (id, date, date_iso, vendor, category, amount,
                subscription_id, month_key, notes, created_at)
            VALUES (:id, :date, :date_iso, :vendor, :category, :amount,
                :subscription_id, :month_key, :notes, :created_at)
            """,
            {**row, "subscription_id": sub_id, "month_key": month_key, "created_at": now_iso},
        )


def insert_owed_item(row, now_iso):
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO owed_items (id, date_iso, amount, description,
                card_account, direction, created_at)
            VALUES (:id, :date_iso, :amount, :description,
                :card_account, :direction, :created_at)
            """,
            {**row, "created_at": now_iso},
        )


def all_owed_items():
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM owed_items ORDER BY date_iso DESC, created_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def update_owed_item(id_, fields):
    if not fields:
        return
    sets = ", ".join(f"{k}=:{k}" for k in fields)
    with connect() as conn:
        conn.execute(f"UPDATE owed_items SET {sets} WHERE id=:_id", {**fields, "_id": id_})


def delete_owed_item(id_):
    with connect() as conn:
        conn.execute("DELETE FROM owed_items WHERE id = ?", (id_,))


def insert_cashback_entry(row, now_iso):
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO cashback_entries (id, date_iso, amount, card_name, source_ref, created_at)
            VALUES (:id, :date_iso, :amount, :card_name, :source_ref, :created_at)
            """,
            {"source_ref": None, **row, "created_at": now_iso},
        )


def has_cashback_source_ref(ref):
    if not ref:
        return False
    with connect() as conn:
        r = conn.execute(
            "SELECT 1 FROM cashback_entries WHERE source_ref = ? LIMIT 1", (ref,)
        ).fetchone()
    return r is not None


def all_cashback_entries():
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM cashback_entries ORDER BY date_iso DESC, created_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def update_cashback_entry(id_, fields):
    if not fields:
        return
    sets = ", ".join(f"{k}=:{k}" for k in fields)
    with connect() as conn:
        conn.execute(f"UPDATE cashback_entries SET {sets} WHERE id=:_id", {**fields, "_id": id_})


def delete_cashback_entry(id_):
    with connect() as conn:
        conn.execute("DELETE FROM cashback_entries WHERE id = ?", (id_,))


def distinct_cashback_cards():
    with connect() as conn:
        rows = conn.execute(
            "SELECT DISTINCT card_name FROM cashback_entries WHERE card_name <> '' ORDER BY card_name"
        ).fetchall()
    return [r["card_name"] for r in rows]


def insert_attachment(row):
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO attachments (id, owner_type, owner_id, filename,
                stored_path, size_bytes, content_type, uploaded_at)
            VALUES (:id, :owner_type, :owner_id, :filename,
                :stored_path, :size_bytes, :content_type, :uploaded_at)
            """,
            row,
        )


def get_attachment(id_):
    with connect() as conn:
        r = conn.execute("SELECT * FROM attachments WHERE id = ?", (id_,)).fetchone()
    return dict(r) if r else None


def list_attachments(owner_type, owner_id):
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM attachments WHERE owner_type = ? AND owner_id = ? "
            "ORDER BY uploaded_at",
            (owner_type, owner_id),
        ).fetchall()
    return [dict(r) for r in rows]


def list_attachments_for_owners(owner_type, owner_ids):
    """Batched version of list_attachments — used by /api/inventory-all to
    avoid an N+1 query when enriching every pending row."""
    ids = list({str(x) for x in owner_ids if x is not None})
    if not ids:
        return {}
    placeholders = ",".join("?" * len(ids))
    with connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM attachments WHERE owner_type = ? "
            f"AND owner_id IN ({placeholders}) ORDER BY uploaded_at",
            (owner_type, *ids),
        ).fetchall()
    out = {}
    for r in rows:
        out.setdefault(r["owner_id"], []).append(dict(r))
    return out


def delete_attachment(id_):
    with connect() as conn:
        conn.execute("DELETE FROM attachments WHERE id = ?", (id_,))


def delete_attachments_for_owner(owner_type, owner_id):
    with connect() as conn:
        conn.execute(
            "DELETE FROM attachments WHERE owner_type = ? AND owner_id = ?",
            (owner_type, owner_id),
        )


def reassign_attachments_owner(owner_type, old_id, new_id):
    """Move attachment DB rows from one owner_id to another. Used when a
    pending_intake row is confirmed and promoted to a manual_inventory row —
    the files on disk stay where they are; we just update the owner pointer
    and (caller is responsible for) optionally moving the disk dir too."""
    with connect() as conn:
        conn.execute(
            "UPDATE attachments SET owner_id = ? WHERE owner_type = ? AND owner_id = ?",
            (new_id, owner_type, old_id),
        )


def insert_pending_intake(row, now_iso):
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO pending_intake (id, message_id, provider, email_from,
                email_subject, email_received_at, event_name, event_date_iso,
                venue, section, row_label, seats, qty, cost, cost_per_unit,
                raw_text, parse_warnings, ticket_url, buyer_email, status, created_at)
            VALUES (:id, :message_id, :provider, :email_from,
                :email_subject, :email_received_at, :event_name, :event_date_iso,
                :venue, :section, :row_label, :seats, :qty, :cost, :cost_per_unit,
                :raw_text, :parse_warnings, :ticket_url, :buyer_email, :status, :created_at)
            ON CONFLICT(message_id) DO NOTHING
            """,
            {**row, "created_at": now_iso},
        )


def get_pending_intake(id_):
    with connect() as conn:
        r = conn.execute("SELECT * FROM pending_intake WHERE id = ?", (id_,)).fetchone()
    return dict(r) if r else None


def all_pending_intake(status="new"):
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM pending_intake WHERE status = ? "
            "ORDER BY email_received_at DESC, created_at DESC",
            (status,),
        ).fetchall()
    return [dict(r) for r in rows]


def has_intake_message(message_id):
    if not message_id:
        return False
    with connect() as conn:
        r = conn.execute(
            "SELECT 1 FROM pending_intake WHERE message_id = ? LIMIT 1",
            (message_id,),
        ).fetchone()
    return r is not None


def update_pending_intake(id_, fields):
    if not fields:
        return
    sets = ", ".join(f"{k}=:{k}" for k in fields)
    with connect() as conn:
        conn.execute(
            f"UPDATE pending_intake SET {sets} WHERE id=:_id",
            {**fields, "_id": id_},
        )


def delete_pending_intake(id_):
    with connect() as conn:
        conn.execute("DELETE FROM pending_intake WHERE id = ?", (id_,))


def insert_maaser(row, now_iso):
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO maaser_payments (id, date, date_iso, recipient, amount, notes, tax_deductible, created_at)
            VALUES (:id, :date, :date_iso, :recipient, :amount, :notes, :tax_deductible, :created_at)
            """,
            {**{"tax_deductible": 0}, **row, "created_at": now_iso},
        )


def all_maaser():
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM maaser_payments ORDER BY date_iso DESC, created_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def update_maaser(id_, fields):
    if not fields:
        return
    sets = ", ".join(f"{k}=:{k}" for k in fields)
    with connect() as conn:
        conn.execute(f"UPDATE maaser_payments SET {sets} WHERE id=:_id", {**fields, "_id": id_})


def delete_maaser(id_):
    with connect() as conn:
        conn.execute("DELETE FROM maaser_payments WHERE id = ?", (id_,))


# --- Ticketmaster drop watchers ----------------------------------------

def tm_insert_watcher(row, now_iso):
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO tm_watchers (id, label, source, event_code, perf_code,
                paused, muted, notify_channels, filters, discord_channel, created_at)
            VALUES (:id, :label, :source, :event_code, :perf_code,
                :paused, :muted, :notify_channels, :filters, :discord_channel, :created_at)
            """,
            {
                "muted": 0,
                "notify_channels": "discord,email",
                "source": "ticketmaster",
                "filters": None,
                "discord_channel": None,
                **row,
                "created_at": now_iso,
            },
        )


def tm_all_watchers():
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM tm_watchers ORDER BY paused, created_at"
        ).fetchall()
    return [dict(r) for r in rows]


def tm_get_watcher(id_):
    with connect() as conn:
        row = conn.execute("SELECT * FROM tm_watchers WHERE id = ?", (id_,)).fetchone()
    return dict(row) if row else None


def tm_active_watchers():
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM tm_watchers WHERE paused = 0 ORDER BY created_at"
        ).fetchall()
    return [dict(r) for r in rows]


def discord_channel_get(name):
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM discord_event_channels WHERE name = ?", (name,)
        ).fetchone()
    return dict(row) if row else None


def discord_channel_put(name, channel_id, webhook_url, now_iso):
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO discord_event_channels (name, channel_id, webhook_url, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET channel_id=excluded.channel_id,
                webhook_url=excluded.webhook_url
            """,
            (name, channel_id, webhook_url, now_iso),
        )


def tm_update_watcher(id_, fields):
    if not fields:
        return
    sets = ", ".join(f"{k}=:{k}" for k in fields)
    with connect() as conn:
        conn.execute(f"UPDATE tm_watchers SET {sets} WHERE id=:_id", {**fields, "_id": id_})


def tm_delete_watcher(id_):
    with connect() as conn:
        conn.execute("DELETE FROM tm_watchers WHERE id = ?", (id_,))
        conn.execute("DELETE FROM tm_seat_state WHERE watcher_id = ?", (id_,))
        conn.execute("DELETE FROM tm_drops WHERE watcher_id = ?", (id_,))
        conn.execute("DELETE FROM tm_seat_cooldown WHERE watcher_id = ?", (id_,))


def tm_seat_cooldown_active(watcher_id, seat_keys, window_seconds, now_iso):
    """Of the given seat_keys, return the set still inside their per-seat
    cool-down window — i.e. notified within the last `window_seconds`.

    A non-positive window disables cool-down (returns empty set). Rows whose
    timestamp is older than the window are pruned opportunistically so the
    table stays small.
    """
    if not seat_keys or not window_seconds or window_seconds <= 0:
        return set()
    try:
        now = _dt.datetime.fromisoformat(now_iso)
    except (TypeError, ValueError):
        now = _dt.datetime.now(_dt.timezone.utc)
    active = set()
    with connect() as conn:
        rows = conn.execute(
            "SELECT seat_key, last_notified_at FROM tm_seat_cooldown WHERE watcher_id = ?",
            (watcher_id,),
        ).fetchall()
        stale = []
        wanted = set(seat_keys)
        for r in rows:
            try:
                age = (now - _dt.datetime.fromisoformat(r["last_notified_at"])).total_seconds()
            except (TypeError, ValueError):
                continue
            if age < 0 or age <= window_seconds:
                if r["seat_key"] in wanted:
                    active.add(r["seat_key"])
            elif age > window_seconds * 4:
                # Long past the window and not refreshed — safe to prune.
                stale.append(r["seat_key"])
        for k in stale:
            conn.execute(
                "DELETE FROM tm_seat_cooldown WHERE watcher_id = ? AND seat_key = ?",
                (watcher_id, k),
            )
    return active


def tm_seat_cooldown_mark(watcher_id, seat_keys, now_iso):
    """Stamp `now_iso` as the last-notified time for each seat_key (upsert)."""
    if not seat_keys:
        return
    with connect() as conn:
        conn.executemany(
            "INSERT INTO tm_seat_cooldown (watcher_id, seat_key, last_notified_at) "
            "VALUES (?, ?, ?) ON CONFLICT(watcher_id, seat_key) "
            "DO UPDATE SET last_notified_at = excluded.last_notified_at",
            [(watcher_id, k, now_iso) for k in seat_keys],
        )


def tm_get_seat_keys(watcher_id):
    with connect() as conn:
        rows = conn.execute(
            "SELECT seat_key FROM tm_seat_state WHERE watcher_id = ?", (watcher_id,)
        ).fetchall()
    return {r["seat_key"] for r in rows}


def tm_replace_seat_state(watcher_id, seats):
    """Atomically replace the watcher's known seat set. Seats must be in the
    normalized shape with `block`, `row`, `seat` keys (each source module
    produces this shape from its raw API response).

    Event-level ticketmaster seats carry `_perf`; it must be part of the
    stored key to match `ticketmaster.event_seat_key`, which the tick loops
    diff against — without it no stored key ever matches and every seat
    re-reports as "added" on every tick.
    """
    with connect() as conn:
        conn.execute("DELETE FROM tm_seat_state WHERE watcher_id = ?", (watcher_id,))
        seen = set()
        for s in seats:
            block = str(s.get("block") or s.get("b") or "")
            row = str(s.get("row") or s.get("r") or "")
            num = str(s.get("seat") if s.get("seat") is not None else (s.get("l") if s.get("l") is not None else ""))
            key = f"{block}|{row}|{num}"
            if s.get("_perf"):
                key = f"{s['_perf']}|{key}"
            # Unconfirmed sold-out-perf seats: mirror ticketmaster.event_seat_key's
            # "U|" prefix or every such seat re-reports as added on every tick.
            if s.get("unconfirmed"):
                key = "U|" + key
            if key in seen:
                continue
            seen.add(key)
            conn.execute(
                "INSERT INTO tm_seat_state (watcher_id, seat_key, block, row_label, seat_num) "
                "VALUES (?, ?, ?, ?, ?)",
                (watcher_id, key, block, row, num),
            )


# --- App-level settings (master pause / mute / future flags) -------------

def setting_get(key, default=None):
    with connect() as conn:
        row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def setting_set(key, value, now_iso):
    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO app_settings (key, value, updated_at) VALUES (?, ?, ?)",
            (key, str(value) if value is not None else None, now_iso),
        )


def setting_get_bool(key, default=False):
    v = setting_get(key)
    if v is None:
        return default
    return v.lower() in ("1", "true", "yes", "on")


def all_settings():
    with connect() as conn:
        rows = conn.execute("SELECT key, value, updated_at FROM app_settings").fetchall()
    return {r["key"]: r["value"] for r in rows}


def pacha_all_seen():
    """Every pacha event ever seen, keyed by event_id."""
    with connect() as conn:
        rows = conn.execute("SELECT * FROM pacha_seen_events").fetchall()
    return {r["event_id"]: dict(r) for r in rows}


def pacha_upsert_seen(ev, now_iso):
    """Insert or refresh one normalized event dict from pacha_events.fetch_events().
    first_seen_at is preserved on update."""
    with connect() as conn:
        conn.execute(
            """INSERT INTO pacha_seen_events (event_id, name, slug, date_text,
                   start_date, on_sale, ga_price, ga_sold_out, buy_url,
                   first_seen_at, last_seen_at, ga_release, ga_available,
                   ga_quantity, total_available, sold_cum, sold_cum_since,
                   tiers_json, vip_price)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(event_id) DO UPDATE SET
                   name = excluded.name, slug = excluded.slug,
                   date_text = excluded.date_text, start_date = excluded.start_date,
                   on_sale = excluded.on_sale, ga_price = excluded.ga_price,
                   ga_sold_out = excluded.ga_sold_out, buy_url = excluded.buy_url,
                   last_seen_at = excluded.last_seen_at,
                   ga_release = excluded.ga_release,
                   ga_available = excluded.ga_available,
                   ga_quantity = excluded.ga_quantity,
                   total_available = excluded.total_available,
                   sold_cum = excluded.sold_cum,
                   sold_cum_since = excluded.sold_cum_since,
                   tiers_json = excluded.tiers_json,
                   vip_price = excluded.vip_price""",
            (ev["event_id"], ev.get("name"), ev.get("slug"), ev.get("date_text"),
             ev.get("start_date"), 1 if ev.get("on_sale") else 0, ev.get("ga_price"),
             1 if ev.get("ga_sold_out") else 0, ev.get("buy_url"), now_iso, now_iso,
             ev.get("ga_release"), ev.get("ga_available"), ev.get("ga_quantity"),
             ev.get("total_available"), ev.get("sold_cum"), ev.get("sold_cum_since"),
             json.dumps(ev["tiers"], ensure_ascii=False) if ev.get("tiers") else None,
             ev.get("vip_price")),
        )


def pacha_seen_count():
    with connect() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM pacha_seen_events").fetchone()
    return row["n"]


def pacha_release_sync(event_id, tiers, now_iso):
    """Reconcile one event's currently-listed tiers against its release log.
    New tier name → new log row; known tier → refresh counts (+stamp
    sold_out_at the first tick it reads 0); open log rows whose tier is no
    longer listed → close with sold_out_at=now (available_last tells whether
    it actually sold out or was just removed)."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM pacha_release_log WHERE event_id = ?", (event_id,)
        ).fetchall()
        logged = {r["tier_name"]: dict(r) for r in rows}
        current_names = set()
        for t in tiers or []:
            name = (t.get("name") or "").strip()
            if not name:
                continue
            current_names.add(name)
            old = logged.get(name)
            sold_out_now = t.get("available") == 0
            if old is None:
                conn.execute(
                    """INSERT INTO pacha_release_log (event_id, tier_name,
                           price, quantity, available_last, used_last,
                           first_seen_at, last_seen_at, sold_out_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (event_id, name, t.get("price"), t.get("quantity"),
                     t.get("available"), t.get("used"), now_iso, now_iso,
                     now_iso if sold_out_now else None),
                )
            else:
                conn.execute(
                    """UPDATE pacha_release_log SET price = ?, quantity = ?,
                           available_last = ?, used_last = ?, last_seen_at = ?,
                           sold_out_at = COALESCE(sold_out_at, ?)
                       WHERE event_id = ? AND tier_name = ?""",
                    (t.get("price"), t.get("quantity"), t.get("available"),
                     t.get("used"), now_iso,
                     now_iso if sold_out_now else None,
                     event_id, name),
                )
        for name, old in logged.items():
            if name not in current_names and not old.get("sold_out_at"):
                conn.execute(
                    "UPDATE pacha_release_log SET sold_out_at = ? "
                    "WHERE event_id = ? AND tier_name = ?",
                    (now_iso, event_id, name),
                )


def pacha_release_log_all():
    """All release-log rows grouped by event_id, oldest release first."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM pacha_release_log ORDER BY first_seen_at, id"
        ).fetchall()
    out = {}
    for r in rows:
        out.setdefault(r["event_id"], []).append(dict(r))
    return out


def dice_tier_log_update(event_code, blocks, now_iso):
    """Diff one event's current ticket-type states (dice labels ``blocks``
    map) against the latest open log row per type; insert a row on change,
    stamp ended_at on the superseded/vanished state. Idempotent within a
    state — repeated calls with the same data only bump last_seen_at."""
    ev = str(event_code)
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM dice_tier_log WHERE event_code = ? AND ended_at IS NULL",
            (ev,),
        ).fetchall()
        open_by_type = {r["type_name"]: dict(r) for r in rows}
        current_types = set()
        for info in (blocks or {}).values():
            name = (info.get("name") or "").strip()
            if not name:
                continue
            current_types.add(name)
            state = (info.get("tier_index"), info.get("price"), info.get("status"))
            old = open_by_type.get(name)
            if old and (old.get("tier_index"), old.get("price"), old.get("status")) == state:
                conn.execute(
                    "UPDATE dice_tier_log SET last_seen_at = ? WHERE id = ?",
                    (now_iso, old["id"]),
                )
                continue
            if old:
                conn.execute(
                    "UPDATE dice_tier_log SET ended_at = ? WHERE id = ?",
                    (now_iso, old["id"]),
                )
            conn.execute(
                """INSERT INTO dice_tier_log (event_code, type_name, tier_index,
                       tier_name, price, currency, status, first_seen_at,
                       last_seen_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (ev, name, info.get("tier_index"), info.get("tier_name"),
                 info.get("price"), info.get("currency"), info.get("status"),
                 now_iso, now_iso),
            )
        for name, old in open_by_type.items():
            if name not in current_types:
                conn.execute(
                    "UPDATE dice_tier_log SET ended_at = ? WHERE id = ?",
                    (now_iso, old["id"]),
                )


def dice_tier_log_all():
    """All log rows grouped by event_code, oldest state first."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM dice_tier_log ORDER BY first_seen_at, id"
        ).fetchall()
    out = {}
    for r in rows:
        out.setdefault(r["event_code"], []).append(dict(r))
    return out


def site_events_all_seen(source):
    """Every event ever seen on one site's listing feed, keyed by event_key."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM site_seen_events WHERE source = ?", (source,)
        ).fetchall()
    return {r["event_key"]: dict(r) for r in rows}


def site_events_upsert_seen(source, ev, now_iso):
    """Insert or refresh one normalized event dict from kupat_events /
    tm_events fetch_events(). first_seen_at is preserved on update."""
    with connect() as conn:
        conn.execute(
            """INSERT INTO site_seen_events (source, event_key, name, venue,
                   date_text, first_date_ms, on_sale, url,
                   first_seen_at, last_seen_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(source, event_key) DO UPDATE SET
                   name = excluded.name, venue = excluded.venue,
                   date_text = excluded.date_text,
                   first_date_ms = excluded.first_date_ms,
                   on_sale = excluded.on_sale, url = excluded.url,
                   last_seen_at = excluded.last_seen_at""",
            (source, ev["event_key"], ev.get("name"), ev.get("venue"),
             ev.get("date_text"), ev.get("first_date_ms"),
             1 if ev.get("on_sale") else 0, ev.get("url"), now_iso, now_iso),
        )


def site_events_set_perfs(source, event_key, perf_keys):
    """Persist the known-performance set for one event (kupat 'new date'
    diff). Callers pass the UNION of stored + current keys — the set only
    ever grows, so a perf dropping off the feed can't re-ping on return.
    The upsert path never touches perfs_json, so tm/barby rows stay NULL."""
    with connect() as conn:
        conn.execute(
            "UPDATE site_seen_events SET perfs_json = ? WHERE source = ? AND event_key = ?",
            (json.dumps(sorted(perf_keys)), source, event_key),
        )


def site_events_seen_counts():
    """{source: rows} for every source that ever stored a row."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT source, COUNT(*) AS n FROM site_seen_events GROUP BY source"
        ).fetchall()
    return {r["source"]: r["n"] for r in rows}


# ---------------- market tracker (market.py / /market page) ----------------

def market_upsert(ev, now_iso):
    """Insert or refresh one market entity dict from a market.py sweep
    adapter. first_seen_at and manual are preserved on update."""
    with connect() as conn:
        conn.execute(
            """INSERT INTO market_events (source, event_code, perf_code, name,
                   venue, date_text, first_date_ms, url, status, capacity,
                   available, min_price, manual, first_seen_at, last_seen_at,
                   last_error, currency)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(source, event_code, perf_code) DO UPDATE SET
                   name = excluded.name, venue = excluded.venue,
                   date_text = excluded.date_text,
                   first_date_ms = excluded.first_date_ms,
                   url = excluded.url, status = excluded.status,
                   capacity = excluded.capacity,
                   available = excluded.available,
                   min_price = excluded.min_price,
                   last_seen_at = excluded.last_seen_at,
                   last_error = excluded.last_error,
                   currency = excluded.currency""",
            (ev["source"], str(ev["event_code"]), str(ev.get("perf_code") or "0"),
             ev.get("name"), ev.get("venue"), ev.get("date_text"),
             ev.get("first_date_ms"), ev.get("url"), ev.get("status"),
             ev.get("capacity"), ev.get("available"), ev.get("min_price"),
             1 if ev.get("manual") else 0, now_iso, now_iso,
             ev.get("last_error"), ev.get("currency") or "ILS"),
        )


def market_set_status(source, event_code, perf_code, status):
    with connect() as conn:
        conn.execute(
            "UPDATE market_events SET status = ? WHERE source = ? AND event_code = ? AND perf_code = ?",
            (status, source, str(event_code), str(perf_code)),
        )


def market_all(include_past=False):
    with connect() as conn:
        q = "SELECT * FROM market_events"
        if not include_past:
            q += " WHERE status IS NULL OR status != 'past'"
        rows = conn.execute(q).fetchall()
    return [dict(r) for r in rows]


def market_manual_all():
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM market_manual ORDER BY added_at"
        ).fetchall()
    return [dict(r) for r in rows]


def market_manual_add(source, kind, code, label, now_iso):
    with connect() as conn:
        conn.execute(
            """INSERT INTO market_manual (source, kind, code, label, added_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(source, kind, code) DO UPDATE SET label = excluded.label""",
            (source, kind, str(code), label, now_iso),
        )


def market_manual_remove(id_):
    with connect() as conn:
        conn.execute("DELETE FROM market_manual WHERE id = ?", (id_,))


def market_hide(source, event_code, name, until_iso, now_iso):
    """Snooze one event off the /market page until until_iso (None = forever).
    Re-hiding an already-hidden event just updates the window."""
    with connect() as conn:
        conn.execute(
            """INSERT INTO market_hidden (source, event_code, name,
                   hidden_until, created_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(source, event_code) DO UPDATE SET
                   name = excluded.name,
                   hidden_until = excluded.hidden_until,
                   created_at = excluded.created_at""",
            (source, str(event_code), name, until_iso, now_iso),
        )


def market_unhide(source, event_code):
    with connect() as conn:
        conn.execute(
            "DELETE FROM market_hidden WHERE source = ? AND event_code = ?",
            (source, str(event_code)),
        )


def market_hidden_active(now_iso):
    """Currently-active hide rows; expired ones are pruned on the way."""
    with connect() as conn:
        conn.execute(
            "DELETE FROM market_hidden WHERE hidden_until IS NOT NULL AND hidden_until <= ?",
            (now_iso,),
        )
        rows = conn.execute(
            "SELECT * FROM market_hidden ORDER BY created_at"
        ).fetchall()
    return [dict(r) for r in rows]


def tm_record_drop(watcher_id, added_count, removed_count, seats_json, notify_result, now_iso, notify_count=None):
    with connect() as conn:
        conn.execute(
            """INSERT INTO tm_drops (watcher_id, detected_at, added_count,
               removed_count, seats_json, notify_result, notify_count)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (watcher_id, now_iso, added_count, removed_count, seats_json, notify_result,
             notify_count if notify_count is not None else added_count),
        )


def tm_recent_drops(watcher_id=None, limit=200):
    with connect() as conn:
        if watcher_id:
            rows = conn.execute(
                "SELECT * FROM tm_drops WHERE watcher_id = ? ORDER BY detected_at DESC LIMIT ?",
                (watcher_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM tm_drops ORDER BY detected_at DESC LIMIT ?", (limit,),
            ).fetchall()
    return [dict(r) for r in rows]


# ---------------- viagogo section name mapping (taught, not literal) -------

def viagogo_section_map_get(venue, kupat_section):
    with connect() as conn:
        row = conn.execute(
            "SELECT viagogo_section FROM viagogo_section_map WHERE venue = ? AND kupat_section = ?",
            (venue, kupat_section),
        ).fetchone()
    return row["viagogo_section"] if row else None


def viagogo_section_map_set(venue, kupat_section, viagogo_section, now_iso):
    with connect() as conn:
        conn.execute(
            """INSERT INTO viagogo_section_map (venue, kupat_section, viagogo_section, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(venue, kupat_section) DO UPDATE SET
                   viagogo_section = excluded.viagogo_section,
                   updated_at = excluded.updated_at""",
            (venue, kupat_section, viagogo_section, now_iso),
        )


def kupat_name_map_get(hebrew_name):
    """Return the English search term for a Hebrew Kupat artist/venue name, or None."""
    with connect() as conn:
        row = conn.execute(
            "SELECT english_name FROM kupat_name_map WHERE hebrew_name = ?",
            (hebrew_name,),
        ).fetchone()
    return row["english_name"] if row else None


def kupat_name_map_set(hebrew_name, english_name, now_iso):
    with connect() as conn:
        conn.execute(
            """INSERT INTO kupat_name_map (hebrew_name, english_name, updated_at)
               VALUES (?, ?, ?)
               ON CONFLICT(hebrew_name) DO UPDATE SET
                   english_name = excluded.english_name,
                   updated_at = excluded.updated_at""",
            (hebrew_name, english_name, now_iso),
        )


def kupat_name_map_all():
    with connect() as conn:
        rows = conn.execute(
            "SELECT hebrew_name, english_name, updated_at FROM kupat_name_map ORDER BY hebrew_name"
        ).fetchall()
    return [dict(r) for r in rows]


def viagogo_section_map_all():
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM viagogo_section_map ORDER BY venue, kupat_section"
        ).fetchall()
    return [dict(r) for r in rows]


def viagogo_section_map_delete(venue, kupat_section):
    with connect() as conn:
        conn.execute(
            "DELETE FROM viagogo_section_map WHERE venue = ? AND kupat_section = ?",
            (venue, kupat_section),
        )


# ---------------- viagogo draft-listing push pipeline ----------------------
# Kupat purchase-confirmation email -> matched viagogo event -> (user
# approval) -> draft (unpublished) viagogo listing. See viagogo_listing.py.

def viagogo_push_insert(row, now_iso):
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO viagogo_push (id, intake_id, status, event_name, venue,
                event_date_iso, section, row_label, seats, qty, cost, cost_per_unit,
                candidates_json, chosen_event_id, chosen_event_name, chosen_venue,
                chosen_event_date, viagogo_section, fx_rate, cost_usd_per_ticket,
                website_price_usd, ticket_url, buyer_email, listing_id, error, created_at, updated_at)
            VALUES (:id, :intake_id, :status, :event_name, :venue,
                :event_date_iso, :section, :row_label, :seats, :qty, :cost, :cost_per_unit,
                :candidates_json, :chosen_event_id, :chosen_event_name, :chosen_venue,
                :chosen_event_date, :viagogo_section, :fx_rate, :cost_usd_per_ticket,
                :website_price_usd, :ticket_url, :buyer_email, :listing_id, :error, :created_at, :updated_at)
            """,
            {
                "status": "searching",
                "candidates_json": None, "chosen_event_id": None, "chosen_event_name": None,
                "chosen_venue": None, "chosen_event_date": None, "viagogo_section": None,
                "fx_rate": None, "cost_usd_per_ticket": None, "website_price_usd": None,
                "ticket_url": None, "buyer_email": None, "listing_id": None, "error": None,
                **row,
                "created_at": now_iso, "updated_at": now_iso,
            },
        )


def viagogo_push_get(id_):
    with connect() as conn:
        row = conn.execute("SELECT * FROM viagogo_push WHERE id = ?", (id_,)).fetchone()
    return dict(row) if row else None


def viagogo_push_all(status=None):
    with connect() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM viagogo_push WHERE status = ? ORDER BY created_at DESC",
                (status,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM viagogo_push ORDER BY created_at DESC"
            ).fetchall()
    return [dict(r) for r in rows]


def viagogo_push_get_by_intake(intake_id):
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM viagogo_push WHERE intake_id = ? ORDER BY created_at DESC LIMIT 1",
            (intake_id,),
        ).fetchone()
    return dict(row) if row else None


def viagogo_push_update(id_, fields, now_iso):
    if not fields:
        return
    fields = {**fields, "updated_at": now_iso}
    sets = ", ".join(f"{k}=:{k}" for k in fields)
    with connect() as conn:
        conn.execute(f"UPDATE viagogo_push SET {sets} WHERE id=:_id", {**fields, "_id": id_})


def viagogo_push_delete(id_):
    with connect() as conn:
        conn.execute("DELETE FROM viagogo_push WHERE id = ?", (id_,))


# ---------------- GA / festival sales snapshots ----------------------------
# Keyed by (source, event_code, perf_code) so tickchak festival (perf '0')
# and kupat GA (real presentation ids) share one table. `capacity`/`sold`
# may be NULL (kupat GA only exposes tickets-left); velocity is computed
# from `available` deltas, which both sources have.

def sales_snapshot_insert(source, event_code, perf_code, capacity, available, sold, now_iso):
    with connect() as conn:
        conn.execute(
            """INSERT INTO tickchak_sales_snapshots
               (source, event_code, perf_code, captured_at, capacity, available, sold)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (source, str(event_code), str(perf_code), now_iso, capacity, available, sold),
        )


def sales_snapshot_latest(source, event_code, perf_code):
    with connect() as conn:
        r = conn.execute(
            "SELECT * FROM tickchak_sales_snapshots WHERE source = ? AND event_code = ? "
            "AND perf_code = ? ORDER BY captured_at DESC LIMIT 1",
            (source, str(event_code), str(perf_code)),
        ).fetchone()
    return dict(r) if r else None


def sales_snapshot_earliest(source, event_code, perf_code):
    with connect() as conn:
        r = conn.execute(
            "SELECT * FROM tickchak_sales_snapshots WHERE source = ? AND event_code = ? "
            "AND perf_code = ? ORDER BY captured_at ASC LIMIT 1",
            (source, str(event_code), str(perf_code)),
        ).fetchone()
    return dict(r) if r else None


def sales_snapshot_asof(source, event_code, perf_code, ts_iso):
    """Latest snapshot at or before ts_iso — the baseline for a rolling window."""
    with connect() as conn:
        r = conn.execute(
            "SELECT * FROM tickchak_sales_snapshots WHERE source = ? AND event_code = ? "
            "AND perf_code = ? AND captured_at <= ? ORDER BY captured_at DESC LIMIT 1",
            (source, str(event_code), str(perf_code), ts_iso),
        ).fetchone()
    return dict(r) if r else None


def sales_snapshot_series(source, event_code, perf_code, since_iso):
    """Ascending [(captured_at_iso, available), …] since since_iso — used to
    sum availability decreases (real sales) over a window while ignoring
    increases (ticket releases/refunds)."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT captured_at, available FROM tickchak_sales_snapshots "
            "WHERE source=? AND event_code=? AND perf_code=? AND captured_at>=? "
            "AND available IS NOT NULL ORDER BY captured_at ASC",
            (source, str(event_code), str(perf_code), since_iso),
        ).fetchall()
    return [(r["captured_at"], r["available"]) for r in rows]


def sales_snapshot_series_bulk(since_iso):
    """{(source, event_code, perf_code): ascending [(captured_at, available), …]}
    for every key with snapshots since since_iso. One scan instead of one
    point query per market row — the /api/market builder walks hundreds of
    keys per page load."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT source, event_code, perf_code, captured_at, available "
            "FROM tickchak_sales_snapshots "
            "WHERE captured_at >= ? AND available IS NOT NULL "
            "ORDER BY captured_at ASC",
            (since_iso,),
        ).fetchall()
    out = {}
    for r in rows:
        key = (r["source"], r["event_code"], r["perf_code"])
        out.setdefault(key, []).append((r["captured_at"], r["available"]))
    return out


def sales_snapshot_prune(cutoff_iso):
    """Drop snapshots older than cutoff_iso (we only need ~7 days of history)."""
    with connect() as conn:
        conn.execute(
            "DELETE FROM tickchak_sales_snapshots WHERE captured_at < ?", (cutoff_iso,),
        )


# ---------------- viagogo market sales (/vgsales) ---------------------------

def vg_sales_event_upsert(event_id, name, venue, event_date, window_json,
                          last_error, now_iso):
    """Bookkeeping row for one tracked event. Metadata only overwrites with
    non-NULL values so a failed fetch doesn't blank out the header fields."""
    with connect() as conn:
        conn.execute(
            """INSERT INTO viagogo_sales_events (event_id, name, venue,
                   event_date, window_json, last_fetch_at, last_error,
                   first_seen_at, last_seen_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(event_id) DO UPDATE SET
                   name = COALESCE(excluded.name, name),
                   venue = COALESCE(excluded.venue, venue),
                   event_date = COALESCE(excluded.event_date, event_date),
                   window_json = COALESCE(excluded.window_json, window_json),
                   last_fetch_at = excluded.last_fetch_at,
                   last_error = excluded.last_error,
                   last_seen_at = excluded.last_seen_at""",
            (str(event_id), name, venue, event_date, window_json, now_iso,
             last_error, now_iso, now_iso),
        )


def vg_sales_event_get(event_id):
    with connect() as conn:
        r = conn.execute(
            "SELECT * FROM viagogo_sales_events WHERE event_id = ?",
            (str(event_id),),
        ).fetchone()
    return dict(r) if r else None


def vg_sales_events_all():
    with connect() as conn:
        rows = conn.execute("SELECT * FROM viagogo_sales_events").fetchall()
    return [dict(r) for r in rows]


def vg_market_sales_insert_many(event_id, rows, baseline, now_iso):
    """Append observed sale rows. rows: [{section, row, seats, qty, price,
    is_ours}, …] in feed order. Returns the number inserted."""
    with connect() as conn:
        conn.executemany(
            """INSERT INTO viagogo_market_sales
               (event_id, section, row_label, seats, qty, price, is_ours,
                baseline, observed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [(str(event_id), r.get("section"), r.get("row"), r.get("seats"),
              r.get("qty"), r.get("price"), 1 if r.get("is_ours") else 0,
              1 if baseline else 0, now_iso) for r in rows],
        )
    return len(rows)


def vg_market_sales_since(since_iso):
    """{event_id: ascending [row dicts]} for every sale observed since
    since_iso (baseline rows included — callers filter). One scan, same
    rationale as sales_snapshot_series_bulk."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM viagogo_market_sales WHERE observed_at >= ? "
            "ORDER BY observed_at ASC, id ASC",
            (since_iso,),
        ).fetchall()
    out = {}
    for r in rows:
        out.setdefault(r["event_id"], []).append(dict(r))
    return out


def vg_market_sales_recent(event_id, limit=30):
    """Newest-first recent rows for one event's expanded view."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM viagogo_market_sales WHERE event_id = ? "
            "ORDER BY id DESC LIMIT ?",
            (str(event_id), int(limit)),
        ).fetchall()
    return [dict(r) for r in rows]


def vg_market_sales_totals():
    """{event_id: {tickets, sales}} counted over every non-baseline row —
    'Σ sold since tracking began' for the /vgsales table."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT event_id, SUM(qty) AS tickets, COUNT(*) AS sales "
            "FROM viagogo_market_sales WHERE baseline = 0 GROUP BY event_id"
        ).fetchall()
    return {r["event_id"]: {"tickets": r["tickets"] or 0,
                            "sales": r["sales"]} for r in rows}


def vg_market_sales_prune(cutoff_iso):
    with connect() as conn:
        conn.execute(
            "DELETE FROM viagogo_market_sales WHERE observed_at < ?",
            (cutoff_iso,),
        )


def vg_watchlist_all():
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM viagogo_sales_watchlist ORDER BY added_at"
        ).fetchall()
    return [dict(r) for r in rows]


def vg_watchlist_add(event_id, label, url, now_iso):
    with connect() as conn:
        conn.execute(
            """INSERT INTO viagogo_sales_watchlist (event_id, label, url, added_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(event_id) DO UPDATE SET
                   label = COALESCE(excluded.label, label),
                   url = COALESCE(excluded.url, url)""",
            (str(event_id), label, url, now_iso),
        )


def vg_watchlist_remove(event_id):
    with connect() as conn:
        conn.execute(
            "DELETE FROM viagogo_sales_watchlist WHERE event_id = ?",
            (str(event_id),),
        )


def viagogo_listing_event_ids(fresh_since_iso):
    """Distinct viagogo event ids we currently hold listings on, with display
    metadata. Freshness-gated on the scrape mirror's last_seen_at so deleted
    listings age out; past-dated events are skipped by the caller (it has a
    'now' to compare event_date_iso against)."""
    with connect() as conn:
        rows = conn.execute(
            """SELECT event_id, MIN(event_name) AS name, MIN(venue) AS venue,
                      MIN(event_date_iso) AS event_date_iso, COUNT(*) AS listings
               FROM viagogo_listings
               WHERE event_id IS NOT NULL AND event_id != ''
                 AND last_seen_at >= ?
               GROUP BY event_id""",
            (fresh_since_iso,),
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------- Kupat credits ---------------------------------------------

def insert_kupat_credit(row, now_iso):
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO kupat_credits (id, issued_date, ils_amount, original_usd_cost, note, created_at)
            VALUES (:id, :issued_date, :ils_amount, :original_usd_cost, :note, :created_at)
            """,
            {**row, "created_at": now_iso},
        )


def all_kupat_credits():
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM kupat_credits ORDER BY issued_date DESC, created_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def update_kupat_credit(id_, fields):
    if not fields:
        return
    sets = ", ".join(f"{k}=:{k}" for k in fields)
    with connect() as conn:
        conn.execute(f"UPDATE kupat_credits SET {sets} WHERE id=:_id", {**fields, "_id": id_})


def delete_kupat_credit(id_):
    with connect() as conn:
        conn.execute("DELETE FROM kupat_credit_spends WHERE credit_id = ?", (id_,))
        conn.execute("DELETE FROM kupat_credits WHERE id = ?", (id_,))


def insert_kupat_spend(row, now_iso):
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO kupat_credit_spends (id, credit_id, spend_date, ils_amount, fx_rate, note, created_at)
            VALUES (:id, :credit_id, :spend_date, :ils_amount, :fx_rate, :note, :created_at)
            """,
            {**row, "created_at": now_iso},
        )


def all_kupat_spends():
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM kupat_credit_spends ORDER BY spend_date DESC, created_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def delete_kupat_spend(id_):
    with connect() as conn:
        conn.execute("DELETE FROM kupat_credit_spends WHERE id = ?", (id_,))


# ---------------- To-Do list -----------------------------------------------

def todo_insert(row, now_iso):
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO todos (id, title, notes, due_date_iso, urgency, status,
                linked_source, linked_source_id, tags, amount, amount_currency,
                recurrence, auto_source, created_at, updated_at, completed_at,
                notified_at)
            VALUES (:id, :title, :notes, :due_date_iso, :urgency, :status,
                :linked_source, :linked_source_id, :tags, :amount, :amount_currency,
                :recurrence, :auto_source, :created_at, :updated_at, :completed_at,
                :notified_at)
            """,
            {
                "notes": None, "due_date_iso": None, "urgency": "normal",
                "status": "open", "linked_source": None, "linked_source_id": None,
                "tags": None, "amount": None, "amount_currency": None,
                "recurrence": None, "auto_source": None,
                "completed_at": None, "notified_at": None,
                **row,
                "created_at": now_iso, "updated_at": now_iso,
            },
        )


def todo_get(id_):
    with connect() as conn:
        r = conn.execute("SELECT * FROM todos WHERE id = ?", (id_,)).fetchone()
    return dict(r) if r else None


def todo_all(status=None):
    sql = "SELECT * FROM todos"
    args = ()
    if status and status != "all":
        sql += " WHERE status = ?"
        args = (status,)
    sql += (
        " ORDER BY CASE status WHEN 'open' THEN 0 ELSE 1 END,"
        " due_date_iso IS NULL, due_date_iso,"
        " CASE urgency WHEN 'critical' THEN 0 WHEN 'high' THEN 1 "
        " WHEN 'normal' THEN 2 WHEN 'low' THEN 3 ELSE 4 END,"
        " created_at"
    )
    with connect() as conn:
        rows = conn.execute(sql, args).fetchall()
    return [dict(r) for r in rows]


def todo_update(id_, fields, now_iso):
    if not fields:
        return
    fields = {**fields, "updated_at": now_iso}
    sets = ", ".join(f"{k}=:{k}" for k in fields)
    with connect() as conn:
        conn.execute(f"UPDATE todos SET {sets} WHERE id=:_id", {**fields, "_id": id_})


def todo_delete(id_):
    with connect() as conn:
        conn.execute("DELETE FROM todo_subtasks WHERE todo_id = ?", (id_,))
        conn.execute("DELETE FROM todos WHERE id = ?", (id_,))


def todo_open_by_auto_source(auto_source):
    """Returns the open todo (if any) linked to a given suggestion key — used
    by the suggestion engine to avoid double-surfacing once the user has
    converted a suggestion into a real task."""
    if not auto_source:
        return None
    with connect() as conn:
        r = conn.execute(
            "SELECT * FROM todos WHERE auto_source = ? AND status = 'open' LIMIT 1",
            (auto_source,),
        ).fetchone()
    return dict(r) if r else None


def todo_due_open(today_iso):
    """Tasks that are open and due today or earlier and not already notified
    today — feeds the daily reminder digest."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM todos WHERE status = 'open' "
            "AND due_date_iso IS NOT NULL AND due_date_iso <= ? "
            "AND (notified_at IS NULL OR notified_at < ?) "
            "ORDER BY due_date_iso, urgency",
            (today_iso, today_iso),
        ).fetchall()
    return [dict(r) for r in rows]


def todo_mark_notified(ids, today_iso):
    if not ids:
        return
    placeholders = ",".join("?" * len(ids))
    with connect() as conn:
        conn.execute(
            f"UPDATE todos SET notified_at = ? WHERE id IN ({placeholders})",
            (today_iso, *ids),
        )


def subtask_insert(row, now_iso):
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO todo_subtasks (id, todo_id, title, done, order_idx, created_at)
            VALUES (:id, :todo_id, :title, :done, :order_idx, :created_at)
            """,
            {"done": 0, "order_idx": 0, **row, "created_at": now_iso},
        )


def subtask_list(todo_id):
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM todo_subtasks WHERE todo_id = ? ORDER BY order_idx, created_at",
            (todo_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def subtask_list_for_todos(todo_ids):
    """Batched: {todo_id: [subtask, ...]} — avoids N+1 on the list page."""
    ids = [str(x) for x in todo_ids if x]
    if not ids:
        return {}
    placeholders = ",".join("?" * len(ids))
    with connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM todo_subtasks WHERE todo_id IN ({placeholders}) "
            f"ORDER BY order_idx, created_at",
            ids,
        ).fetchall()
    out = {}
    for r in rows:
        out.setdefault(r["todo_id"], []).append(dict(r))
    return out


def subtask_toggle(id_):
    with connect() as conn:
        conn.execute(
            "UPDATE todo_subtasks SET done = 1 - done WHERE id = ?", (id_,)
        )


def subtask_update(id_, fields):
    if not fields:
        return
    sets = ", ".join(f"{k}=:{k}" for k in fields)
    with connect() as conn:
        conn.execute(f"UPDATE todo_subtasks SET {sets} WHERE id=:_id", {**fields, "_id": id_})


def subtask_delete(id_):
    with connect() as conn:
        conn.execute("DELETE FROM todo_subtasks WHERE id = ?", (id_,))


def suggestion_dismiss(key, dismissed_until_iso, now_iso):
    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO todo_suggestion_dismissals "
            "(suggestion_key, dismissed_until, created_at) VALUES (?, ?, ?)",
            (key, dismissed_until_iso, now_iso),
        )


def suggestion_active_dismissals(today_iso):
    """Returns {key: dismissed_until} for dismissals that haven't expired."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT suggestion_key, dismissed_until FROM todo_suggestion_dismissals "
            "WHERE dismissed_until > ?",
            (today_iso,),
        ).fetchall()
    return {r["suggestion_key"]: r["dismissed_until"] for r in rows}


# --- CrowdVolt auto-pricer -------------------------------------------------
# Same shape as the viagogo pricer helpers above; ask_uqid plays listing_id
# and ticket_type plays section.

def cv_listings_upsert(rows, now_iso):
    """Rewrite the active-asks mirror from a sell_active fetch. Rows not in
    `rows` keep their old last_seen_at (sold/deleted asks age out in the UI
    the same way viagogo_listings rows do)."""
    with connect() as conn:
        for r in rows:
            conn.execute(
                """
                INSERT INTO cv_active_listings (ask_uqid, event_uqid, event_name,
                    event_date, event_location, ticket_type, price, qty,
                    all_in_price, last_seen_at)
                VALUES (:ask_uqid, :event_uqid, :event_name, :event_date,
                        :event_location, :ticket_type, :price, :qty,
                        :all_in_price, :last_seen_at)
                ON CONFLICT(ask_uqid) DO UPDATE SET
                    event_uqid = excluded.event_uqid,
                    event_name = excluded.event_name,
                    event_date = excluded.event_date,
                    event_location = excluded.event_location,
                    ticket_type = excluded.ticket_type,
                    price = excluded.price,
                    qty = excluded.qty,
                    all_in_price = excluded.all_in_price,
                    last_seen_at = excluded.last_seen_at
                """,
                {**r, "last_seen_at": now_iso},
            )


def cv_listings_all():
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM cv_active_listings ORDER BY event_date"
        ).fetchall()
    return [dict(r) for r in rows]


def cv_pricer_config_get(ask_uqid):
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM cv_pricer_config WHERE ask_uqid = ?",
            (str(ask_uqid),),
        ).fetchone()
    return dict(row) if row else None


def cv_pricer_config_all():
    with connect() as conn:
        rows = conn.execute("SELECT * FROM cv_pricer_config").fetchall()
    return {r["ask_uqid"]: dict(r) for r in rows}


_CV_PRICER_CONFIG_FIELDS = (
    "enabled", "floor_price", "allow_raise", "paused", "paused_reason",
    "paused_at", "last_set_price", "last_set_at", "no_drop_cap",
)


def cv_pricer_config_set(ask_uqid, fields, now_iso):
    fields = {k: v for k, v in fields.items() if k in _CV_PRICER_CONFIG_FIELDS}
    with connect() as conn:
        existing = conn.execute(
            "SELECT ask_uqid FROM cv_pricer_config WHERE ask_uqid = ?",
            (str(ask_uqid),),
        ).fetchone()
        if existing:
            sets = ", ".join(f"{k} = :{k}" for k in fields)
            conn.execute(
                f"UPDATE cv_pricer_config SET {sets}, updated_at = :updated_at "
                "WHERE ask_uqid = :ask_uqid",
                {**fields, "updated_at": now_iso, "ask_uqid": str(ask_uqid)},
            )
        else:
            cols = ["ask_uqid", *fields.keys(), "updated_at"]
            conn.execute(
                f"INSERT INTO cv_pricer_config ({', '.join(cols)}) "
                f"VALUES ({', '.join(':' + c for c in cols)})",
                {**fields, "ask_uqid": str(ask_uqid), "updated_at": now_iso},
            )


def cv_pricer_log_add(row, now_iso):
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO cv_pricer_log (ask_uqid, event_uqid, ticket_type,
                                       old_price, new_price, competitor_price,
                                       action, detail, dry_run, created_at)
            VALUES (:ask_uqid, :event_uqid, :ticket_type,
                    :old_price, :new_price, :competitor_price,
                    :action, :detail, :dry_run, :created_at)
            """,
            {
                "event_uqid": None, "ticket_type": None, "old_price": None,
                "new_price": None, "competitor_price": None, "detail": None,
                "dry_run": 0,
                **row,
                "created_at": now_iso,
            },
        )


def cv_pricer_writes_since(ask_uqid, since_iso):
    """Real price changes since `since_iso`, oldest first — the drop guard's
    window reconstruction (see pricer_writes_since)."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT action, old_price, new_price, created_at FROM cv_pricer_log "
            "WHERE ask_uqid = ? AND dry_run = 0 "
            "AND action IN ('lower', 'floor_clamp', 'rate_clamp', 'raise', 'manual_adopt') "
            "AND created_at >= ? ORDER BY id",
            (str(ask_uqid), since_iso),
        ).fetchall()
    return [dict(r) for r in rows]


def cv_pricer_log_recent(limit=100, ask_uqid=None):
    with connect() as conn:
        if ask_uqid:
            rows = conn.execute(
                "SELECT * FROM cv_pricer_log WHERE ask_uqid = ? "
                "ORDER BY id DESC LIMIT ?",
                (str(ask_uqid), limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM cv_pricer_log ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
    return [dict(r) for r in rows]


def cv_market_snapshot_set(event_uqid, rows_json, now_iso):
    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO cv_market_snapshots "
            "(event_uqid, fetched_at, rows_json) VALUES (?, ?, ?)",
            (str(event_uqid), now_iso, rows_json),
        )


def cv_market_snapshot_get(event_uqid):
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM cv_market_snapshots WHERE event_uqid = ?",
            (str(event_uqid),),
        ).fetchone()
    return dict(row) if row else None
