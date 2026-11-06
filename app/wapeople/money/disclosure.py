"""Reduce a personal financial affairs statement to the fields we publish.

The statements are public records, but they're also full of things that don't help anyone follow
the money and could hurt a real person: home addresses, parcel numbers, and the names of spouses
and children. This is a whitelist: a field appears on the site only if it's named here, so a new
field in the source stays private until someone decides otherwise.

Kept: who paid the official, what they own and owe, gifts, and clients they lobby for.
Dropped: every street address and postal code, spouse and dependent names, anything reported
only for a dependent or labeled as a child's, property buyers' names, and judges' and officers'
property locations.

Filers also type those same names and addresses into fields we keep (an asset described as 'for
Leah Zahn', a creditor named '9800 Roosevelt Condominiums'), so what comes out of the whitelist is
checked again against the statement's own spouse, dependent and address fields.
"""
from __future__ import annotations

import json
import re

from .clean import ADDRESS_REMOVED
from .clean import free_text as text
from .clean import person_shaped, place
from .link import NICKNAMES, name_tokens

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


# ---- Family names ----------------------------------------------------------------------------

FAMILY_REMOVED = "[family member]"
PERSONAL_REMOVED = "[personal detail removed]"
_NOT_NAME_WORDS = {"NONE", "NA", "N", "A", "NOT", "APPLICABLE", "NO", "YES", "SINGLE", "DIVORCED", "WIDOWED", "DECEASED",
                   "SPOUSE", "PARTNER", "JR", "SR", "II", "III", "IV", "MR", "MRS", "MS", "DR"}


def _names(value) -> list[str]:
    """The names in a statement's spouse or dependents field: a string, a list of strings, or {'name': ...}."""
    if isinstance(value, str):
        return [" ".join(value.split())] if value.strip() else []
    if isinstance(value, dict):
        return _names(value.get("name"))
    if isinstance(value, list):
        return [n for v in value for n in _names(v)]
    return []


def _name_pattern(names: list[str], official: str | None) -> re.Pattern | None:
    """Matches each full name, and each part of one on its own, whole words only. The official's own
    names stay: a statement can say 'John Smith Consulting', and 'Smith' alone says nothing new."""
    own = set(name_tokens(official))
    alternatives = set()
    for name in names:
        parts = [p for p in re.findall(r"[^\W\d_][^\W\d_'’]*(?:['’][^\W\d_]+)?", name) if len(p) > 1]
        useful = [p for p in parts if not (_NOT_NAME_WORDS | own) & set(name_tokens(p))]
        if not useful:      # 'None', 'N/A', or a son with exactly his parent's name.
            continue
        if len(parts) > 1:
            alternatives.add(r"\s+".join(re.escape(p) + r"\.?" for p in re.findall(r"[^\s]+", name.strip(" .,"))))
        alternatives.update(re.escape(p) for p in useful)
        # Listed as Ted, written as Theodore.
        alternatives.update(NICKNAMES[t] for p in useful for t in name_tokens(p) if NICKNAMES.get(t, t) not in own | {t})
    if not alternatives:
        return None
    return re.compile(r"(?<![\w-])(?:" + "|".join(sorted(alternatives, key=len, reverse=True)) + r")(?![\w])", re.I)


# ---- Holdings that are a child's --------------------------------------------------------------

# Brokerages and banks with a person's name in them: a spouse named James doesn't make Raymond James family.
_FIRMS = re.compile(
    r"\b(?:Raymond James|Edward (?:D\. )?Jones|Charles Schwab|John Hancock|Morgan Stanley|T\. ?Rowe Price|Wells Fargo"
    r"|Merrill Lynch|Smith Barney|Goldman Sachs|Dean Witter|Franklin Templeton|William Blair)\b",
    re.I,
)
# 'Seattle Children's Hospital' isn't a child, and 'Johnson & Sons' isn't a son.
_CHILD = re.compile(
    r"(?<!& )(?<!and )\b(?:sons?|daughters?|child|children(?!['’]s)|grand(?:son|daughter|child|children)s?)\b(?!['’]s)"
    r"|\b(?:utma|ugma|fbo|f/b/o)\b|\bfor (?:the )?benefit of\b",
    re.I,
)
# Labeled for someone: '529 Plan - James Schwarmann', 'Aspen - 529', 'Education Tuition Account for Henry Pollet'.
# '529 Plan - Cash' and 'Fidelity 529' aren't.
_EDUCATION = r"(?i:\b(?:529|education\w*|tuition|college)\b)"
_EDUCATION_FOR_SOMEONE = re.compile(
    rf"{_EDUCATION}.*\bfor [A-Z]|{_EDUCATION}.*\s-\s*[A-Z][a-z]+ [A-Z][a-z]+\s*$|^\s*[A-Z][a-z]+\s*-\s*529\b"
)
# One entry lumps the filer's own brokerage accounts in with a child's. Only a child's name takes it out.
_ALSO_THE_FILERS = re.compile(r"\b(?:joint|filer)\b", re.I)


