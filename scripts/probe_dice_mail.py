"""Probe the intake inbox for DICE (dice.fm) emails so the parser regexes in
dice_email.py can be built against real samples.

Usage:
    .venv\\Scripts\\python scripts\\probe_dice_mail.py [--days N] [--full]

Fetches two ways (mirrors how run_intake would see them):
  1. imap_fetch_from("dice.fm", days)  — auto-forwards keep From: dice.fm
  2. imap_fetch_new(limit=200, days)   — manual forwards (outer From: is the
     user); dice.fm only shows up after _unwrap_forwarded.

Prints, per message: uid, outer/inner from + subject, _buyer_email, and the
unwrapped body text (truncated unless --full).
"""
import argparse
import io
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from email import message_from_bytes

import mail_intake as mi


def _dump(tag, uid, raw, full):
    parsed = mi.parse_email(raw)
    eff_from, eff_subject, eff_body = mi._unwrap_forwarded(parsed)
    if "dice" not in (eff_from or "").lower() and "dice" not in (eff_subject or "").lower() \
            and "dice.fm" not in (eff_body or "").lower():
        return False
    msg = message_from_bytes(raw)
    buyer = mi._buyer_email(msg, parsed.get("body") or "")
    print("=" * 78)
    print(f"[{tag}] uid={uid}")
    print(f"outer From:    {parsed['from']}")
    print(f"outer Subject: {parsed['subject']}")
    print(f"inner From:    {eff_from}")
    print(f"inner Subject: {eff_subject}")
    print(f"buyer_email:   {buyer}")
    print(f"received_at:   {parsed['received_at']}")
    print(f"message_id:    {parsed['message_id']}")
    print("-" * 78)
    body = eff_body or ""
    print(body if full else body[:4000])
    print()
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--full", action="store_true")
    args = ap.parse_args()

    hits = 0
    seen_mids = set()
    print(f"# targeted FROM dice.fm search, last {args.days}d")
    for uid, raw, mid in mi.imap_fetch_from("dice.fm", args.days):
        if mid:
            seen_mids.add(mid)
        if _dump("from:dice.fm", uid, raw, args.full):
            hits += 1

    print(f"# general scan (manual forwards), last {args.days}d")
    for uid, raw, mid in mi.imap_fetch_new(limit=200, lookback_days=args.days):
        if mid and mid in seen_mids:
            continue
        if _dump("general", uid, raw, args.full):
            hits += 1

    print(f"total dice-looking messages: {hits}")


if __name__ == "__main__":
    main()
