r"""Probe the intake inbox for CrowdVolt emails so the parser regexes in a
future crowdvolt_email.py can be built against real samples.

Usage:
    .venv\Scripts\python scripts\probe_cv_mail.py [--days N] [--full]
                                                    [--folder "[Gmail]/All Mail"]
                                                    [--subject-only]

Mirrors scripts/probe_dice_mail.py. Fetches two ways, the same two run_intake
would see:
  1. imap_fetch_from("crowdvolt", days)  — auto-forwards keep From: crowdvolt.com
  2. imap_fetch_new(limit=500, days)     — manual forwards (outer From: is the
     user); crowdvolt only shows up after _unwrap_forwarded.

Default output is a SURVEY: one line per message plus a tally of normalised
inner subjects, which is what tells us how many distinct CrowdVolt mail
templates exist (sold / payout / cancelled / ...) before we write a parser.
Pass --full to also dump each unwrapped body.

The question this probe exists to answer: do CrowdVolt sale emails carry an
ORDER NUMBER? crowdvolt_sales.id is the order_id (db.py:309), so if the mail
has no order number the email path needs a synthetic key and loses the
fee/payout/ticket_source columns /cvfees reads.
"""
import argparse
import io
import re
import sys
from collections import Counter

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from email import message_from_bytes

import mail_intake as mi

# Anything that smells like CrowdVolt in the sender, subject or body.
_CV_RX = re.compile(r"crowdvolt", re.I)

# Candidate order-number shapes, cast wide on purpose — we are looking for
# WHETHER an id exists, not yet committing to its format.
_ORDER_RXES = [
    re.compile(r"\border\s*(?:number|no\.?|#|id)\s*[:#]?\s*([A-Za-z0-9][A-Za-z0-9-]{4,})", re.I),
    re.compile(r"\b(?:confirmation|reference|ref)\s*(?:number|no\.?|#|code)?\s*[:#]?\s*([A-Za-z0-9][A-Za-z0-9-]{4,})", re.I),
    re.compile(r"\buqid\b\W{0,3}([A-Za-z0-9-]{8,})", re.I),
    re.compile(r"#\s?([A-Z0-9]{6,})\b"),
]

_MONEY_RX = re.compile(r"[\$€£]\s?\d[\d,]*(?:\.\d{2})?")
_QTY_RX = re.compile(r"\b(\d{1,2})\s*(?:x|×|tickets?)\b", re.I)
# Strip digits/money out of subjects so templates collapse into one bucket.
_NORM_RX = re.compile(r"[\$€£]?\d[\d,.:]*")


def _norm_subject(s):
    return _NORM_RX.sub("#", (s or "").strip())[:90]


def _order_ids(text):
    out = []
    for rx in _ORDER_RXES:
        out.extend(m.group(1) for m in rx.finditer(text or ""))
    # Preserve order, drop dupes.
    return list(dict.fromkeys(out))


def _is_cv(parsed, eff_from, eff_subject, eff_body):
    return bool(
        _CV_RX.search(eff_from or "")
        or _CV_RX.search(eff_subject or "")
        or _CV_RX.search(parsed.get("from") or "")
        or _CV_RX.search(parsed.get("subject") or "")
        or _CV_RX.search(eff_body or "")
    )


def _dump(tag, uid, raw, args, subjects, order_hits, no_order):
    parsed = mi.parse_email(raw)
    eff_from, eff_subject, eff_body = mi._unwrap_forwarded(parsed)
    if not _is_cv(parsed, eff_from, eff_subject, eff_body):
        return False

    body = eff_body or ""
    subjects[_norm_subject(eff_subject)] += 1
    ids = _order_ids(f"{eff_subject}\n{body}")
    if ids:
        order_hits.append((eff_subject, ids[:3]))
    else:
        no_order.append(eff_subject)

    msg = message_from_bytes(raw)
    buyer = mi._buyer_email(msg, parsed.get("body") or "")
    print("=" * 78)
    print(f"[{tag}] uid={uid}  received={parsed['received_at']}")
    print(f"outer From:    {parsed['from']}")
    print(f"outer Subject: {parsed['subject']}")
    print(f"inner From:    {eff_from}")
    print(f"inner Subject: {eff_subject}")
    print(f"buyer_email:   {buyer}")
    print(f"message_id:    {parsed['message_id']}")
    print(f"order-ish ids: {ids or '(NONE FOUND)'}")
    print(f"money tokens:  {_MONEY_RX.findall(body)[:8]}")
    print(f"qty tokens:    {_QTY_RX.findall(body)[:6]}")
    if not args.subject_only:
        print("-" * 78)
        print(body if args.full else body[:3000])
    print()
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--full", action="store_true",
                    help="dump the whole unwrapped body, not the first 3000 chars")
    ap.add_argument("--subject-only", action="store_true",
                    help="headers + extracted tokens only, no body text")
    ap.add_argument("--folder", default=None,
                    help='IMAP folder to scan (default KARTIS_INTAKE_FOLDER or '
                         'INBOX). Try "[Gmail]/All Mail" if a filter archives them.')
    args = ap.parse_args()

    if args.folder:
        mi.INTAKE_FOLDER = args.folder
    print(f"# folder: {mi.INTAKE_FOLDER}   window: last {args.days}d")

    subjects = Counter()
    order_hits, no_order = [], []
    hits = 0
    seen_mids = set()

    print(f"\n# targeted FROM crowdvolt search, last {args.days}d")
    try:
        for uid, raw, mid in mi.imap_fetch_from("crowdvolt", args.days):
            if mid:
                seen_mids.add(mid)
            if _dump("from:crowdvolt", uid, raw, args, subjects, order_hits, no_order):
                hits += 1
    except Exception as e:
        print(f"!! targeted search failed: {type(e).__name__}: {e}")

    print(f"\n# general scan (manual forwards), last {args.days}d")
    try:
        for uid, raw, mid in mi.imap_fetch_new(limit=500, lookback_days=args.days):
            if mid and mid in seen_mids:
                continue
            if _dump("general", uid, raw, args, subjects, order_hits, no_order):
                hits += 1
    except Exception as e:
        print(f"!! general scan failed: {type(e).__name__}: {e}")

    print("=" * 78)
    print(f"total crowdvolt-looking messages: {hits}")
    print(f"\n-- distinct subject templates ({len(subjects)}) --")
    for subj, n in subjects.most_common():
        print(f"{n:4d}  {subj}")
    print(f"\n-- messages WITH an order-ish id: {len(order_hits)} --")
    for subj, ids in order_hits[:15]:
        print(f"  {ids}  <- {_norm_subject(subj)}")
    print(f"\n-- messages WITHOUT any order-ish id: {len(no_order)} --")
    for subj in Counter(_norm_subject(s) for s in no_order).most_common(15):
        print(f"  {subj[1]:4d}  {subj[0]}")


if __name__ == "__main__":
    main()
