"""Account vault — encrypted storage for reseller-site logins.

Secrets (password, notes, address, card) are Fernet-encrypted with
KARTIS_VAULT_KEY from .env; non-secret columns (platform, label, username,
url, status) stay plaintext so the /vault list renders without decrypting.
The page is gated by a 4-6 digit PIN (salted PBKDF2 hash in app_settings,
key 'vault_pin'); a correct PIN issues an in-memory session token the
browser sends as X-Vault-Token. The PIN is an access gate only — the .env
key is what protects DB backups / the OneDrive repo copy.
"""

import base64
import hashlib
import os
import secrets
import threading
import time

import db

SECRET_FIELDS = ("password", "notes", "address", "card")

TOKEN_TTL_SECONDS = 12 * 3600
MAX_PIN_ATTEMPTS = 5
LOCKOUT_SECONDS = 300

_lock = threading.Lock()
_tokens = {}          # token -> expiry epoch
_attempts = {"count": 0, "locked_until": 0.0}


# ---------------------------------------------------------------- crypto

def _fernet():
    from cryptography.fernet import Fernet
    key = (os.environ.get("KARTIS_VAULT_KEY") or "").strip()
    if not key:
        raise RuntimeError(
            "KARTIS_VAULT_KEY is not set — generate one with "
            "python -c \"from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())\" and add it to .env"
        )
    return Fernet(key.encode())


def encrypt(plaintext):
    if not plaintext:
        return ""
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt(token):
    if not token:
        return ""
    return _fernet().decrypt(token.encode("ascii")).decode("utf-8")


# ---------------------------------------------------------------- PIN

def _hash_pin(pin, salt):
    dk = hashlib.pbkdf2_hmac("sha256", pin.encode(), salt, 200_000)
    return base64.b64encode(dk).decode()


def pin_is_set():
    return bool(db.setting_get("vault_pin"))


def set_pin(pin, now_iso):
    salt = secrets.token_bytes(16)
    stored = base64.b64encode(salt).decode() + "$" + _hash_pin(pin, salt)
    db.setting_set("vault_pin", stored, now_iso)


def check_pin(pin):
    stored = db.setting_get("vault_pin")
    if not stored or "$" not in stored:
        return False
    salt_b64, want = stored.split("$", 1)
    got = _hash_pin(pin, base64.b64decode(salt_b64))
    return secrets.compare_digest(got, want)


def lockout_remaining():
    with _lock:
        rem = _attempts["locked_until"] - time.time()
    return max(0, int(rem))


def record_attempt(ok):
    """Track a PIN attempt; returns seconds locked out (0 if not)."""
    with _lock:
        if ok:
            _attempts["count"] = 0
            _attempts["locked_until"] = 0.0
            return 0
        _attempts["count"] += 1
        if _attempts["count"] >= MAX_PIN_ATTEMPTS:
            _attempts["count"] = 0
            _attempts["locked_until"] = time.time() + LOCKOUT_SECONDS
            return LOCKOUT_SECONDS
    return 0


# ---------------------------------------------------------------- tokens

def issue_token():
    tok = secrets.token_urlsafe(32)
    now = time.time()
    with _lock:
        for t in [t for t, exp in _tokens.items() if exp < now]:
            del _tokens[t]
        _tokens[tok] = now + TOKEN_TTL_SECONDS
    return tok


def token_valid(tok):
    if not tok:
        return False
    with _lock:
        exp = _tokens.get(tok)
        if exp is None:
            return False
        if exp < time.time():
            del _tokens[tok]
            return False
    return True


# ---------------------------------------------------------------- CRUD

def list_accounts():
    """All accounts, secrets replaced by has_* flags."""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM vault_accounts ORDER BY platform, label, id"
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        for f in SECRET_FIELDS:
            d["has_" + f] = bool(d.pop(f + "_enc", ""))
        out.append(d)
    return out


