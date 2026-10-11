"""Load Washington legislation, sponsors, history and roll-call votes into Postgres."""
from __future__ import annotations

import logging
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from typing import Iterable

import psycopg

from ..config import FIRST_BIENNIUM_START
from .client import LegClient, as_list
from .parse import (
    TYPE_BY_PREFIX,
    TYPE_NAMES,
    YEAS_NAYS_RE,
    biennium_years,
    is_true,
    parse_date,
    roll_call_result,
    session_law,
    split_bill_id,
)
from .stages import biennium_status, compute_stage, is_activity_line

log = logging.getLogger(__name__)

Key = tuple[int, str]  # (bill_number, type_code)


# ---- Biennium helpers ------------------------------------------------------------

def all_bienniums(today: date | None = None) -> list[str]:
    today = today or date.today()
    last_start = today.year if today.year % 2 == 1 else today.year - 1
    return [f"{y}-{str(y + 1)[-2:]}" for y in range(FIRST_BIENNIUM_START, last_start + 1, 2)]


def current_biennium(today: date | None = None) -> str:
    return all_bienniums(today)[-1]


def window(biennium: str) -> tuple[str, str]:
    """Date range covering a biennium, including December prefiling."""
    start, end = biennium_years(biennium)
    return f"{start - 1}-11-01", f"{end}-12-31"


# ---- Record shaping ---------------------------------------------------------------

def compact_version(rec: dict) -> dict | None:
    """Keep only the fields we use from a <Legislation> record (one bill version)."""
    parts = split_bill_id(rec.get("BillId", ""))
    if not parts or not rec.get("Biennium"):
        return None
    prefix, number = parts
    slt = rec.get("ShortLegislationType")
    api_type = slt.get("ShortLegislationType") if isinstance(slt, dict) else None
    type_code = api_type if api_type in TYPE_NAMES else TYPE_BY_PREFIX[prefix]
    cs = rec.get("CurrentStatus")
    cs = cs if isinstance(cs, dict) else {}
    comps = rec.get("Companions")
    companions = [
        c.get("BillId")
        for c in as_list(comps.get("Companion") if isinstance(comps, dict) else None)
        if isinstance(c, dict) and c.get("BillId")
    ]
    requested_by = [
        label
        for field, label in (
            ("RequestedByGovernor", "Governor"),
            ("RequestedByBudgetCommittee", "Budget committee"),
            ("RequestedByDepartment", "State agency"),
            ("RequestedByOther", "Other"),
        )
        if is_true(rec.get(field))
    ]
    prime = rec.get("PrimeSponsorID") or ""
    return {
        "biennium": rec["Biennium"],
        "number": number,
        "type_code": type_code,
        "prefix": prefix,
        "version_id": " ".join(rec["BillId"].split()),
        "sub": int(rec.get("SubstituteVersion") or 0),
        "eng": int(rec.get("EngrossedVersion") or 0),
        "agency": rec.get("OriginalAgency") or None,
        "active": is_true(rec.get("Active")),
        "short": rec.get("ShortDescription") or None,
        "long": rec.get("LongDescription") or None,
        "title": rec.get("LegalTitle") or None,
        "introduced": parse_date(rec.get("IntroducedDate")),
        "sponsor": rec.get("Sponsor") or None,
        "prime": int(prime) if prime.isdigit() and int(prime) > 0 else None,
        "request": rec.get("Request") or None,
        "requested_by": requested_by,
        "approp": is_true(rec.get("Appropriations")),
        "fiscal": is_true(rec.get("StateFiscalNote")) or is_true(rec.get("LocalFiscalNote")),
        "status": " ".join((cs.get("Status") or "").split()) or None,
        "status_line": cs.get("HistoryLine") or None,
        "status_date": parse_date(cs.get("ActionDate")),
        "veto": is_true(cs.get("Veto")),
        "pveto": is_true(cs.get("PartialVeto")),
        "companions": companions,
    }


_SUPERSEDED = ("subst for", "not subst")


