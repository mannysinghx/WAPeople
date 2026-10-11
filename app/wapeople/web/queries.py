"""SQL for the website. Every query is parameterized; filters come from whitelists."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

import psycopg

from ..legislature.parse import FINAL_VOTE_KINDS

PAGE_SIZE = 25

TYPE_FILTERS = {
    "B": "Bills",
    "JR": "Constitutional amendments (joint resolutions)",
    "JM": "Joint memorials",
    "CR": "Concurrent resolutions",
    "R": "Resolutions",
    "I": "Initiatives to the Legislature",
    "GA": "Governor's appointments",
    "all": "All types",
}
BUCKETS = {
    "proposed": "Proposed",
    "ongoing": "Ongoing",
    "past": "Past",
}
STAGE_FILTERS = {
    "enacted": "Became law (any)",
    "prefiled": "Prefiled",
    "introduced": "Introduced",
    "committee": "In committee",
    "floor": "Awaiting floor action",
    "passed_origin": "Passed one chamber",
    "failed_vote": "Failed floor vote",
    "passed_legislature": "Passed Legislature",
    "law": "Enacted",
    "law_partial_veto": "Enacted (partial veto)",
    "vetoed": "Vetoed",
    "filed": "Filed with Secretary of State",
    "adopted": "Adopted",
    "confirmed": "Confirmed",
    "not_confirmed": "Not confirmed",
    "did_not_pass": "Did not pass",
}
BILL_SORTS = {
    "recent": "b.last_action_date DESC NULLS LAST, b.bill_number",
    "number": "b.biennium DESC, b.bill_number",
    "introduced": "b.introduced_date DESC NULLS LAST, b.bill_number",
    "relevance": None,  # only meaningful with a text query
}

_BILL_REF_RE = re.compile(
    r"^\s*(?P<prefix>\d?E?\d?S?(?:HB|SB|HJR|SJR|HJM|SJM|HCR|SCR|HR|SR|SGA|HI|SI))?\s*-?\s*(?P<num>\d{3,4})\s*$",
    re.I,
)


@dataclass
class Page:
    items: list[dict]
    total: int
    page: int
    page_size: int = PAGE_SIZE
    extra: dict = field(default_factory=dict)

    @property
    def pages(self) -> int:
        return max(1, -(-self.total // self.page_size))


def parse_bill_ref(q: str) -> tuple[str | None, int] | None:
    """'ESSB 5096' -> ('SB', 5096); '1217' -> (None, 1217)."""
    m = _BILL_REF_RE.match(q or "")
    if not m:
        return None
    prefix = (m["prefix"] or "").upper() or None
    if prefix:
        prefix = re.sub(r"^\d?E?\d?S?(?=(HB|SB|HJR|SJR|HJM|SJM|HCR|SCR|HR|SR|SGA|HI|SI)$)", "", prefix)
    return prefix, int(m["num"])


# ---- Site-wide --------------------------------------------------------------------

def bienniums(c: psycopg.Connection) -> list[dict]:
    return c.execute(
        "SELECT id, start_year, end_year, status, last_activity_date, bills_synced_at"
        " FROM bienniums WHERE bills_synced_at IS NOT NULL ORDER BY id DESC"
    ).fetchall()


def last_updates(c: psycopg.Connection) -> dict:
    rows = c.execute(
        "SELECT job, max(finished_at) AS t FROM sync_runs WHERE status = 'ok' GROUP BY job"
    ).fetchall()
    return {r["job"]: r["t"] for r in rows}


def home_counts(c: psycopg.Connection, current: str | None) -> dict:
    row = c.execute(
        "SELECT"
        " count(*) FILTER (WHERE bucket = 'proposed' AND type_code = 'B') AS proposed,"
        " count(*) FILTER (WHERE bucket = 'ongoing' AND type_code = 'B') AS ongoing,"
        " count(*) FILTER (WHERE bucket = 'past' AND type_code = 'B') AS past,"
        " count(*) FILTER (WHERE type_code = 'B' AND biennium = %(cur)s) AS cur_bills,"
        " count(*) FILTER (WHERE type_code = 'B' AND biennium = %(cur)s AND stage IN ('law','law_partial_veto')) AS cur_laws,"
        " count(*) FILTER (WHERE type_code = 'B' AND biennium = %(cur)s AND stage = 'vetoed') AS cur_vetoed,"
        " count(*) FILTER (WHERE type_code = 'B' AND biennium = %(cur)s AND stage = 'did_not_pass') AS cur_dead,"
        " count(*) FILTER (WHERE type_code = 'B') AS all_bills"
        " FROM bills",
        {"cur": current},
    ).fetchone()
    row["members"] = c.execute("SELECT count(*) AS n FROM members").fetchone()["n"]
    row["roll_calls"] = c.execute("SELECT count(*) AS n FROM roll_calls").fetchone()["n"]
    row["cases"] = c.execute("SELECT count(*) AS n FROM court_cases").fetchone()["n"]
    return row


# ---- Bills ------------------------------------------------------------------------

BILL_LIST_COLS = (
    "b.id, b.biennium, b.bill_id, b.bill_number, b.type_code, b.current_version,"
    " b.short_description, b.long_description, b.stage, b.stage_label, b.bucket,"
    " b.introduced_date, b.last_action_date, b.original_agency, b.prime_sponsor_id,"
    " m.name AS prime_name, mt.party AS prime_party, mt.district AS prime_district"
)
BILL_LIST_FROM = (
    " FROM bills b LEFT JOIN members m ON m.id = b.prime_sponsor_id"
    " LEFT JOIN member_terms mt ON mt.member_id = b.prime_sponsor_id AND mt.biennium = b.biennium"
)


def search_bills(
    c: psycopg.Connection,
    *,
    q: str = "",
    bucket: str = "",
    biennium: str = "",
    stage: str = "",
    chamber: str = "",
    type_code: str = "B",
    sponsor: int | None = None,
    sort: str = "recent",
    page: int = 1,
    page_size: int = PAGE_SIZE,
) -> Page:
    where, params = ["TRUE"], {}
    rank_sql = None
    ref = parse_bill_ref(q) if q else None
    if ref:
        prefix, num = ref
        where.append("b.bill_number = %(num)s")
        params["num"] = num
        if prefix:
            where.append("split_part(b.bill_id, ' ', 1) = %(prefix)s")
            params["prefix"] = prefix
        type_code = type_code if prefix is None else "all"
    elif q:
        where.append("b.search @@ websearch_to_tsquery('english', %(q)s)")
        params["q"] = q
        rank_sql = "ts_rank_cd(b.search, websearch_to_tsquery('english', %(q)s))"
    if bucket in BUCKETS:
        where.append("b.bucket = %(bucket)s")
        params["bucket"] = bucket
    if biennium:
        where.append("b.biennium = %(biennium)s")
        params["biennium"] = biennium
    if stage == "enacted":
        where.append("b.stage IN ('law', 'law_partial_veto')")
    elif stage in STAGE_FILTERS:
        where.append("b.stage = %(stage)s")
        params["stage"] = stage
    if chamber in ("House", "Senate"):
        where.append("b.original_agency = %(chamber)s")
        params["chamber"] = chamber
    if type_code in TYPE_FILTERS and type_code != "all":
        where.append("b.type_code = %(type_code)s")
        params["type_code"] = type_code
    if sponsor:
        where.append("EXISTS (SELECT 1 FROM bill_sponsors s WHERE s.bill_pk = b.id AND s.member_id = %(sponsor)s)")
        params["sponsor"] = sponsor

    w = " WHERE " + " AND ".join(where)
    total = c.execute("SELECT count(*) AS n" + BILL_LIST_FROM + w, params).fetchone()["n"]
    if sort == "relevance" and rank_sql:
        order = f"{rank_sql} DESC, b.last_action_date DESC NULLS LAST"
    else:
        order = BILL_SORTS.get(sort) or BILL_SORTS["recent"]
    params["limit"] = page_size
    params["offset"] = (page - 1) * page_size
    items = c.execute(
        f"SELECT {BILL_LIST_COLS}{BILL_LIST_FROM}{w} ORDER BY {order} LIMIT %(limit)s OFFSET %(offset)s",
        params,
    ).fetchall()
    return Page(items, total, page, page_size)


def stage_counts(c: psycopg.Connection, biennium: str, type_code: str = "B") -> list[dict]:
    return c.execute(
        "SELECT stage, count(*) AS n FROM bills WHERE biennium = %s AND type_code = %s GROUP BY stage ORDER BY n DESC",
        (biennium, type_code),
    ).fetchall()


def get_bill(c: psycopg.Connection, biennium: str, prefix: str, number: int) -> dict | None:
    return c.execute(
        "SELECT b.*, m.name AS prime_name,"
        " mt.party AS prime_party, mt.district AS prime_district, bn.status AS biennium_status"
        f"{BILL_LIST_FROM} JOIN bienniums bn ON bn.id = b.biennium"
        " WHERE b.biennium = %s AND b.bill_number = %s AND split_part(b.bill_id, ' ', 1) = %s",
        (biennium, number, prefix),
    ).fetchone()


def bill_sponsors(c: psycopg.Connection, bill: dict) -> list[dict]:
    return c.execute(
        "SELECT s.member_id, s.sponsor_type, s.sponsor_order, coalesce(m.name, s.name) AS name,"
        " mt.party, mt.district, coalesce(mt.agency, m.agency) AS agency"
        " FROM bill_sponsors s LEFT JOIN members m ON m.id = s.member_id"
        " LEFT JOIN member_terms mt ON mt.member_id = s.member_id AND mt.biennium = %s"
        " WHERE s.bill_pk = %s ORDER BY (s.sponsor_type = 'Primary') DESC, s.sponsor_order NULLS LAST",
        (bill["biennium"], bill["id"]),
    ).fetchall()


def bill_actions(c: psycopg.Connection, bill_pk: int) -> list[dict]:
    return c.execute(
        "SELECT action_date, version_id, description FROM bill_actions WHERE bill_pk = %s ORDER BY seq",
        (bill_pk,),
    ).fetchall()


def bill_roll_calls(c: psycopg.Connection, bill: dict) -> list[dict]:
    rolls = c.execute(
        "SELECT * FROM roll_calls WHERE bill_pk = %s ORDER BY vote_date, sequence_number",
        (bill["id"],),
    ).fetchall()
    if not rolls:
        return []
    votes = c.execute(
        "SELECT v.roll_call_id, v.member_id, coalesce(m.name, v.member_name) AS name, v.vote,"
        " mt.party, mt.district"
        " FROM votes v LEFT JOIN members m ON m.id = v.member_id"
        " LEFT JOIN member_terms mt ON mt.member_id = v.member_id AND mt.biennium = %s"
        " WHERE v.roll_call_id = ANY(%s) ORDER BY m.last_name NULLS LAST, name",
        (bill["biennium"], [r["id"] for r in rolls]),
    ).fetchall()
    by_roll: dict[int, list[dict]] = {}
    for v in votes:
        by_roll.setdefault(v["roll_call_id"], []).append(v)
    for r in rolls:
        r["votes"] = by_roll.get(r["id"], [])
        r["party_breakdown"] = party_breakdown(r["votes"])
    return rolls


def party_breakdown(votes: list[dict]) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for v in votes:
        p = v.get("party") or "?"
        out.setdefault(p, {}).setdefault(v["vote"], 0)
        out[p][v["vote"]] += 1
    return dict(sorted(out.items()))


def bill_cases(c: psycopg.Connection, bill_pk: int) -> list[dict]:
    return c.execute(
        "SELECT cc.id, cc.case_name, cc.date_filed, cc.categories, l.link_type, l.mention"
        " FROM case_bill_links l JOIN court_cases cc ON cc.id = l.case_id"
        " WHERE l.bill_pk = %s ORDER BY cc.date_filed DESC",
        (bill_pk,),
    ).fetchall()


def other_sessions(c: psycopg.Connection, bill: dict) -> list[dict]:
    """Same number in other bienniums (handy because numbers repeat)."""
    return c.execute(
        "SELECT biennium, bill_id, short_description, stage_label FROM bills"
        " WHERE bill_number = %s AND split_part(bill_id, ' ', 1) = split_part(%s, ' ', 1) AND id <> %s"
        " ORDER BY biennium DESC LIMIT 8",
        (bill["bill_number"], bill["bill_id"], bill["id"]),
    ).fetchall()


# ---- Legislators ------------------------------------------------------------------

def list_members(
    c: psycopg.Connection, *, biennium: str, chamber: str = "", party: str = "", q: str = "",
    district: str = "",
) -> list[dict]:
    where, params = ["mt.biennium = %(b)s"], {"b": biennium}
    if chamber in ("House", "Senate"):
        where.append("mt.agency = %(chamber)s")
        params["chamber"] = chamber
    if party:
        where.append("mt.party = %(party)s")
        params["party"] = party
    if district:
        where.append("mt.district = %(district)s")
        params["district"] = district
    if q:
        where.append("(m.name ILIKE %(like)s OR m.name %% %(q)s)")
        params["like"] = f"%{q}%"
        params["q"] = q
    return c.execute(
        "SELECT m.id, m.name, m.last_name, mt.agency, mt.party, mt.district,"
        " (SELECT count(*) FROM bill_sponsors s JOIN bills b ON b.id = s.bill_pk"
        "   WHERE s.member_id = m.id AND s.sponsor_type = 'Primary' AND b.biennium = %(b)s AND b.type_code = 'B') AS prime_bills,"
        " (SELECT count(*) FROM bill_sponsors s JOIN bills b ON b.id = s.bill_pk"
        "   WHERE s.member_id = m.id AND s.sponsor_type = 'Primary' AND b.biennium = %(b)s AND b.type_code = 'B'"
        "   AND b.stage IN ('law','law_partial_veto')) AS prime_laws"
        " FROM member_terms mt JOIN members m ON m.id = mt.member_id"
        " WHERE " + " AND ".join(where) +
        " ORDER BY mt.agency DESC, NULLIF(regexp_replace(mt.district, '\\D', '', 'g'), '')::int NULLS LAST, m.last_name",
        params,
    ).fetchall()


def get_member(c: psycopg.Connection, member_id: int) -> dict | None:
    m = c.execute("SELECT * FROM members WHERE id = %s", (member_id,)).fetchone()
    if not m:
        return None
    m["terms"] = c.execute(
        "SELECT * FROM member_terms WHERE member_id = %s ORDER BY biennium DESC", (member_id,)
    ).fetchall()
    return m


def member_stats(c: psycopg.Connection, member_id: int) -> dict:
    s = c.execute(
        "SELECT"
        " count(*) FILTER (WHERE s.sponsor_type = 'Primary') AS prime,"
        " count(*) FILTER (WHERE s.sponsor_type <> 'Primary') AS cosponsor,"
        " count(*) FILTER (WHERE s.sponsor_type = 'Primary' AND b.stage IN ('law','law_partial_veto')) AS prime_laws"
        " FROM bill_sponsors s JOIN bills b ON b.id = s.bill_pk"
        " WHERE s.member_id = %s AND b.type_code = 'B'",
        (member_id,),
    ).fetchone()
    v = c.execute(
        "SELECT count(*) AS total,"
        " count(*) FILTER (WHERE vote = 'Yea') AS yea, count(*) FILTER (WHERE vote = 'Nay') AS nay,"
        " count(*) FILTER (WHERE vote = 'Absent') AS absent, count(*) FILTER (WHERE vote = 'Excused') AS excused"
        " FROM votes WHERE member_id = %s",
        (member_id,),
    ).fetchone()
    s.update({f"votes_{k}": val for k, val in v.items()})
    return s


def member_votes(
    c: psycopg.Connection, member_id: int, *, biennium: str = "", vote: str = "",
    final_only: bool = True, q: str = "", page: int = 1,
) -> Page:
    where, params = ["v.member_id = %(m)s"], {"m": member_id}
    if biennium:
        where.append("rc.biennium = %(b)s")
        params["b"] = biennium
    if vote in ("Yea", "Nay", "Absent", "Excused"):
        where.append("v.vote = %(vote)s")
        params["vote"] = vote
    if final_only:
        where.append("rc.vote_kind = ANY(%(final_kinds)s)")
        params["final_kinds"] = list(FINAL_VOTE_KINDS)
    if q:
        ref = parse_bill_ref(q)
        if ref:
            where.append("b.bill_number = %(num)s")
            params["num"] = ref[1]
        else:
            where.append("b.search @@ websearch_to_tsquery('english', %(q)s)")
            params["q"] = q
    w = " WHERE " + " AND ".join(where)
    frm = " FROM votes v JOIN roll_calls rc ON rc.id = v.roll_call_id JOIN bills b ON b.id = rc.bill_pk"
    total = c.execute("SELECT count(*) AS n" + frm + w, params).fetchone()["n"]
    params.update(limit=PAGE_SIZE, offset=(page - 1) * PAGE_SIZE)
    items = c.execute(
        "SELECT v.vote, rc.id AS roll_call_id, rc.vote_date, rc.agency, rc.motion, rc.yeas, rc.nays,"
        " rc.result, rc.vote_kind, b.biennium, b.bill_id, b.short_description, b.stage_label"
        + frm + w + " ORDER BY rc.vote_date DESC, rc.id DESC LIMIT %(limit)s OFFSET %(offset)s",
        params,
    ).fetchall()
    return Page(items, total, page)


# ---- Roll calls -------------------------------------------------------------------

def list_roll_calls(
    c: psycopg.Connection, *, biennium: str = "", chamber: str = "", final_only: bool = True,
    q: str = "", close: bool = False, page: int = 1,
) -> Page:
    where, params = ["TRUE"], {}
    if biennium:
        where.append("rc.biennium = %(b)s")
        params["b"] = biennium
    if chamber in ("House", "Senate"):
        where.append("rc.agency = %(chamber)s")
        params["chamber"] = chamber
    if final_only:
        where.append("rc.vote_kind = ANY(%(final_kinds)s)")
        params["final_kinds"] = list(FINAL_VOTE_KINDS)
    if close:
        where.append("abs(rc.yeas - rc.nays) <= 6")
    if q:
        ref = parse_bill_ref(q)
        if ref:
            where.append("b.bill_number = %(num)s")
            params["num"] = ref[1]
        else:
            where.append("b.search @@ websearch_to_tsquery('english', %(q)s)")
            params["q"] = q
    w = " WHERE " + " AND ".join(where)
    frm = " FROM roll_calls rc JOIN bills b ON b.id = rc.bill_pk"
    total = c.execute("SELECT count(*) AS n" + frm + w, params).fetchone()["n"]
    params.update(limit=PAGE_SIZE, offset=(page - 1) * PAGE_SIZE)
    items = c.execute(
        "SELECT rc.*, b.bill_id, b.short_description, b.stage_label, b.bill_number" + frm + w +
        " ORDER BY rc.vote_date DESC NULLS LAST, rc.id DESC LIMIT %(limit)s OFFSET %(offset)s",
        params,
    ).fetchall()
    return Page(items, total, page)


def get_roll_call(c: psycopg.Connection, rid: int) -> dict | None:
    rc = c.execute(
        "SELECT rc.*, b.bill_id, b.bill_number, b.short_description, b.stage_label, b.biennium AS bill_biennium"
        " FROM roll_calls rc JOIN bills b ON b.id = rc.bill_pk WHERE rc.id = %s",
        (rid,),
    ).fetchone()
    if not rc:
        return None
    rc["votes"] = c.execute(
        "SELECT v.member_id, coalesce(m.name, v.member_name) AS name, v.vote, mt.party, mt.district"
        " FROM votes v LEFT JOIN members m ON m.id = v.member_id"
        " LEFT JOIN member_terms mt ON mt.member_id = v.member_id AND mt.biennium = %s"
        " WHERE v.roll_call_id = %s ORDER BY m.last_name NULLS LAST, name",
        (rc["biennium"], rid),
    ).fetchall()
    rc["party_breakdown"] = party_breakdown(rc["votes"])
    return rc


# ---- Court cases ------------------------------------------------------------------

CASE_SORTS = {
    "newest": "cc.date_filed DESC NULLS LAST",
    "oldest": "cc.date_filed ASC NULLS LAST",
    "cited": "cc.cite_count DESC, cc.date_filed DESC",
    "relevance": None,
}


def search_cases(
    c: psycopg.Connection, *, q: str = "", category: str = "", year_from: int | None = None,
    year_to: int | None = None, linked: bool = False, sort: str = "newest", page: int = 1,
    page_size: int = PAGE_SIZE,
) -> Page:
    where, params = ["TRUE"], {}
    rank_sql = None
    if q:
        where.append("(cc.search @@ websearch_to_tsquery('english', %(q)s) OR cc.case_name ILIKE %(like)s)")
        params["q"] = q
        params["like"] = f"%{q}%"
        rank_sql = "ts_rank_cd(cc.search, websearch_to_tsquery('english', %(q)s))"
    if category:
        where.append("%(cat)s = ANY(cc.categories)")
        params["cat"] = category
    if year_from:
        where.append("cc.date_filed >= make_date(%(yf)s, 1, 1)")
        params["yf"] = year_from
    if year_to:
        where.append("cc.date_filed <= make_date(%(yt)s, 12, 31)")
        params["yt"] = year_to
    if linked:
        where.append("EXISTS (SELECT 1 FROM case_bill_links l WHERE l.case_id = cc.id)")
    w = " WHERE " + " AND ".join(where)
    total = c.execute("SELECT count(*) AS n FROM court_cases cc" + w, params).fetchone()["n"]
    order = (f"{rank_sql} DESC, cc.date_filed DESC" if sort == "relevance" and rank_sql
             else CASE_SORTS.get(sort) or CASE_SORTS["newest"])
    params.update(limit=page_size, offset=(page - 1) * page_size)
    items = c.execute(
        "SELECT cc.id, cc.case_name, cc.date_filed, cc.docket_number, cc.citations, cc.cite_count,"
        " cc.categories, cc.snippets, cc.pdf_url, cc.courtlistener_url,"
        " (SELECT count(*) FROM case_bill_links l WHERE l.case_id = cc.id) AS n_bills"
        " FROM court_cases cc" + w + f" ORDER BY {order} LIMIT %(limit)s OFFSET %(offset)s",
        params,
    ).fetchall()
    return Page(items, total, page, page_size)


def case_category_counts(c: psycopg.Connection) -> dict[str, int]:
    rows = c.execute(
        "SELECT cat, count(*) AS n FROM court_cases, unnest(categories) AS cat GROUP BY cat"
    ).fetchall()
    return {r["cat"]: r["n"] for r in rows}


def get_case(c: psycopg.Connection, case_id: int) -> dict | None:
    cc = c.execute(
        "SELECT id, case_name, case_name_full, date_filed, docket_number, citations, cite_count, status,"
        " judges, courtlistener_url, pdf_url, categories, snippets, bill_mentions, full_text_source,"
        " length(full_text) AS text_len FROM court_cases WHERE id = %s",
        (case_id,),
    ).fetchone()
    if not cc:
        return None
    cc["bills"] = c.execute(
        "SELECT b.id, b.biennium, b.bill_id, b.short_description, b.stage_label, l.link_type, l.mention"
        " FROM case_bill_links l JOIN bills b ON b.id = l.bill_pk WHERE l.case_id = %s"
        " ORDER BY b.biennium DESC, b.bill_number",
        (case_id,),
    ).fetchall()
    return cc


# ---- Global search ----------------------------------------------------------------

def search_members(c: psycopg.Connection, q: str, limit: int = 8) -> list[dict]:
    return c.execute(
        "SELECT m.id, m.name, m.party, m.agency, m.district, m.latest_biennium"
        " FROM members m WHERE m.name ILIKE %(like)s OR m.name %% %(q)s"
        " ORDER BY (m.name ILIKE %(like)s) DESC, similarity(m.name, %(q)s) DESC, m.latest_biennium DESC NULLS LAST"
        " LIMIT %(limit)s",
        {"like": f"%{q}%", "q": q, "limit": limit},
    ).fetchall()


def today() -> date:
    return date.today()
