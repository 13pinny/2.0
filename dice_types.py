"""Ticket-type identity for DICE purchases and the resale sales of them.

A "type" is the set of DISTINCTIVE words in a ticket label: "Early Entry GA"
-> {early}, "General Admission - Tier 2" -> {} (plain GA), "VIP Balcony" ->
{vip, balcony}. Price tiers, delivery methods and filler words are generic,
so a GA bought at $60 (tier 1) and $75 (tier 2) is ONE type -- which is what
lets their cost be averaged -- while Early Entry stays its own type.
"""
import re

_GENERIC = {
    "general", "admission", "ga", "ticket", "tickets", "entry", "standard",
    "regular", "tier", "phase", "release", "wave", "round", "advance", "presale",
    "final", "first", "second", "third", "last", "chance", "door", "doors",
    "e", "etickets", "eticket", "mobile", "transfer", "paper", "hard", "pdf",
    "x", "the", "a", "an", "and", "of", "for", "to", "only", "admit", "one",
    "person", "per", "inc", "incl", "fees", "fee", "floor", "standing", "room",
}


def type_key(*labels):
    """frozenset of distinctive words across the given label strings."""
    text = " ".join(l for l in labels if l).lower()
    text = re.sub(r"early\s*bird", " ", text)  # a price tier, not early entry
    words = re.findall(r"[a-z]+", text)
    return frozenset(w for w in words if w not in _GENERIC)


def purchase_type_key(ticket_type):
    """A DICE purchase's type, or None when one email bought several types
    ("2 × GA, 1 × Early Entry") so no single type applies."""
    if ticket_type and "×" in ticket_type and "," in ticket_type:
        return None
    return type_key(ticket_type)


def describe(key):
    if key is None:
        return "mixed"
    return " ".join(sorted(key)) or "GA"
