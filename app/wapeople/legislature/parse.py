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


def matching_history_line(yeas: int, nays: int, lines: list[str]) -> str | None:
    """The history line that records a vote with these counts, if any."""
    for line in lines:
        m = YEAS_NAYS_RE.search(line)
        if m and int(m[1]) == yeas and int(m[2]) == nays:
            return line
    return None


def roll_call_result(yeas: int, nays: int, lines: list[str]) -> str | None:
    """Find the history line recording this vote (same counts) and read its outcome."""
    line = matching_history_line(yeas, nays, lines)
    if line is None:
        return None
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


# What a roll call was about. The Legislature's motion text varies by year and chamber
# ("Final Passage", "FP as Amd by the House", "7th Order: 3rd Reading", or blank).
VOTE_KINDS = {
    "final_passage": "Final passage",
    "concurrence": "Resolving differences between chambers",
    "confirmation": "Confirmation",
    "adoption": "Adoption",
    "veto_override": "Veto override",
    "amendment": "Amendment",
    "procedural": "Procedural motion",
}
FINAL_VOTE_KINDS = ("final_passage", "concurrence", "confirmation", "adoption", "veto_override")

_FINAL_RE = re.compile(r"final passage|(?<![a-z])fp(?![a-z])|3rd reading|third reading", re.I)
_AMENDMENT_RE = re.compile(r"^\s*(com\s+)?\S*\d+\s+\S.*(pg\s+\d+|striker)|^amd\b|amendment number|\bamendment\b|striker", re.I)


def classify_vote(motion: str | None, history_line: str | None = None) -> str:
    m = " ".join((motion or "").split()).lower()
    if not m:
        h = (history_line or "").lower()
        if "third reading" in h or "final passage" in h:
            return "final_passage"
        if "concur" in h:
            return "concurrence"
        if "confirm" in h:
            return "confirmation"
        if "adopt" in h:
            return "adoption"
        return "procedural"
    if m.startswith(("motion to", "suspend", "demand", "refer", "postpone", "grant", "immediately")):
        return "procedural"
    if "veto" in m or "notwithstanding" in m:
        return "veto_override"
    if "confirm" in m:
        return "confirmation"
    if m.startswith(("concur", "recede")) or ("concur" in m and "motion" in m):
        return "concurrence"
    if m.startswith("adopt"):
        return "adoption"
    if _FINAL_RE.search(m):
        return "final_passage"
    if _AMENDMENT_RE.search(m):
        return "amendment"
    return "procedural"