def get_secrets(id_):
    """Decrypt all secret fields of one account (edit form / copy)."""
    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM vault_accounts WHERE id = ?", (id_,)
        ).fetchone()
    if not row:
        return None
    return {f: decrypt(row[f + "_enc"]) for f in SECRET_FIELDS}


def add_account(fields, now_iso):
    with db.connect() as conn:
        cur = conn.execute(
            """INSERT INTO vault_accounts
               (platform, label, username, phone, login_url, status,
                password_enc, notes_enc, address_enc, card_enc,
                created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                fields.get("platform", "").strip(),
                fields.get("label", "").strip(),
                fields.get("username", "").strip(),
                fields.get("phone", "").strip(),
                fields.get("login_url", "").strip(),
                fields.get("status", "active").strip() or "active",
                encrypt(fields.get("password", "")),
                encrypt(fields.get("notes", "")),
                encrypt(fields.get("address", "")),
                encrypt(fields.get("card", "")),
                now_iso,
                now_iso,
            ),
        )
        return cur.lastrowid


def update_account(id_, fields, now_iso):
    sets, vals = [], []
    for col in ("platform", "label", "username", "phone", "login_url", "status"):
        if col in fields:
            sets.append(f"{col} = ?")
            vals.append((fields[col] or "").strip())
    for f in SECRET_FIELDS:
        if f in fields:
            sets.append(f"{f}_enc = ?")
            vals.append(encrypt(fields[f] or ""))
    if not sets:
        return
    sets.append("updated_at = ?")
    vals.append(now_iso)
    vals.append(id_)
    with db.connect() as conn:
        conn.execute(
            f"UPDATE vault_accounts SET {', '.join(sets)} WHERE id = ?", vals
        )


def delete_account(id_):
    with db.connect() as conn:
        conn.execute("DELETE FROM vault_accounts WHERE id = ?", (id_,))


# ------------------------------------------------ TM accounts (/tm page)

TM_SECRET_FIELDS = ("password", "proxy", "cc", "address")
_TM_PLAIN = ("account_no", "email", "acct_source", "proxy_provider",
             "card_provider", "notes")


def _tm_enc_col(f):
    return {"cc": "cc_enc"}.get(f, f + "_enc")


def tm_list():
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM tm_accounts "
            "ORDER BY CAST(account_no AS INTEGER), account_no, id"
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        for f in TM_SECRET_FIELDS:
            d["has_" + f] = bool(d.pop(_tm_enc_col(f), ""))
        out.append(d)
    return out


def tm_get_secrets(id_):
    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM tm_accounts WHERE id = ?", (id_,)
        ).fetchone()
    if not row:
        return None
    return {f: decrypt(row[_tm_enc_col(f)]) for f in TM_SECRET_FIELDS}


def tm_add(fields, now_iso):
    with db.connect() as conn:
        cur = conn.execute(
            """INSERT INTO tm_accounts
               (account_no, email, acct_source, proxy_provider, card_provider,
                notes, password_enc, proxy_enc, cc_enc, address_enc,
                created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            tuple((fields.get(c) or "").strip() for c in _TM_PLAIN)
            + tuple(encrypt(fields.get(f, "")) for f in TM_SECRET_FIELDS)
            + (now_iso, now_iso),
        )
        return cur.lastrowid


def tm_update(id_, fields, now_iso):
    sets, vals = [], []
    for col in _TM_PLAIN:
        if col in fields:
            sets.append(f"{col} = ?")
            vals.append((fields[col] or "").strip())
    for f in TM_SECRET_FIELDS:
        if f in fields:
            sets.append(f"{_tm_enc_col(f)} = ?")
            vals.append(encrypt(fields[f] or ""))
    if not sets:
        return
    sets.append("updated_at = ?")
    vals.extend([now_iso, id_])
    with db.connect() as conn:
        conn.execute(
            f"UPDATE tm_accounts SET {', '.join(sets)} WHERE id = ?", vals
        )


def tm_delete(id_):
    with db.connect() as conn:
        conn.execute("DELETE FROM tm_accounts WHERE id = ?", (id_,))
