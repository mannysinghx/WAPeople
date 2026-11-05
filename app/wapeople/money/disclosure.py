"""Reduce a personal financial affairs statement to the fields we publish.

The statements are public records, but they're also full of things that don't help anyone follow
the money and could hurt a real person: home addresses, parcel numbers, and the names of spouses
and children. This is a whitelist: a field appears on the site only if it's named here, so a new
field in the source stays private until someone decides otherwise.

Kept: who paid the official, what they own and owe, gifts, and clients they lobby for.
Dropped: every street address and postal code, spouse and dependent names, anything reported
only for a dependent, property buyers' names, and judges' and officers' property locations.
"""
from __future__ import annotations

import json

from .clean import free_text as text
from .clean import place

WHO = {"filer": "Official", "spouse": "Spouse or partner"}


def _who(item: dict) -> list[str]:
    return [WHO[w] for w in (item.get("who") or []) if w in WHO]


def _keep(item: dict) -> bool:
    """False for anything reported only for a dependent. Unlabeled items stay."""
    who = item.get("who") or []
    return not who or any(w in WHO for w in who)


def _place(item: dict) -> dict:
    return {"city": place(item.get("city")), "state": text(item.get("state"))}


def _section_items(section) -> list[dict]:
    if isinstance(section, dict):
        section = section.get("data")
    return [i for i in (section or []) if isinstance(i, dict) and _keep(i)]


def _pairs(items, *fields) -> list[dict]:
    out = []
    for i in items or []:
        if isinstance(i, dict):
            row = {f: text(i.get(f)) for f in fields}
            if any(row.values()):
                out.append(row)
    return out


def sanitize(raw: dict) -> dict:
    s = raw.get("statement", raw) if isinstance(raw, dict) else {}
    private_location = bool(s.get("judge_or_law_enforcement"))

    income = [{"who": _who(i), "source": text(i.get("name")), "for": text(i.get("why")),
               "amount": text(i.get("income")), "category": text(i.get("category")), **_place(i)}
              for i in _section_items(s.get("income"))]
    accounts = [{"who": _who(i), "name": text(i.get("name")), "category": text(i.get("category")),
                 "assets": _pairs(i.get("assets"), "nature", "value", "income"), **_place(i)}
                for i in _section_items(s.get("accounts"))]
    businesses = [{"who": _who(i), "name": text(i.get("operating_name")) or text(i.get("legal_name")),
                   "legal_name": text(i.get("legal_name")), "what": text(i.get("description")),
                   "position": text(i.get("position")), "ownership": text(i.get("percentage_of_ownership")),
                   "category": text(i.get("category")),
                   "paid_by_government": _pairs(i.get("office_payments"), "purpose", "amount"),
                   "paid_by_state_agencies": _pairs(i.get("agency_payments"), "agency", "purpose"),
                   "paid_by_businesses": _pairs(i.get("business_payments"), "business", "purpose"), **_place(i)}
                  for i in _section_items(s.get("business_associations"))]
    real_estate = []
    for i in _section_items(s.get("real_estate")):
        loc = i.get("location") if isinstance(i.get("location"), dict) else {}
        real_estate.append({
            "who": _who(i), "assessed_value": text(i.get("assessed_value")),
            "purchased": bool(i.get("purchased")), "sold": bool(i.get("sold")),
            "city": None if private_location else place(loc.get("city")),
            "county": None if private_location else text(loc.get("county")),
            "lenders": _pairs(i.get("creditors"), "name", "original_amount", "current_amount"),
        })
    creditors = [{"who": _who(i), "name": text(i.get("name")), "original": text(i.get("original_amount")),
                  "owed": text(i.get("ending_amount")), "terms": text(i.get("payment_terms")),
                  "security": text(i.get("security_given")), **_place(i)}
                 for i in _section_items(s.get("creditors"))]
    gifts = [{"who": _who(i), "donor": text(i.get("donor_name")), "value": text(i.get("value")),
              "what": text(i.get("description")), "date": text(i.get("date_received")), **_place(i)}
             for i in _section_items(s.get("gifts"))]
    lobbying = [{"who": _who(i), "for": text(i.get("for")), "purpose": text(i.get("purpose")),
                 "compensation": text(i.get("compensation"))}
                for i in _section_items(s.get("lobbies"))]
    return {"income": income, "accounts": accounts, "businesses": businesses,
            "real_estate": real_estate, "creditors": creditors, "gifts": gifts, "lobbying": lobbying,
            "location_withheld": private_location}


def from_json(value) -> dict | None:
    """Sanitized details from the dataset's JSON text, or None for older paper filings."""
    if not value:
        return None
    try:
        return sanitize(json.loads(value))
    except (ValueError, TypeError):
        return None
