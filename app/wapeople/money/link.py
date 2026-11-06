"""Match legislators to their Public Disclosure Commission records.

Attaching someone's donors to the wrong person would be a serious error, so this is strict by
design. A legislator links to a PDC person only when all of these agree:
  * every last-name word appears in the PDC filer name,
  * the first name matches (exactly, as a known nickname, or by a shared 3-letter start),
  * the PDC person ran for the same chamber in the same district within the years of a term.
Anything else stays unlinked unless it's listed by hand in data/money_officials.json with a note.
Two candidates for one legislator also stay unlinked: we never pick between them.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

NICKNAMES = {
    "ANDY": "ANDREW", "BILL": "WILLIAM", "BOB": "ROBERT", "BOBBY": "ROBERT", "CHUCK": "CHARLES",
    "DICK": "RICHARD", "ED": "EDWARD", "JIM": "JAMES", "JIMMY": "JAMES", "JOE": "JOSEPH",
    "KATE": "KATHERINE", "KATIE": "KATHERINE", "LARRY": "LAWRENCE", "LIZ": "ELIZABETH",
    "MAGGIE": "MARGARET", "MIKE": "MICHAEL", "PAT": "PATRICIA", "PEGGY": "MARGARET", "RICK": "RICHARD",
    "SANDY": "SANDRA", "SUE": "SUSAN", "TED": "THEODORE", "TOM": "THOMAS", "TONY": "ANTHONY",
    "VICKI": "VICTORIA", "WES": "WESLEY", "ZACK": "ZACHARY",
}
CHAMBER_BY_OFFICE = {"STATE REPRESENTATIVE": "House", "STATE SENATOR": "Senate"}
_NOT_FAMILY_NAMES = {"JR", "SR", "II", "III", "IV", "VON", "VAN", "DE", "DEL", "LA", "LE"}


def name_tokens(name: str | None) -> list[str]:
    s = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().upper()
    return re.sub(r"[^A-Z ]+", " ", s.replace("'", "").replace("-", " ")).split()


def surname_tokens(name: str | None, last_name: str | None = None) -> frozenset[str]:
    """The words of someone's family name. First names stay out: 'Friends of Jane Roe' is not
    Jane Doe's campaign."""
    words = name_tokens(last_name) or name_tokens(name)[1:]
    return frozenset(w for w in words if len(w) > 1 and w not in _NOT_FAMILY_NAMES)


def first_name_matches(first: list[str], tokens: set[str]) -> bool:
    for f in first:
        if f in tokens or NICKNAMES.get(f) in tokens:
            return True
        if len(f) >= 3 and any(len(t) >= 3 and t[:3] == f[:3] for t in tokens):
            return True
    return False


@dataclass
class Link:
    person_ids: list[int] = field(default_factory=list)
    method: str | None = None
    note: str | None = None


def group_by_person(candidate_rows: list[dict]) -> dict[int, dict]:
    people: dict[int, dict] = {}
    for r in candidate_rows:
        pid = r.get("person_id")
        if pid in (None, ""):
            continue
        p = people.setdefault(int(pid), {"tokens": set(), "rows": []})
        p["tokens"] |= set(name_tokens(r.get("filer_name")))
        p["rows"].append(r)
    return people


def seat_matches(person_rows: list[dict], terms: list[dict]) -> int:
    """How many of a PDC person's campaigns were for a seat this legislator held around then."""
    hits = 0
    for r in person_rows:
        chamber = CHAMBER_BY_OFFICE.get(r.get("office"))
        district = (r.get("legislative_district") or "").lstrip("0")
        year = r.get("election_year")
        if not (chamber and district and year):
            continue
        y = int(year)
        for t in terms:
            start = int(t["biennium"][:4])
            # Elected the November before the term starts; Senate terms run four years.
            if t.get("agency") == chamber and t.get("district") == district and start - 5 <= y <= start + 2:
                hits += 1
                break
    return hits


def link_member(member: dict, terms: list[dict], people: dict[int, dict],
                manual: dict | None = None) -> Link:
    if manual:
        return Link(list(manual["person_ids"]), "manual", manual.get("note"))
    last = set(name_tokens(member.get("last_name")))
    first = name_tokens(member.get("first_name"))
    if not last or not first or not terms:
        return Link(note="No name or term to match on.")
    found = [pid for pid, p in people.items()
             if last <= p["tokens"] and first_name_matches(first, p["tokens"])
             and seat_matches(p["rows"], terms) > 0]
    if len(found) == 1:
        return Link(found, "name, chamber and district")
    if len(found) > 1:
        return Link(note=f"Ambiguous: {len(found)} PDC people match this name and seat, so none was linked.")
    return Link(note="No matching campaign found in the PDC's electronic records by name, chamber and district.")