def _for_a_child(value, dependents: re.Pattern | None) -> bool:
    s = _FIRMS.sub("", value) if isinstance(value, str) else ""
    if dependents and dependents.search(s):
        return True
    return not _ALSO_THE_FILERS.search(s) and bool(_CHILD.search(s) or _EDUCATION_FOR_SOMEONE.search(s))


# ---- What's left to check in every kept string --------------------------------------------------

_DIRECTIONAL = r"(?:[NS][EW]?|[EW]|north|south|east|west|northeast|northwest|southeast|southwest)\.?"
_HOUSE_AND_STREET = re.compile(rf"^\s*(\d{{1,6}})[A-Za-z]?\s+(?:{_DIRECTIONAL}\s+(?=\S))?([^\s,.#]+)", re.I)


def _home_addresses(raw: dict, statement: dict) -> re.Pattern | None:
    """Each house number and first street word the statement gives for the official's home or property.
    A creditor named '9800 Roosevelt Condominiums' is that address minus the unit number."""
    person = raw.get("person") if isinstance(raw.get("person"), dict) else {}
    places = [person.get("address")]
    for i in statement.get("real_estate") or []:
        if isinstance(i, dict) and isinstance(i.get("location"), dict):
            places.append(i["location"].get("address"))
    pairs = set()
    for address in places:
        m = _HOUSE_AND_STREET.match(address) if isinstance(address, str) else None
        if m:
            pairs.add(rf"{m[1]}\s+(?:{_DIRECTIONAL}\s+)?{re.escape(m[2])}")
    if not pairs:
        return None
    return re.compile(r"(?<![\w-])(?:" + "|".join(sorted(pairs)) + r")(?!\w)", re.I)


# A cautious default until the owner decides otherwise: these filings are public, but no one needs to read
# them here. A group's name or description ('Domestic Violence Prevention Services') isn't a person's story.
_PERSONAL = re.compile(
    r"\bdomestic violence\b|\b(?:protection|protective|restraining) orders?\b|\babuser\b|\bstalk(?:ing|ed|er)\b"
    r"|\bsexual assault\b",
    re.I,
)
_A_GROUPS_WORK = re.compile(
    r"\b(?:prevention|services?|center|support|coalition|shelter|program|advocacy|awareness|agency|foundation"
    r"|nonprofit|non-profit)\b",
    re.I,
)
_FAMILY_SUPPORT = re.compile(r"\b(?:child support|spousal support|spousal maintenance|alimony)\b", re.I)


def _scrub(value, family: re.Pattern | None, home: re.Pattern | None):
    if isinstance(value, dict):
        return {k: v if k == "who" else _scrub(v, family, home) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub(v, family, home) for v in value]
    if not isinstance(value, str):
        return value
    if _PERSONAL.search(value) and not _A_GROUPS_WORK.search(value):
        return PERSONAL_REMOVED
    if home and home.search(value):
        return ADDRESS_REMOVED
    if not family:
        return value
    pieces = re.split(f"({_FIRMS.pattern})", value, flags=re.I)     # Odd pieces are the firms.
    return "".join(p if n % 2 else family.sub(FAMILY_REMOVED, p) for n, p in enumerate(pieces))


def sanitize(raw: dict) -> dict:
    s = raw.get("statement", raw) if isinstance(raw, dict) else {}
    person = raw.get("person") if isinstance(raw, dict) and isinstance(raw.get("person"), dict) else {}
    official = person.get("name")
    family = _name_pattern(_names(s.get("spouse")) + _names(s.get("dependents")), official)
    dependents = _name_pattern(_names(s.get("dependents")), official)
    private_location = bool(s.get("judge_or_law_enforcement"))

    # Support payments name a former spouse, who is in neither family field.
    income = [{"who": _who(i),
               "source": FAMILY_REMOVED if _FAMILY_SUPPORT.search(str(i.get("why"))) and person_shaped(i.get("name"))
               else text(i.get("name")),
               "for": text(i.get("why")),
               "amount": text(i.get("income")), "category": text(i.get("category")), **_place(i)}
              for i in _section_items(s.get("income"))]
    accounts = []
    for i in _section_items(s.get("accounts")):
        if _for_a_child(i.get("name"), dependents):
            continue
        held = [a for a in i.get("assets") or [] if isinstance(a, dict)]
        assets = [a for a in held if not _for_a_child(a.get("nature"), dependents)]
        if held and not assets:     # Everything in the account was a child's.
            continue
        accounts.append({"who": _who(i), "name": text(i.get("name")), "category": text(i.get("category")),
                         "assets": _pairs(assets, "nature", "value", "income"), **_place(i)})
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
    return _scrub({"income": income, "accounts": accounts, "businesses": businesses,
                   "real_estate": real_estate, "creditors": creditors, "gifts": gifts, "lobbying": lobbying,
                   "location_withheld": private_location}, family, _home_addresses(raw if isinstance(raw, dict) else {}, s))


def from_json(value) -> dict | None:
    """Sanitized details from the dataset's JSON text, or None for older paper filings."""
    if not value:
        return None
    try:
        return sanitize(json.loads(value))
    except (ValueError, TypeError):
        return None