def version_rank(v: dict) -> tuple:
    superseded = any((v["status"] or "").endswith(s) for s in _SUPERSEDED)
    return (
        not superseded,
        v["active"],
        v["status_date"] or date.min,
        v["sub"] + v["eng"],
        v["sub"],
    )


def pick_current(versions: list[dict]) -> dict:
    """The version that carries the bill's live status."""
    return max(versions, key=version_rank)


def group_versions(versions: list[dict]) -> dict[Key, list[dict]]:
    """Group version records by bill. The service sometimes returns the same version id
    more than once with a stale status; keep the best-ranked copy of each."""
    best: dict[Key, dict[str, dict]] = defaultdict(dict)
    for v in versions:
        k = (v["number"], v["type_code"])
        prev = best[k].get(v["version_id"])
        if prev is None or version_rank(v) > version_rank(prev):
            best[k][v["version_id"]] = v
    return {k: list(vs.values()) for k, vs in best.items()}


def first_value(versions: list[dict], field: str):
    for v in versions:
        if v.get(field):
            return v[field]
    return None


def build_bill(
    biennium: str,
    key: Key,
    versions: list[dict],
    actions: list[tuple[date | None, str, str]],
    passed: dict[str, set[Key]],
    bstatus: str,
) -> dict:
    number, type_code = key
    cur = pick_current(versions)
    # Most recent version first so descriptive fields come from the latest text.
    ordered = [cur] + sorted(
        (v for v in versions if v is not cur),
        key=lambda v: (v["status_date"] or date.min, v["sub"] + v["eng"]),
        reverse=True,
    )
    lines = [a[2] for a in actions]
    introduced = min((v["introduced"] for v in versions if v["introduced"]), default=None)
    passed_house = key in passed["house"]
    passed_senate = key in passed["senate"]
    passed_leg = key in passed["legislature"]
    st = compute_stage(
        type_code=type_code,
        original_agency=cur["agency"] or first_value(ordered, "agency"),
        status_code=cur["status"],
        veto=cur["veto"],
        partial_veto=cur["pveto"],
        passed_house=passed_house,
        passed_senate=passed_senate,
        passed_legislature=passed_leg,
        introduced_date=introduced,
        action_lines=lines,
        biennium_status=bstatus,
    )
    law = session_law(cur["status"])
    action_dates = [a[0] for a in actions if a[0]]
    last_action = max(action_dates + ([cur["status_date"]] if cur["status_date"] else []), default=None)
    version_ids = sorted({v["version_id"] for v in versions}, key=lambda s: (len(s.split()[0]), s))
    companions = sorted({c for v in versions for c in v["companions"]})
    return {
        "biennium": biennium,
        "bill_number": number,
        "type_code": type_code,
        "type_name": TYPE_NAMES.get(type_code, type_code),
        "bill_id": f"{cur['prefix']} {number}",
        "current_version": cur["version_id"],
        "versions": version_ids,
        "original_agency": cur["agency"] or first_value(ordered, "agency"),
        "short_description": first_value(ordered, "short"),
        "long_description": first_value(ordered, "long"),
        "legal_title": first_value(ordered, "title"),
        "introduced_date": introduced,
        "prime_sponsor_id": first_value(ordered, "prime"),
        "sponsor_text": first_value(ordered, "sponsor"),
        "request_number": first_value(ordered, "request"),
        "requested_by": sorted({r for v in versions for r in v["requested_by"]}),
        "appropriations": any(v["approp"] for v in versions),
        "fiscal_note": any(v["fiscal"] for v in versions),
        "companions": companions,
        "status_code": cur["status"],
        "status_line": cur["status_line"],
        "status_date": cur["status_date"],
        "vetoed": cur["veto"],
        "partial_veto": cur["pveto"],
        "passed_house": passed_house,
        "passed_senate": passed_senate,
        "passed_legislature": passed_leg,
        "session_law_year": law[0] if law else None,
        "session_law_chapter": law[1] if law else None,
        "session_law_special": law[2] if law else None,
        "stage": st.stage,
        "stage_label": st.label,
        "bucket": st.bucket,
        "last_action_date": last_action,
    }


