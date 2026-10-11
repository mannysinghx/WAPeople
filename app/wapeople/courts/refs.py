"""Find references to legislation inside court-opinion text (pure functions; unit tested).

Three kinds of mention are extracted:
  session_law  "Laws of 2021, ch. 196"            -> exact link via the bill's chapter number
  bill         "Engrossed Substitute Senate Bill 5096", "ESSB 5096"
               -> linked only when a year is stated next to it (bill numbers repeat every biennium)
  initiative   "Initiative 976", "I-1631"
"""
from __future__ import annotations

import re

_ORD = r"(?:Second|Third|Fourth|2d|2nd|3d|3rd|4th)"
LONG_BILL_RE = re.compile(
    rf"\b(?P<text>(?:(?:{_ORD}\s+)?Engrossed\s+)?(?:(?:{_ORD}\s+)?Substitute\s+)?"
    r"(?P<chamber>House|Senate)\s+(?P<kind>Bill|Joint\s+Resolution|Joint\s+Memorial|Concurrent\s+Resolution))"
    r"\s*(?:\((?P<abbr>[0-9A-Z]{2,9})\)\s*)?(?:No\.?\s*)?(?P<num>\d{4})\b",
    re.I,
)
SHORT_BILL_RE = re.compile(
    r"\b(?P<prefix>\d?E?\d?S?(?P<base>HB|SB|HJR|SJR|HJM|SJM|HCR|SCR))\s?(?P<num>\d{4})\b"
)
SESSION_LAW_RE = re.compile(
    r"Laws\s+of\s+(?P<year>(?:19|20)\d{2}),?\s*"
    r"(?:(?P<sp>\d)(?:st|nd|rd|d|th)\s+(?:Spec(?:ial)?\.?|Sp\.|Ex(?:traordinary|\.)?)\s*Sess(?:ion|\.)?,?\s*)?"
    r"ch(?:apter|\.)?\s*(?P<ch>\d+)",
    re.I,
)
INITIATIVE_RE = re.compile(
    r"\b(?:Initiative\s+(?:Measure\s+)?(?:No\.\s*)?|I-)(?P<num>\d{2,4})\b"
)
YEAR_RE = re.compile(r"\b(19[89]\d|20[0-4]\d)\b")

KIND_TO_TYPE = {
    "bill": "B",
    "joint resolution": "JR",
    "joint memorial": "JM",
    "concurrent resolution": "CR",
}
BASE_TO_TYPE = {"HB": "B", "SB": "B", "HJR": "JR", "SJR": "JR", "HJM": "JM", "SJM": "JM", "HCR": "CR", "SCR": "CR"}


def _plausible_number(chamber: str, number: int) -> bool:
    # House measures are numbered 1000-4999, Senate measures 5000-8999.
    return (1000 <= number < 5000) if chamber == "H" else (5000 <= number < 9000)


def _nearby_year(text: str, start: int, end: int) -> int | None:
    """A year stated right after the mention (preferred) or just before it."""
    after = text[end:end + 160]
    m = re.search(r"Laws\s+of\s+((?:19|20)\d{2})", after) or YEAR_RE.search(after)
    if m:
        return int(m.group(1))
    before = text[max(0, start - 60):start]
    years = YEAR_RE.findall(before)
    return int(years[-1]) if years else None


def _clean(s: str) -> str:
    return " ".join(s.split())


def extract_mentions(text: str) -> list[dict]:
    """All distinct legislation mentions in text, in order of first appearance."""
    if not text:
        return []
    text = re.sub(r"</?mark>", "", text)
    found: list[tuple[int, dict]] = []
    seen: set[tuple] = set()

    def add(pos: int, item: dict, key: tuple) -> None:
        if key not in seen:
            seen.add(key)
            found.append((pos, item))

    for m in SESSION_LAW_RE.finditer(text):
        year, ch, sp = int(m["year"]), int(m["ch"]), int(m["sp"]) if m["sp"] else None
        add(m.start(), {"kind": "session_law", "text": _clean(m.group(0)), "year": year,
                        "chapter": ch, "special": sp}, ("law", year, ch, sp))

    for m in LONG_BILL_RE.finditer(text):
        chamber = "H" if m["chamber"].lower() == "house" else "S"
        number = int(m["num"])
        if not _plausible_number(chamber, number):
            continue
        type_code = KIND_TO_TYPE[_clean(m["kind"]).lower()]
        year = _nearby_year(text, m.start(), m.end())
        label = f"{_clean(m['text'])} {number}"
        add(m.start(), {"kind": "bill", "text": label, "chamber": chamber, "type_code": type_code,
                        "number": number, "year": year}, ("bill", chamber, type_code, number, year))

    for m in SHORT_BILL_RE.finditer(text):
        base = m["base"]
        chamber = base[0]
        number = int(m["num"])
        if not _plausible_number(chamber, number):
            continue
        year = _nearby_year(text, m.start(), m.end())
        add(m.start(), {"kind": "bill", "text": f"{m['prefix']} {number}", "chamber": chamber,
                        "type_code": BASE_TO_TYPE[base], "number": number, "year": year},
            ("bill", chamber, BASE_TO_TYPE[base], number, year))

    for m in INITIATIVE_RE.finditer(text):
        num = int(m["num"])
        add(m.start(), {"kind": "initiative", "text": f"Initiative {num}", "number": num}, ("init", num))

    found.sort(key=lambda t: t[0])
    # Collapse a bill mentioned once with a year and once without: keep the dated one.
    dated = {(i["chamber"], i["type_code"], i["number"]) for _, i in found if i["kind"] == "bill" and i["year"]}
    out = []
    for _, item in found:
        if item["kind"] == "bill" and item["year"] is None and (item["chamber"], item["type_code"], item["number"]) in dated:
            continue
        out.append(item)
    return out
