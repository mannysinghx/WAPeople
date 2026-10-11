"""Turn the Legislature's terse status codes into plain-language stages.

Status codes look like 'H Approps', 'S Rules X', 'C 209 L 25', 'Gov vetoed'. The leading
H/S says which chamber currently holds the bill. We combine the code with the
passed-House / passed-Senate lists and the biennium's state to decide:

  stage   a fine-grained code (see STAGES)
  label   one human sentence, e.g. "Passed House · in Senate committee: Housing"
  bucket  proposed | ongoing | past — the three top-level views of the site
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta

from .parse import session_law, session_law_text

# code: (short label, bucket while the biennium is still open)
STAGES: dict[str, tuple[str, str]] = {
    "prefiled": ("Prefiled", "proposed"),
    "introduced": ("Introduced", "proposed"),
    "committee": ("In committee", "proposed"),
    "floor": ("Awaiting floor action", "ongoing"),
    "passed_origin": ("Passed one chamber", "ongoing"),
    "failed_vote": ("Failed floor vote", "ongoing"),
    "passed_legislature": ("Passed Legislature", "ongoing"),
    "law": ("Enacted", "past"),
    "law_partial_veto": ("Enacted (partial veto)", "past"),
    "vetoed": ("Vetoed", "past"),
    "adopted": ("Adopted", "past"),
    "filed": ("Passed Legislature", "past"),
    "confirmed": ("Confirmed", "past"),
    "not_confirmed": ("Not confirmed", "past"),
    "did_not_pass": ("Did not pass", "past"),
}
FINAL_STAGES = {k for k, (_, b) in STAGES.items() if b == "past"}

CHAMBER = {"H": "House", "S": "Senate"}
OTHER = {"House": "Senate", "Senate": "House"}

_FLOOR_RE = re.compile(
    r"^(Rules\b|\d(st|nd|rd|th) Reading|\d(st|nd|rd|th) Rd|Confirm cal|Held|insists|"
    r"concurs|recedes|Concur|Passed \d|Spkr Signed|Pres Signed|President Signed)",
    re.I,
)
_REFERRED_RE = re.compile(r"referred to ([^.;]+?)(?:\s*\(|\.|;|$)", re.I)


@dataclass
class StageResult:
    stage: str
    label: str
    bucket: str


def _norm(s: str | None) -> str:
    return " ".join((s or "").split())


def _committee_name(actions: list[str]) -> str | None:
    for line in reversed(actions):
        m = _REFERRED_RE.search(line)
        if m:
            name = m[1].strip()
            if not name.lower().startswith("rules"):
                return name
    return None


def _location(chamber: str, rest: str, actions: list[str]) -> tuple[str, str]:
    """(stage, where-it-is text) for a bill that is still moving."""
    if rest.lower().startswith("rules"):
        where = f"in {chamber} Rules Committee"
        if rest.rstrip().endswith("X"):
            where += " (X-file: no further action expected)"
        return "floor", where
    if re.match(r"^\d(st|nd|rd|th) (Reading|Rd)", rest):
        return "floor", f"on the {chamber} floor calendar ({rest})"
    if rest.lower().startswith("confirm cal"):
        return "floor", f"on the {chamber} confirmation calendar"
    if rest.lower() in ("held",):
        return "floor", f"held on the {chamber} floor"
    if rest.lower() in ("insists", "concurs", "recedes", "concur"):
        return "floor", "chambers resolving differences"
    if _FLOOR_RE.match(rest):
        return "floor", f"on the {chamber} floor"
    if rest.lower() == "introduced":
        return "introduced", f"introduced in the {chamber}"
    committee = _committee_name(actions) or rest
    return "committee", f"in {chamber} committee: {committee}"


def compute_stage(
    *,
    type_code: str,
    original_agency: str | None,
    status_code: str | None,
    veto: bool,
    partial_veto: bool,
    passed_house: bool,
    passed_senate: bool,
    passed_legislature: bool,
    introduced_date: date | None,
    action_lines: list[str],
    biennium_status: str,
) -> StageResult:
    s = _norm(status_code)
    chamber_letter, _, rest = s.partition(" ")
    chamber = CHAMBER.get(chamber_letter)
    if chamber is None:
        rest = s  # codes without a chamber, e.g. 'Gov vetoed', 'Del to Gov'
    origin = original_agency if original_agency in ("House", "Senate") else None

    law = session_law(s)
    stage: str
    label: str

    if law or s == "Veto override":
        if law:
            text = session_law_text(*law)
        else:
            text = "veto overridden"
        if partial_veto:
            stage, label = "law_partial_veto", f"Enacted with partial veto · {text}"
        else:
            stage, label = "law", f"Enacted · {text}"
    elif s in ("Gov vetoed", "Veto sustain") or (veto and not partial_veto):
        stage, label = "vetoed", "Vetoed by the Governor"
    elif rest == "Filed Sec/St":
        if type_code == "JR":
            label = "Passed Legislature · constitutional amendment sent to voters"
        else:
            label = "Passed Legislature · filed with Secretary of State"
        stage = "filed"
    elif rest.startswith("Adopted"):
        stage, label = "adopted", f"Adopted by the {chamber or 'Legislature'}"
    elif rest == "Confirmed":
        stage, label = "confirmed", "Confirmed by the Senate"
    elif rest in ("Term expired", "Resigned", "Deceased") or s == "Ret to Gov":
        reason = {"Ret to Gov": "returned to Governor"}.get(s, rest.lower())
        stage, label = "not_confirmed", f"Not confirmed ({reason})"
    elif passed_legislature or s in ("Del to Gov",) or rest in (
        "Spkr Signed", "Pres Signed", "President Signed"
    ):
        stage, label = "passed_legislature", "Passed Legislature · awaiting Governor"
    elif rest.startswith("Failed"):
        stage, label = "failed_vote", f"Failed a {chamber or 'floor'} floor vote"
    else:
        passed_origin = (origin == "House" and passed_house) or (
            origin == "Senate" and passed_senate
        )
        if not s:
            stage = "introduced" if introduced_date else "prefiled"
            where = "introduced" if introduced_date else "prefiled for introduction"
        elif chamber is None:
            stage, where = "floor", s
        else:
            stage, where = _location(chamber, rest, action_lines)
        if passed_origin and origin:
            stage = "passed_origin"
            label = f"Passed {origin} · now {where}"
        else:
            label = where[0].upper() + where[1:]

    if stage not in FINAL_STAGES and biennium_status in ("closed", "adjourned_final"):
        passed_origin = (origin == "House" and passed_house) or (
            origin == "Senate" and passed_senate
        )
        if stage == "passed_legislature":
            label = "Passed Legislature · no Governor action recorded"
            return StageResult("passed_legislature", label, "past")
        if passed_origin and origin:
            label = f"Did not pass · passed {origin}, stalled in {OTHER[origin]}"
        elif stage == "committee":
            label = "Did not pass · died in committee"
        elif stage in ("floor", "failed_vote"):
            label = f"Did not pass · stalled on the {chamber or 'chamber'} floor"
        else:
            label = "Did not pass"
        return StageResult("did_not_pass", label, "past")

    bucket = STAGES[stage][1]
    if biennium_status == "interim" and stage not in FINAL_STAGES:
        label += " · carries over to the next session"
    return StageResult(stage, label, bucket)


# ---- Biennium state --------------------------------------------------------------

_ACTIVITY_RE = re.compile(
    r"(first reading|second reading|third reading|executive action|public hearing|"
    r"passed|adopted|referred to|placed on|rules suspended|concurred|confirmed)",
    re.I,
)


def is_activity_line(line: str) -> bool:
    """True for history lines that mean the Legislature itself was meeting."""
    low = line.lower()
    if low.startswith(("by resolution", "effective date", "governor", "chapter")):
        return False
    return bool(_ACTIVITY_RE.search(line))


def biennium_status(
    start_year: int, last_activity: date | None, today: date | None = None
) -> str:
    """upcoming | in_session | interim | adjourned_final | closed."""
    today = today or date.today()
    end_year = start_year + 1
    if today.year > end_year:
        return "closed"
    if today.year < start_year:
        return "upcoming"
    if last_activity and today - last_activity <= timedelta(days=10):
        return "in_session"
    if today.year == end_year and last_activity and last_activity.year == end_year:
        return "adjourned_final"
    return "interim"


def next_session_start(today: date | None = None) -> date:
    """Regular sessions convene the second Monday of January (RCW 44.04.010)."""
    today = today or date.today()

    def second_monday(year: int) -> date:
        d = date(year, 1, 1)
        first_monday = d + timedelta(days=(7 - d.weekday()) % 7)
        return first_monday + timedelta(days=7)

    this_year = second_monday(today.year)
    return this_year if today <= this_year else second_monday(today.year + 1)