BILL_COLUMNS = [
    "biennium", "bill_number", "type_code", "type_name", "bill_id", "current_version",
    "versions", "original_agency", "short_description", "long_description", "legal_title",
    "introduced_date", "prime_sponsor_id", "sponsor_text", "request_number", "requested_by",
    "appropriations", "fiscal_note", "companions", "status_code", "status_line", "status_date",
    "vetoed", "partial_veto", "passed_house", "passed_senate", "passed_legislature",
    "session_law_year", "session_law_chapter", "session_law_special", "stage", "stage_label",
    "bucket", "last_action_date",
]
_KEY_COLS = {"biennium", "bill_number", "type_code"}
UPSERT_BILL_SQL = (
    f"INSERT INTO bills ({', '.join(BILL_COLUMNS)}) VALUES ({', '.join(['%s'] * len(BILL_COLUMNS))})"
    " ON CONFLICT (biennium, bill_number, type_code) DO UPDATE SET "
    + ", ".join(f"{c} = EXCLUDED.{c}" for c in BILL_COLUMNS if c not in _KEY_COLS)
    + ", updated_at = now()"
)


def parse_roll_call(rc: dict) -> dict:
    def count(field: str) -> int:
        block = rc.get(field)
        try:
            return int(block.get("Count")) if isinstance(block, dict) else 0
        except (TypeError, ValueError):
            return 0

    votes_block = rc.get("Votes")
    votes = []
    for v in as_list(votes_block.get("Vote") if isinstance(votes_block, dict) else None):
        mid = (v.get("MemberId") or "").strip()
        vote = v.get("VOte") or v.get("Vote") or ""
        if mid.isdigit() and vote:
            votes.append((int(mid), v.get("Name") or None, vote))
    seq = (rc.get("SequenceNumber") or "").strip()
    return {
        "agency": rc.get("Agency") or "",
        "version_id": " ".join((rc.get("BillId") or "").split()) or None,
        "motion": rc.get("Motion") or None,
        "sequence_number": int(seq) if seq.isdigit() else None,
        "vote_date": parse_date(rc.get("VoteDate")),
        "yeas": count("YeaVotes"),
        "nays": count("NayVotes"),
        "absent": count("AbsentVotes"),
        "excused": count("ExcusedVotes"),
        "votes": votes,
    }


# ---- The sync job -----------------------------------------------------------------

