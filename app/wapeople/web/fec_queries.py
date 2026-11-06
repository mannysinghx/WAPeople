"""SQL for the federal money views. Every query is parameterized; sort orders come from whitelists."""
from __future__ import annotations

import psycopg

from .queries import PAGE_SIZE, Page, clamp_page

CONTRIBUTION_SORTS = {
    "recent": "tx_date DESC NULLS LAST, sub_id DESC",
    "oldest": "tx_date ASC NULLS LAST, sub_id",
    "largest": "amount DESC, sub_id DESC",
}
SPENDING_SORTS = CONTRIBUTION_SORTS


def cycles(c: psycopg.Connection, person_id: int) -> list[dict]:
    return c.execute("SELECT * FROM fec_cycles WHERE person_pk = %s ORDER BY cycle DESC, receipts DESC", (person_id,)).fetchall()


def other_committees(c: psycopg.Connection, person_id: int) -> list[dict]:
    """Leadership and joint fundraising committees the FEC links to this person."""
    return c.execute(
        "SELECT committee_id, max(name) AS name, min(designation) AS designation, min(cycle) AS first_cycle,"
        " max(cycle) AS last_cycle FROM fec_committees WHERE person_pk = %s AND NOT is_campaign"
        " GROUP BY committee_id ORDER BY max(cycle) DESC, committee_id", (person_id,)).fetchall()


def source_types(c: psycopg.Connection, person_id: int) -> list[str]:
    return [r["source_type"] for r in c.execute(
        "SELECT source_type, sum(amount) AS t FROM fec_contributions WHERE person_pk = %s AND NOT is_memo"
        " GROUP BY 1 ORDER BY t DESC", (person_id,))]


def _page(c, table, where, params, order, page) -> Page:
    w = " WHERE " + " AND ".join(where)
    # Counted rows and the total shown beside them leave out memo entries, unless asked to list them.
    summary = c.execute(f"SELECT count(*) AS n, coalesce(sum(amount) FILTER (WHERE NOT is_memo), 0) AS total FROM {table}{w}", params).fetchone()
    page = clamp_page(page, summary["n"])
    items = c.execute(f"SELECT * FROM {table}{w} ORDER BY {order} LIMIT %(limit)s OFFSET %(offset)s",
                      {**params, "limit": PAGE_SIZE, "offset": (page - 1) * PAGE_SIZE}).fetchall()
    return Page(items, summary["n"], page, extra={"total": summary["total"]})


def contributions(c: psycopg.Connection, person_id: int, *, cycle: int | None = None, source: str = "", state: str = "",
                  q: str = "", min_amount: float | None = None, memo: bool = False, sort: str = "recent",
                  page: int = 1) -> Page:
    where, params = ["person_pk = %(p)s"], {"p": person_id}
    if not memo:
        where.append("NOT is_memo")
    if cycle:
        where.append("cycle = %(cycle)s")
        params["cycle"] = cycle
    if source:
        where.append("source_type = %(source)s")
        params["source"] = source
    if state == "WA":
        where.append("upper(state) = 'WA'")
    elif state == "OUT":
        where.append("state IS NOT NULL AND upper(state) <> 'WA'")
    if q:
        where.append("(name ILIKE %(like)s OR employer ILIKE %(like)s OR occupation ILIKE %(like)s OR city ILIKE %(like)s"
                     " OR connected_org ILIKE %(like)s)")
        params["like"] = f"%{q}%"
    if min_amount:
        where.append("amount >= %(min)s")
        params["min"] = min_amount
    return _page(c, "fec_contributions", where, params, CONTRIBUTION_SORTS.get(sort) or CONTRIBUTION_SORTS["recent"], page)


def top_individuals(c: psycopg.Connection, person_id: int, cycle: int | None = None, limit: int = 15) -> list[dict]:
    return c.execute(
        "SELECT max(name) AS name, sum(amount) AS total, count(*) AS n, max(employer) AS employer, max(occupation) AS occupation,"
        " max(city) AS city, max(state) AS state FROM fec_contributions"
        " WHERE person_pk = %(p)s AND source_type = 'Individuals (itemized)' AND NOT is_memo"
        " AND (%(cycle)s::int IS NULL OR cycle = %(cycle)s)"
        " GROUP BY upper(name) ORDER BY sum(amount) DESC LIMIT %(limit)s",
        {"p": person_id, "cycle": cycle, "limit": limit},
    ).fetchall()


def spending(c: psycopg.Connection, person_id: int, *, cycle: int | None = None, category: str = "", q: str = "",
             memo: bool = False, sort: str = "recent", page: int = 1) -> Page:
    where, params = ["person_pk = %(p)s"], {"p": person_id}
    if not memo:
        where.append("NOT is_memo")
    if cycle:
        where.append("cycle = %(cycle)s")
        params["cycle"] = cycle
    if category:
        where.append("category = %(category)s")
        params["category"] = category
    if q:
        where.append("(name ILIKE %(like)s OR purpose ILIKE %(like)s)")
        params["like"] = f"%{q}%"
    return _page(c, "fec_disbursements", where, params, SPENDING_SORTS.get(sort) or SPENDING_SORTS["recent"], page)


def outside(c: psycopg.Connection, person_id: int, page: int = 1) -> dict:
    by_spender = c.execute(
        "SELECT cycle, spender_id, max(spender_name) AS spender_name, support_oppose, sum(amount) AS total, count(*) AS n,"
        " min(tx_date) AS first_date, max(tx_date) AS last_date FROM fec_outside WHERE person_pk = %s"
        " GROUP BY cycle, spender_id, support_oppose ORDER BY cycle DESC, total DESC", (person_id,)).fetchall()
    totals = c.execute(
        "SELECT coalesce(sum(amount) FILTER (WHERE support_oppose = 'S'), 0) AS supporting,"
        " coalesce(sum(amount) FILTER (WHERE support_oppose = 'O'), 0) AS opposing, count(*) AS n"
        " FROM fec_outside WHERE person_pk = %s", (person_id,)).fetchone()
    where, params = ["person_pk = %(p)s"], {"p": person_id}
    w = " WHERE " + where[0]
    n = c.execute(f"SELECT count(*) AS n FROM fec_outside{w}", params).fetchone()["n"]
    page = clamp_page(page, n)
    items = c.execute(f"SELECT * FROM fec_outside{w} ORDER BY tx_date DESC NULLS LAST, id LIMIT %(limit)s OFFSET %(offset)s",
                      {**params, "limit": PAGE_SIZE, "offset": (page - 1) * PAGE_SIZE}).fetchall()
    return {"by_spender": by_spender, "outside_totals": totals, "listing": Page(items, n, page)}


def tab_counts(c: psycopg.Connection, person_id: int) -> dict:
    return c.execute(
        "SELECT (SELECT count(*) FROM fec_contributions WHERE person_pk = %(p)s AND NOT is_memo) AS contributions,"
        " (SELECT count(*) FROM fec_disbursements WHERE person_pk = %(p)s AND NOT is_memo) AS spending,"
        " (SELECT count(*) FROM fec_outside WHERE person_pk = %(p)s) AS outside", {"p": person_id}).fetchone()
