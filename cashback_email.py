"""Parse Capital One cash-back redemption emails into cashback_entries fields.

These emails are auto-forwarded into the same Gmail inbox the ticket intake
polls (see mail_intake.run_intake). They are NOT ticket purchases, so the
intake loop routes them here instead of into pending_intake: we extract the
redeemed amount, the card, and a stable dedup key, then add a row straight to
the /cashback page.

Pure functions only — no DB or network. mail_intake owns the IMAP fetch and
the db.insert_cashback_entry call.

Sample email this was built against:

    From:    Capital One | Savor <capitalone@notification.capitalone.com>
    Subject: Your $168.00 statement credit is on the way!
    Body:    ... Savor Credit Card...3011
             Congrats! You're getting $168.00 back in your account.
             Transaction details
             Account ending        3011
             Rewards order number  1XW-QS49
             Account credit        $168.00
"""
import re

# Sender substring that marks a Capital One notification.
_SENDER_HINT = "capitalone"

# Redemption markers — at least one must be present (beyond the sender) for us
# to treat the mail as a cash-back redemption rather than a statement, fraud
# alert, or marketing blast.
_REDEMPTION_MARKERS = (
    "rewards order number",
    "statement credit is on the way",
    "back in your account",
    "account credit",
)

# Amount patterns, in priority order. The "Account credit" line is the most
# authoritative; subject and the "getting $X back" sentence are fallbacks.
_AMOUNT_RXS = (
    re.compile(r"account\s*credit\s*\$?\s*([\d,]+\.\d{2})", re.I),
    re.compile(r"getting\s*\$\s*([\d,]+\.\d{2})\s*back", re.I),
    re.compile(r"\$\s*([\d,]+\.\d{2})"),  # generic, e.g. the subject line
)

# Rewards order number → stable dedup key (survives re-forwarding, unlike the
# Message-ID which Gmail rewrites).
_ORDER_RX = re.compile(r"rewards\s*order\s*number\s*[:\s]*([A-Z0-9][A-Z0-9-]+)", re.I)

# Card name fallbacks: display-name after the pipe ("Capital One | Savor"), or
# a "<Word> Credit Card" mention in the body.
_DISPLAY_CARD_RX = re.compile(r"capital\s*one\s*\|\s*([A-Za-z0-9 ]+?)\s*<", re.I)
_DISPLAY_CARD_PLAIN_RX = re.compile(r"capital\s*one\s*\|\s*([A-Za-z0-9 ]+)$", re.I)
# Card line in the body, e.g. "Savor Credit Card...3011" or
# "Spark Cash Plus card...3134" — capture the card name before "[Credit ]Card...####".
_BODY_CARD_RX = re.compile(r"([A-Z][A-Za-z0-9 ]+?)\s+(?:[Cc]redit\s+)?[Cc]ard\s*(?:\.{2,}|…)\s*\d")


def is_capitalone_cashback(from_addr, subject, body):
    """True only for Capital One redemption emails (sender + a redemption marker)."""
    blob = f"{from_addr or ''}".lower()
    if _SENDER_HINT not in blob:
        return False
    haystack = f"{subject or ''}\n{body or ''}".lower()
    return any(m in haystack for m in _REDEMPTION_MARKERS)


def _extract_amount(subject, body):
    # Try the authoritative body patterns first, then fall back to the subject.
    for rx in _AMOUNT_RXS[:-1]:
        m = rx.search(body or "")
        if m:
            return float(m.group(1).replace(",", ""))
    generic = _AMOUNT_RXS[-1]
    m = generic.search(body or "") or generic.search(subject or "")
    if m:
        return float(m.group(1).replace(",", ""))
    return None


def _extract_card_name(from_addr, body):
    token = ""
    for rx in (_DISPLAY_CARD_RX, _DISPLAY_CARD_PLAIN_RX):
        m = rx.search(from_addr or "")
        if m:
            token = m.group(1).strip()
            break
    if not token:
        m = _BODY_CARD_RX.search(body or "")
        if m:
            token = m.group(1).strip()
    if not token:
        return "Capital One"
    if token.lower().startswith("capital one"):
        return token
    return f"Capital One {token}"


def _extract_source_ref(body, message_id):
    m = _ORDER_RX.search(body or "")
    if m:
        return f"capitalone:{m.group(1).strip()}"
    if message_id:
        return f"capitalone-msgid:{message_id}"
    return None


def parse_cashback(from_addr, subject, body, received_at_iso, message_id=None):
    """Return {amount, date_iso, card_name, source_ref} or None if unparseable.

    None means we couldn't pull a dollar amount or a dedup key — caller skips
    it rather than inserting a junk row.
    """
    amount = _extract_amount(subject, body)
    if not amount or amount <= 0:
        return None
    source_ref = _extract_source_ref(body, message_id)
    if not source_ref:
        return None
    date_iso = (received_at_iso or "")[:10]
    return {
        "amount": amount,
        "date_iso": date_iso,
        "card_name": _extract_card_name(from_addr, body),
        "source_ref": source_ref,
    }


if __name__ == "__main__":
    # Probe against the sample email (mirrors the *.py probe convention).
    from datetime import datetime, timezone

    _from = "Capital One | Savor <capitalone@notification.capitalone.com>"
    _subject = "Your $168.00 statement credit is on the way!"
    _body = (
        "Capital One | Savor\nSavor\nSavor Credit Card...3011\n\n"
        "Congrats! You’re getting $168.00 back in your account.\n\n"
        "Transaction details\nAccount ending\t3011\n"
        "Rewards order number\t1XW-QS49\nAccount credit\t$168.00\n"
    )
    _now = datetime.now(timezone.utc).isoformat()
    print("is_cashback:", is_capitalone_cashback(_from, _subject, _body))
    print(parse_cashback(_from, _subject, _body, _now, message_id="<abc@mail>"))