class LegislatureSync:
    def __init__(self, conn: psycopg.Connection, client: LegClient | None = None, workers: int = 4):
        self.conn = conn
        self.client = client or LegClient()
        self.workers = workers

    # Bulk feed ----------------------------------------------------------------------

    def load_bulk(self, since: str) -> dict[str, list[dict]]:
        """All bill versions introduced since a date, grouped by biennium (one big request)."""
        out: dict[str, list[dict]] = defaultdict(list)
        n = 0
        for rec in self.client.legislation_introduced_since(since):
            v = compact_version(rec)
            if v:
                out[v["biennium"]].append(v)
                n += 1
        log.info("bulk feed since %s: %d versions across %d bienniums", since, n, len(out))
        return out

    # Per biennium -------------------------------------------------------------------

    def sync_biennium(
        self,
        biennium: str,
        versions: list[dict] | None = None,
        details: bool = True,
        force_details: bool = False,
        today: date | None = None,
    ) -> dict:
        today = today or date.today()
        start, end = biennium_years(biennium)
        begin, finish = window(biennium)
        stats: dict = {"biennium": biennium}
        t0 = time.time()
        self._ensure_biennium(biennium)

        members = self.client.members(biennium) if start <= today.year else []
        self._upsert_members(biennium, members)
        stats["members"] = len(members)

        # Complete list of numbers (the bulk feed omits items with no introduced date,
        # e.g. gubernatorial appointments and some Senate resolutions).
        listed: set[Key] = set()
        for y in (start, end):
            if y <= today.year:
                for li in self.client.legislation_by_year(y):
                    if li.get("Biennium") != biennium:
                        continue
                    parts = split_bill_id(li.get("BillId", ""))
                    if parts:
                        listed.add((parts[1], TYPE_BY_PREFIX[parts[0]]))

        if versions is None:
            versions = [
                v for v in (compact_version(r) for r in self.client.legislation_introduced_since(begin))
                if v and v["biennium"] == biennium
            ]
        else:
            versions = list(versions)
        if start >= today.year - 1:
            versions += [
                v for v in (compact_version(r) for r in self.client.prefiled())
                if v and v["biennium"] == biennium
            ]
        have = {(v["number"], v["type_code"]) for v in versions}
        missing = sorted(listed - have)
        stats["fetched_individually"] = len(missing)
        if missing:
            versions += self._fetch_missing(biennium, missing)

        groups = group_versions(versions)

        actions: dict[Key, list[tuple[date | None, str, str]]] = defaultdict(list)
        n_actions = 0
        for i, rec in enumerate(self.client.status_changes(biennium, begin, finish)):
            parts = split_bill_id(rec.get("BillId", ""))
            line = (rec.get("HistoryLine") or "").strip()
            if not parts or not line:
                continue
            k = (parts[1], TYPE_BY_PREFIX[parts[0]])
            actions[k].append((parse_date(rec.get("ActionDate")), " ".join(rec["BillId"].split()), line))
            n_actions += 1
        for k in actions:
            actions[k].sort(key=lambda a: a[0] or date.min)  # stable: keeps feed order within a day
        stats["actions"] = n_actions

        # Chamber passage comes from recorded final-passage votes (see passage_flags);
        # the service's GetLegislationPassedHouse/Senate return the same list as
        # PassedLegislature, so only that one is used.
        passed = self.passage_flags(biennium)
        passed["legislature"] = set()
        if start <= today.year:
            for li in self.client.passed("GetLegislationPassedLegislature", biennium):
                parts = split_bill_id(li.get("BillId", ""))
                if parts:
                    passed["legislature"].add((parts[1], TYPE_BY_PREFIX[parts[0]]))

        last_activity = max(
            (d for acts in actions.values() for d, _, line in acts if d and d <= today and is_activity_line(line)),
            default=None,
        )
        bstatus = biennium_status(start, last_activity, today)

        bills = [build_bill(biennium, k, vs, actions.get(k, []), passed, bstatus) for k, vs in groups.items()]
        stats["bills"] = len(bills)
        self._write_bills(biennium, bills, actions)
        self.conn.execute(
            "UPDATE bienniums SET status = %s, last_activity_date = %s, bills_synced_at = now()"
            " WHERE id = %s",
            (bstatus, last_activity, biennium),
        )
        self.conn.commit()
        stats["biennium_status"] = bstatus
        log.info("%s: %d bills, %d actions, status=%s (%.0fs)", biennium, len(bills), n_actions, bstatus, time.time() - t0)

        if details:
            stats.update(self.sync_details(biennium, force=force_details))
        stats["stages_changed"] = self.recompute_stages(biennium, bstatus, passed["legislature"])
        return stats

    def passage_flags(self, biennium: str) -> dict[str, set[Key]]:
        """Bills with a successful final-passage roll call in each chamber.

        Washington's constitution (Art. II, sec. 22) requires a recorded yea/nay vote on
        final passage, so every bill that passed a chamber has one. When the outcome
        couldn't be read from the history line, a constitutional majority of the
        chamber (50 of 98 House, 25 of 49 Senate) counts as passing.
        """
        rows = self.conn.execute(
            "SELECT b.bill_number, b.type_code,"
            " bool_or(rc.agency = 'House' AND rc.motion ILIKE '%%final passage%%'"
            "   AND (rc.result = 'passed' OR (rc.result IS NULL AND rc.yeas >= 50))) AS house,"
            " bool_or(rc.agency = 'Senate' AND rc.motion ILIKE '%%final passage%%'"
            "   AND (rc.result = 'passed' OR (rc.result IS NULL AND rc.yeas >= 25))) AS senate"
            " FROM roll_calls rc JOIN bills b ON b.id = rc.bill_pk"
            " WHERE b.biennium = %s GROUP BY 1, 2",
            (biennium,),
        ).fetchall()
        return {
            "house": {(r["bill_number"], r["type_code"]) for r in rows if r["house"]},
            "senate": {(r["bill_number"], r["type_code"]) for r in rows if r["senate"]},
        }

    def recompute_stages(self, biennium: str, bstatus: str, passed_legislature: set[Key]) -> int:
        """Re-derive stage/label/bucket once votes are loaded; returns rows changed."""
        passed = self.passage_flags(biennium)
        lines: dict[int, list[str]] = defaultdict(list)
        for r in self.conn.execute(
            "SELECT a.bill_pk, a.description FROM bill_actions a JOIN bills b ON b.id = a.bill_pk"
            " WHERE b.biennium = %s ORDER BY a.bill_pk, a.seq",
            (biennium,),
        ):
            lines[r["bill_pk"]].append(r["description"])
        updates = []
        for b in self.conn.execute(
            "SELECT id, bill_number, type_code, original_agency, status_code, vetoed, partial_veto,"
            " introduced_date, stage, stage_label, bucket, passed_house, passed_senate, passed_legislature"
            " FROM bills WHERE biennium = %s", (biennium,)).fetchall():
            k = (b["bill_number"], b["type_code"])
            ph, ps, pl = k in passed["house"], k in passed["senate"], k in passed_legislature
            st = compute_stage(
                type_code=b["type_code"], original_agency=b["original_agency"],
                status_code=b["status_code"], veto=b["vetoed"], partial_veto=b["partial_veto"],
                passed_house=ph, passed_senate=ps, passed_legislature=pl,
                introduced_date=b["introduced_date"], action_lines=lines.get(b["id"], []),
                biennium_status=bstatus,
            )
            if (st.stage, st.label, st.bucket, ph, ps, pl) != (
                b["stage"], b["stage_label"], b["bucket"], b["passed_house"], b["passed_senate"],
                b["passed_legislature"],
            ):
                updates.append((st.stage, st.label, st.bucket, ph, ps, pl, b["id"]))
        if updates:
            with self.conn.cursor() as cur:
                cur.executemany(
                    "UPDATE bills SET stage = %s, stage_label = %s, bucket = %s, passed_house = %s,"
                    " passed_senate = %s, passed_legislature = %s, updated_at = now() WHERE id = %s",
                    updates,
                )
        self.conn.commit()
        return len(updates)

    def _ensure_biennium(self, biennium: str) -> None:
        start, end = biennium_years(biennium)
        self.conn.execute(
            "INSERT INTO bienniums (id, start_year, end_year) VALUES (%s, %s, %s)"
            " ON CONFLICT (id) DO NOTHING",
            (biennium, start, end),
        )

    def _upsert_members(self, biennium: str, members: list[dict]) -> None:
        rows, terms = [], []
        for m in members:
            mid = (m.get("Id") or "").strip()
            if not mid.isdigit():
                continue
            rows.append((
                int(mid), m.get("Name") or m.get("LongName") or mid, m.get("FirstName") or None,
                m.get("LastName") or None, m.get("Agency") or None, m.get("Party") or None,
                m.get("District") or None, m.get("Email") or None, m.get("Phone") or None, biennium,
            ))
            terms.append((
                int(mid), biennium, m.get("Agency") or None, m.get("Party") or None,
                m.get("District") or None, m.get("LongName") or None, m.get("Acronym") or None,
            ))
        with self.conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO members (id, name, first_name, last_name, agency, party, district,"
                " email, phone, latest_biennium) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
                " ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name, first_name = EXCLUDED.first_name,"
                " last_name = EXCLUDED.last_name, agency = EXCLUDED.agency, party = EXCLUDED.party,"
                " district = EXCLUDED.district, email = EXCLUDED.email, phone = EXCLUDED.phone,"
                " latest_biennium = EXCLUDED.latest_biennium, updated_at = now()"
                " WHERE members.latest_biennium IS NULL OR EXCLUDED.latest_biennium >= members.latest_biennium",
                rows,
            )
            cur.executemany(
                "INSERT INTO member_terms (member_id, biennium, agency, party, district, long_name, acronym)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (member_id, biennium) DO UPDATE SET"
                " agency = EXCLUDED.agency, party = EXCLUDED.party, district = EXCLUDED.district,"
                " long_name = EXCLUDED.long_name, acronym = EXCLUDED.acronym",
                terms,
            )

    def _fetch_missing(self, biennium: str, missing: list[Key]) -> list[dict]:
        out: list[dict] = []
        numbers = sorted({n for n, _ in missing})
        wanted = set(missing)
        with ThreadPoolExecutor(self.workers) as ex:
            futs = {ex.submit(self.client.legislation, biennium, n): n for n in numbers}
            for fut in as_completed(futs):
                try:
                    recs = fut.result()
                except Exception as e:  # keep going; the item is retried next sync
                    log.warning("GetLegislation %s %s failed: %s", biennium, futs[fut], e)
                    continue
                for r in recs:
                    v = compact_version(r)
                    if v and v["biennium"] == biennium and (v["number"], v["type_code"]) in wanted:
                        out.append(v)
        return out

    def _write_bills(self, biennium: str, bills: list[dict], actions: dict[Key, list]) -> None:
        with self.conn.cursor() as cur:
            cur.executemany(UPSERT_BILL_SQL, [tuple(b[c] for c in BILL_COLUMNS) for b in bills])
            ids = {
                (r["bill_number"], r["type_code"]): r["id"]
                for r in cur.execute(
                    "SELECT id, bill_number, type_code FROM bills WHERE biennium = %s", (biennium,)
                )
            }
            cur.execute(
                "DELETE FROM bill_actions WHERE bill_pk IN (SELECT id FROM bills WHERE biennium = %s)",
                (biennium,),
            )
            with cur.copy(
                "COPY bill_actions (bill_pk, action_date, version_id, description, seq) FROM STDIN"
            ) as copy:
                for k, acts in actions.items():
                    pk = ids.get(k)
                    if pk is None:
                        continue
                    for seq, (d, vid, line) in enumerate(acts):
                        copy.write_row((pk, d, vid, line, seq))

    # Sponsors + roll calls ------------------------------------------------------------

    def sync_details(self, biennium: str, force: bool = False, bill_pks: Iterable[int] | None = None) -> dict:
        q = (
            "SELECT id, bill_number, type_code, bill_id, last_action_date FROM bills WHERE biennium = %s"
        )
        params: list = [biennium]
        if bill_pks is not None:
            q += " AND id = ANY(%s)"
            params.append(list(bill_pks))
        elif not force:
            q += " AND (details_synced_at IS NULL OR details_action_date IS DISTINCT FROM last_action_date)"
        todo = list(self.conn.execute(q + " ORDER BY last_action_date DESC NULLS LAST", params))
        if not todo:
            return {"details_bills": 0}

        lines: dict[int, list[tuple[date | None, str]]] = defaultdict(list)
        for r in self.conn.execute(
            "SELECT bill_pk, action_date, description FROM bill_actions WHERE bill_pk = ANY(%s) ORDER BY bill_pk, seq",
            ([t["id"] for t in todo],),
        ):
            lines[r["bill_pk"]].append((r["action_date"], r["description"]))
        self.conn.commit()

        log.info("%s: fetching sponsors/votes for %d bills", biennium, len(todo))
        done = errors = n_rolls = 0
        t0 = time.time()
        with ThreadPoolExecutor(self.workers) as ex:
            futs = {
                ex.submit(self._fetch_details, biennium, t, lines.get(t["id"], [])): t for t in todo
            }
            for fut in as_completed(futs):
                t = futs[fut]
                try:
                    sponsors, rolls = fut.result()
                except Exception as e:
                    errors += 1
                    log.warning("details %s %s failed: %s", biennium, t["bill_id"], e)
                    continue
                n_rolls += self._write_details(t, sponsors, rolls, lines.get(t["id"], []))
                done += 1
                if done % 250 == 0:
                    self.conn.commit()
                    log.info("%s: details %d/%d (%.0fs)", biennium, done, len(todo), time.time() - t0)
        self.conn.execute("UPDATE bienniums SET details_synced_at = now() WHERE id = %s", (biennium,))
        self.conn.commit()
        return {"details_bills": done, "details_errors": errors, "roll_calls": n_rolls}

    def _fetch_details(self, biennium: str, bill: dict, lines: list[tuple[date | None, str]]):
        sponsors: list[dict] = []
        if bill["type_code"] not in ("GA", "I"):
            sponsors = self.client.sponsors(biennium, bill["bill_id"])
        rolls: list[dict] = []
        if any(YEAS_NAYS_RE.search(text) for _, text in lines):
            for rc in self.client.roll_calls(biennium, bill["bill_number"]):
                parts = split_bill_id(rc.get("BillId", ""))
                if parts and TYPE_BY_PREFIX[parts[0]] == bill["type_code"]:
                    rolls.append(parse_roll_call(rc))
        return sponsors, rolls

    def _write_details(self, bill: dict, sponsors: list[dict], rolls: list[dict], lines) -> int:
        pk = bill["id"]
        seen: dict[int, tuple] = {}
        placeholders: dict[int, tuple] = {}
        for s in sponsors:
            sid = (s.get("Id") or "").strip()
            if not sid.isdigit():
                continue
            mid = int(sid)
            order = (s.get("Order") or "").strip()
            row = (pk, mid, s.get("Type") or "Secondary", int(order) if order.isdigit() else None, s.get("Name") or None)
            # The service repeats sponsors once per bill version; keep the first (primary wins).
            if mid not in seen or (row[2] == "Primary" and seen[mid][2] != "Primary"):
                seen[mid] = row
            full = " ".join(x for x in (s.get("FirstName"), s.get("LastName")) if x) or s.get("Name") or sid
            placeholders[mid] = (mid, full, s.get("FirstName") or None, s.get("LastName") or None, s.get("Agency") or None)
        for rc in rolls:
            for mid, name, _ in rc["votes"]:
                placeholders.setdefault(mid, (mid, name or str(mid), None, name, rc["agency"] or None))

        with self.conn.cursor() as cur:
            if placeholders:
                cur.executemany(
                    "INSERT INTO members (id, name, first_name, last_name, agency) VALUES (%s,%s,%s,%s,%s)"
                    " ON CONFLICT (id) DO NOTHING",
                    list(placeholders.values()),
                )
            cur.execute("DELETE FROM bill_sponsors WHERE bill_pk = %s", (pk,))
            if seen:
                cur.executemany(
                    "INSERT INTO bill_sponsors (bill_pk, member_id, sponsor_type, sponsor_order, name)"
                    " VALUES (%s,%s,%s,%s,%s)",
                    list(seen.values()),
                )
            cur.execute("DELETE FROM roll_calls WHERE bill_pk = %s", (pk,))
            for rc in rolls:
                same_day = [text for d, text in lines if d == rc["vote_date"]]
                result = roll_call_result(rc["yeas"], rc["nays"], same_day)
                rid = cur.execute(
                    "INSERT INTO roll_calls (bill_pk, biennium, agency, version_id, motion, sequence_number,"
                    " vote_date, yeas, nays, absent, excused, result)"
                    " SELECT %s, biennium, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s FROM bills WHERE id = %s"
                    " RETURNING id",
                    (pk, rc["agency"], rc["version_id"], rc["motion"], rc["sequence_number"], rc["vote_date"],
                     rc["yeas"], rc["nays"], rc["absent"], rc["excused"], result, pk),
                ).fetchone()["id"]
                uniq = {mid: (rid, mid, name, vote) for mid, name, vote in rc["votes"]}
                if uniq:
                    cur.executemany(
                        "INSERT INTO votes (roll_call_id, member_id, member_name, vote) VALUES (%s,%s,%s,%s)",
                        list(uniq.values()),
                    )
            cur.execute(
                "UPDATE bills SET details_action_date = last_action_date, details_synced_at = now() WHERE id = %s",
                (pk,),
            )
        return len(rolls)
