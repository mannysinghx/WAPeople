"""Pure parsing helpers for Legislature web-service records (no I/O; unit tested)."""
from __future__ import annotations

import re
from datetime import date

# Version modifiers (E = engrossed, S = substitute, digits = 2nd/3rd...) + base prefix.
_BILL_ID_RE = re.compile(
    r"^(?P<mods>(?:\d?E)?(?:\d?S)?)"
    r"(?P<base>HB|SB|HJM|SJM|HJR|SJR|HCR|SCR|HR|SR|SGA|HI|SI)\s+(?P<num>\d+)$"
)

TYPE_BY_PREFIX = {
    "HB": "B", "SB": "B",
    "HJM": "JM", "SJM": "JM",
    "HJR": "JR", "SJR": "JR",
    "HCR": "CR", "SCR": "CR",
    "HR": "R", "SR": "R",
    "SGA": "GA",
    "HI": "I", "SI": "I",
}

TYPE_NAMES = {
    "B": "Bill",
    "JR": "Joint Resolution",
    "JM": "Joint Memorial",
    "CR": "Concurrent Resolution",
    "R": "Resolution",
    "GA": "Gubernatorial Appointment",
    "I": "Initiative",
}

SESSION_LAW_STATUS_RE = re.compile(r"^C\s+(\d+)\s+L\s+(\d{2})(?:\s+E(\d))?")
YEAS_NAYS_RE = re.compile(r"yeas,?\s*(\d+);\s*nays,?\s*(\d+)", re.I)


def split_bill_id(bill_id: str) -> tuple[str, int] | None:
    """'E2SHB 1234' -> ('HB', 1234). Returns None for unrecognized ids."""
    m = _BILL_ID_RE.match(" ".join((bill_id or "").split()))
    if not m:
        return None
    return m["base"], int(m["num"])


def base_bill_id(bill_id: str) -> str | None:
    parts = split_bill_id(bill_id)
    return f"{parts[0]} {parts[1]}" if parts else None


def parse_date(s: str | None) -> date | None:
    if not s or s.startswith("0001-"):
        return None
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        return None


def is_true(s) -> bool:
    return str(s).strip().lower() == "true"


def two_digit_year(yy: int) -> int:
    return 1900 + yy if yy >= 80 else 2000 + yy


def session_law(status: str | None) -> tuple[int, int, int | None] | None:
    """'C 209 L 25' -> (2025, 209, None); 'C 4 L 21 E1' -> (2021, 4, 1)."""
    m = SESSION_LAW_STATUS_RE.match((status or "").strip())
    if not m:
        return None
    return two_digit_year(int(m[2])), int(m[1]), int(m[3]) if m[3] else None


def session_law_text(year: int, chapter: int, special: int | None) -> str:
    ordinal = {1: "1st", 2: "2nd", 3: "3rd"}
    sp = f", {ordinal.get(special, f'{special}th')} Special Session" if special else ""
    return f"Chapter {chapter}, Laws of {year}{sp}"


def biennium_for_year(year: int) -> str:
    start = year if year % 2 == 1 else year - 1
    return f"{start}-{str(start + 1)[-2:]}"


def biennium_years(biennium: str) -> tuple[int, int]:
    start = int(biennium[:4])
    return start, start + 1


def roll_call_result(yeas: int, nays: int, lines: list[str]) -> str | None:
    """Find the history line recording this vote (same counts) and read its outcome."""
    for line in lines:
        m = YEAS_NAYS_RE.search(line)
        if not m or int(m[1]) != yeas or int(m[2]) != nays:
            continue
        low = line.lower()
        for word, result in (
            ("failed", "failed"),
            ("not adopted", "failed"),
            ("passed", "passed"),
            ("adopted", "adopted"),
            ("confirmed", "confirmed"),
            ("concurred", "concurred"),
            ("override", "override"),
            ("sustain", "failed"),
        ):
            if word in low:
                return result
        return "recorded"
    return None
