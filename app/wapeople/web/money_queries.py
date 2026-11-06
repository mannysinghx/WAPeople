"""SQL for the money pages. Every query is parameterized; sort orders and filters come from whitelists."""
from __future__ import annotations

import psycopg

from .queries import PAGE_SIZE, Page, clamp_page

GROUPS = {
    "all": "Everyone",
    "house": "State House",
    "senate": "State Senate",
    "statewide": "Statewide officials",
    "justice": "Supreme Court justices",
    "federal": "U.S. Congress",
}
PEOPLE_SORTS = {
    # State and federal money are never added together: each person ranks by the figure the table
    # shows first, which is the FEC's for anyone with federal cycles.
    "raised": "CASE WHEN p.fed_cycles > 0 THEN p.fed_raised ELSE p.total_raised END DESC, p.name",
    "latest": "coalesce(p.fed_latest_raised, p.latest_raised) DESC NULLS LAST, p.name",
    "spent": "CASE WHEN p.fed_cycles > 0 THEN p.fed_spent ELSE p.total_spent END DESC, p.name",
    "name": "p.name",
    "district": "NULLIF(p.district, '')::int NULLS LAST, p.chamber DESC, p.name",
}
CONTRIBUTION_SORTS = {
    "recent": "receipt_date DESC NULLS LAST, id DESC",
    "oldest": "receipt_date ASC NULLS LAST, id",
    "largest": "amount DESC, id DESC",
}
SPENDING_SORTS = {
    "recent": "expenditure_date DESC NULLS LAST, id DESC",
    "oldest": "expenditure_date ASC NULLS LAST, id",
    "largest": "amount DESC, id DESC",
}


def overview(c: psycopg.Connection) -> dict:
    row = c.execute(
        "SELECT count(*) FILTER (WHERE is_current) AS current_people,"
        " count(*) FILTER (WHERE n_campaigns > 0) AS with_records,"
        " coalesce(sum(total_raised) FILTER (WHERE is_current), 0) AS current_raised,"
        " coalesce(sum(fed_raised) FILTER (WHERE is_current), 0) AS current_fed_raised,"
        " coalesce(sum(n_contributions + fed_contributions) FILTER (WHERE is_current), 0) AS contributions,"
        " max(updated_at) AS updated_at FROM money_people"
    ).fetchone()
    row["orgs"] = c.execute("SELECT count(*) AS n FROM money_orgs WHERE total > 0").fetchone()["n"]
    return row


def list_people(c: psycopg.Connection, *, group: str = "all", party: str = "", q: str = "",
                former: bool = False, sort: str = "raised") -> list[dict]:
    where, params = ["(p.is_current OR %(former)s)"], {"former": former}
    if group == "house":
        where.append("p.kind = 'legislator' AND p.chamber = 'House'")
    elif group == "senate":
        where.append("p.kind = 'legislator' AND p.chamber = 'Senate'")
    elif group in ("statewide", "justice", "federal"):
        where.append("p.kind = %(kind)s")
        params["kind"] = group
    if party in ("D", "R"):
        where.append("p.party = %(party)s")
        params["party"] = party
    if q:
        where.append("(p.name ILIKE %(like)s OR p.name %% %(q)s)")
        params.update(like=f"%{q}%", q=q)
    order = PEOPLE_SORTS.get(sort) or PEOPLE_SORTS["raised"]
    return c.execute(
        "SELECT p.id, p.member_id, p.name, p.kind, p.title, p.party, p.chamber, p.district, p.is_current,"
        " p.total_raised, p.total_spent, p.latest_year, p.latest_raised, p.in_state, p.out_of_state,"
        " p.not_stated, p.total_itemized, p.top_source, p.n_campaigns, p.fec_candidate_ids, p.fed_raised, p.fed_spent,"
        " p.fed_cycles, p.fed_latest_raised, p.fed_last_cycle, p.fed_in_state, p.fed_out_of_state, p.fed_top_source"
        " FROM money_people p"
        " WHERE " + " AND ".join(where) + f" ORDER BY {order} LIMIT 700",
        params,
    ).fetchall()


def get_person(c: psycopg.Connection, person_id: int) -> dict | None:
    return c.execute("SELECT * FROM money_people WHERE id = %s", (person_id,)).fetchone()


def campaigns(c: psycopg.Connection, person_id: int) -> list[dict]:
    return c.execute(
        "SELECT * FROM money_campaigns WHERE person_pk = %s ORDER BY election_year DESC, office, id DESC",
        (person_id,),
    ).fetchall()


def years(c: psycopg.Connection, person_id: int) -> list[int]:
    rows = c.execute(
        "SELECT DISTINCT election_year AS y FROM money_campaigns WHERE person_pk = %s ORDER BY 1 DESC", (person_id,)
    ).fetchall()
    return [r["y"] for r in rows]


def _page_query(c: psycopg.Connection, table: str, where: list[str], params: dict, order: str, page: int) -> Page:
    w = " WHERE " + " AND ".join(where)
    summary = c.execute(f"SELECT count(*) AS n, coalesce(sum(amount), 0) AS total FROM {table}{w}", params).fetchone()
    page = clamp_page(page, summary["n"])
    params = {**params, "limit": PAGE_SIZE, "offset": (page - 1) * PAGE_SIZE}
    items = c.execute(f"SELECT * FROM {table}{w} ORDER BY {order} LIMIT %(limit)s OFFSET %(offset)s", params).fetchall()
    return Page(items, summary["n"], page, extra={"total": summary["total"]})


def contributions(c: psycopg.Connection, person_id: int, *, year: int | None = None, source: str = "",
                  state: str = "", q: str = "", min_amount: float | None = None, donor: str = "",
                  sort: str = "recent", page: int = 1) -> Page:
    where, params = ["person_pk = %(p)s"], {"p": person_id}
    if year:
        where.append("election_year = %(year)s")
        params["year"] = year
    if source:
        where.append("source_type = %(source)s")
        params["source"] = source
    if state == "WA":
        where.append("upper(state) IN ('WA', 'WASHINGTON')")
    elif state == "OUT":
        where.append("state IS NOT NULL AND upper(state) NOT IN ('WA', 'WASHINGTON')")
    elif state == "NONE":
        where.append("state IS NULL")
    if q:
        where.append("(contributor_name ILIKE %(like)s OR employer ILIKE %(like)s OR occupation ILIKE %(like)s"
                     " OR city ILIKE %(like)s)")
        params["like"] = f"%{q}%"
    if donor:
        where.append("contributor_key = %(donor)s")
        params["donor"] = donor
    if min_amount:
        where.append("amount >= %(min)s")
        params["min"] = min_amount
    return _page_query(c, "money_contributions", where, params,
                       CONTRIBUTION_SORTS.get(sort) or CONTRIBUTION_SORTS["recent"], page)


def top_individuals(c: psycopg.Connection, person_id: int, year: int | None = None, limit: int = 15) -> list[dict]:
    params = {"p": person_id, "limit": limit, "year": year}
    return c.execute(
        "SELECT contributor_key AS key, max(contributor_name) AS name, sum(amount) AS total, count(*) AS n,"
        " max(employer) AS employer, max(occupation) AS occupation, max(city) AS city, max(state) AS state"
        " FROM money_contributions WHERE person_pk = %(p)s AND source_type = 'Individuals'"
        " AND (%(year)s::int IS NULL OR election_year = %(year)s)"
        " GROUP BY contributor_key ORDER BY sum(amount) DESC LIMIT %(limit)s",
        params,
    ).fetchall()


def spending(c: psycopg.Connection, person_id: int, *, year: int | None = None, category: str = "",
             q: str = "", sort: str = "recent", page: int = 1) -> Page:
    where, params = ["person_pk = %(p)s"], {"p": person_id}
    if year:
        where.append("election_year = %(year)s")
        params["year"] = year
    if category:
        where.append("category = %(category)s")
        params["category"] = category
    if q:
        where.append("(recipient_name ILIKE %(like)s OR description ILIKE %(like)s)")
        params["like"] = f"%{q}%"
    return _page_query(c, "money_expenditures", where, params, SPENDING_SORTS.get(sort) or SPENDING_SORTS["recent"], page)


def debts_and_loans(c: psycopg.Connection, person_id: int, page: int = 1) -> dict:
    return {
        "loans": c.execute("SELECT * FROM money_loans WHERE person_pk = %s ORDER BY receipt_date DESC NULLS LAST, id DESC", (person_id,)).fetchall(),
        "debts": _page_query(c, "money_debts", ["person_pk = %(p)s"], {"p": person_id}, "debt_date DESC NULLS LAST, id DESC", page),
        "pledges": c.execute("SELECT * FROM money_pledges WHERE person_pk = %s ORDER BY receipt_date DESC NULLS LAST, id DESC", (person_id,)).fetchall(),
    }


def outside(c: psycopg.Connection, person_id: int) -> dict:
    by_sponsor = c.execute(
        "SELECT sponsor_id, max(sponsor_name) AS sponsor_name, election_year, for_or_against,"
        " sum(amount) AS total, count(*) AS n, min(report_date) AS first_date, max(report_date) AS last_date,"
        " array_agg(DISTINCT report_number) AS reports, max(url) AS url"
        " FROM money_outside WHERE person_pk = %s"
        " GROUP BY sponsor_id, election_year, for_or_against ORDER BY election_year DESC, total DESC",
        (person_id,),
    ).fetchall()
    # Every report a sponsor files restates its funders, so the same gift shows up once per report.
    # Each gift is counted once: one sponsor, funder, date and amount.
    funders = c.execute(
        "SELECT g.sponsor_id, max(g.sponsor_name) AS sponsor_name, g.funder_name, max(g.employer) AS employer,"
        " max(g.city) AS city, max(g.state) AS state, sum(g.amount) AS total, count(*) AS n"
        " FROM (SELECT f.sponsor_id, f.funder_name, f.date_received, f.amount, max(f.sponsor_name) AS sponsor_name,"
        "  max(f.employer) AS employer, max(f.city) AS city, max(f.state) AS state"
        "  FROM money_outside_funders f"
        "  WHERE f.report_number IN (SELECT report_number FROM money_outside WHERE person_pk = %s)"
        "  GROUP BY f.sponsor_id, f.funder_name, f.date_received, f.amount) g"
        " GROUP BY g.sponsor_id, g.funder_name ORDER BY total DESC LIMIT 60",
        (person_id,),
    ).fetchall()
    return {"by_sponsor": by_sponsor, "funders": funders}


def surplus(c: psycopg.Connection, person_id: int, page: int = 1) -> dict:
    accounts = c.execute("SELECT * FROM money_surplus WHERE person_pk = %s ORDER BY thru_date DESC NULLS LAST", (person_id,)).fetchall()
    where, params = ["person_pk = %(p)s"], {"p": person_id}
    return {"accounts": accounts,
            "spending": _page_query(c, "money_surplus_spending", where, params,
                                    "expenditure_date DESC NULLS LAST, id DESC", page)}


def surplus_paid(c: psycopg.Connection, person_id: int) -> float:
    return c.execute("SELECT coalesce(sum(amount), 0) AS total FROM money_surplus_spending WHERE person_pk = %s",
                     (person_id,)).fetchone()["total"]


def disclosures(c: psycopg.Connection, person_id: int) -> list[dict]:
    return c.execute(
        "SELECT id, receipt_date, period_start, period_end, url, details IS NOT NULL AS has_details,"
        " coalesce((details->>'location_withheld')::boolean, false) AS location_withheld"
        " FROM money_disclosures WHERE person_pk = %s ORDER BY period_end DESC NULLS LAST, id DESC",
        (person_id,),
    ).fetchall()


def disclosure(c: psycopg.Connection, person_id: int, disclosure_id: int) -> dict | None:
    return c.execute(
        "SELECT * FROM money_disclosures WHERE person_pk = %s AND id = %s AND details IS NOT NULL",
        (person_id, disclosure_id),
    ).fetchone()


def withhold_locations(statement: dict) -> None:
    """Treat a statement as a judge's or officer's: no property city or county."""
    details = statement["details"]
    details["location_withheld"] = True
    for prop in details.get("real_estate") or []:
        prop["city"] = prop["county"] = None


def tab_counts(c: psycopg.Connection, person_id: int) -> dict:
    return c.execute(
        "SELECT"
        " (SELECT count(*) FROM money_expenditures WHERE person_pk = %(p)s) AS spending,"
        " (SELECT count(*) FROM money_loans WHERE person_pk = %(p)s) +"
        " (SELECT count(*) FROM money_debts WHERE person_pk = %(p)s) +"
        " (SELECT count(*) FROM money_pledges WHERE person_pk = %(p)s) AS debts,"
        " (SELECT count(*) FROM money_outside WHERE person_pk = %(p)s) AS outside,"
        " (SELECT count(*) FROM money_surplus WHERE person_pk = %(p)s) +"
        " (SELECT count(*) FROM money_surplus_spending WHERE person_pk = %(p)s) AS surplus,"
        " (SELECT count(*) FROM money_disclosures WHERE person_pk = %(p)s) AS disclosures",
        {"p": person_id},
    ).fetchone()


# ---- Organizations -----------------------------------------------------------------------

def organizations(c: psycopg.Connection, *, q: str = "", source: str = "", page: int = 1) -> Page:
    # "Organizations that gave": refunds can leave a net total of zero or less.
    where, params = ["total > 0"], {}
    if q:
        where.append("(name ILIKE %(like)s OR name %% %(q)s)")
        params.update(like=f"%{q}%", q=q)
    if source:
        where.append("source_type = %(source)s")
        params["source"] = source
    w = " WHERE " + " AND ".join(where)
    total = c.execute(f"SELECT count(*) AS n FROM money_orgs{w}", params).fetchone()["n"]
    page = clamp_page(page, total)
    params.update(limit=PAGE_SIZE, offset=(page - 1) * PAGE_SIZE)
    items = c.execute(f"SELECT * FROM money_orgs{w} ORDER BY total DESC, key LIMIT %(limit)s OFFSET %(offset)s", params).fetchall()
    return Page(items, total, page)


def org_source_types(c: psycopg.Connection) -> list[str]:
    return [r["source_type"] for r in c.execute(
        "SELECT source_type, sum(total) AS t FROM money_orgs WHERE source_type IS NOT NULL AND total > 0"
        " GROUP BY 1 ORDER BY t DESC")]


def organization(c: psycopg.Connection, key: str) -> dict | None:
    org = c.execute("SELECT * FROM money_orgs WHERE key = %s", (key,)).fetchone()
    if not org:
        return None
    org["recipients"] = c.execute(
        "SELECT p.id, p.name, p.title, p.party, p.is_current, p.fec_candidate_ids, sum(m.amount) AS total, count(*) AS n,"
        " min(m.election_year) AS first_year, max(m.election_year) AS last_year"
        " FROM money_contributions m JOIN money_people p ON p.id = m.person_pk"
        " WHERE m.contributor_key = %s AND m.is_org"
        " GROUP BY p.id, p.name, p.title, p.party, p.is_current, p.fec_candidate_ids ORDER BY total DESC",
        (key,),
    ).fetchall()
    org["by_year"] = c.execute(
        "SELECT election_year AS year, sum(amount) AS total FROM money_contributions"
        " WHERE contributor_key = %s AND is_org GROUP BY 1 ORDER BY 1 DESC",
        (key,),
    ).fetchall()
    org["recent"] = c.execute(
        "SELECT m.receipt_date, m.amount, m.election_year, m.cash_or_in_kind, m.url, p.id AS person_id, p.name AS person_name"
        " FROM money_contributions m JOIN money_people p ON p.id = m.person_pk"
        " WHERE m.contributor_key = %s AND m.is_org ORDER BY m.receipt_date DESC NULLS LAST LIMIT 50",
        (key,),
    ).fetchall()
    org["upstream"] = c.execute("SELECT * FROM money_committee_sources WHERE key = %s", (key,)).fetchone()
    return org


def known_org_keys(c: psycopg.Connection, keys: list[str]) -> set[str]:
    if not keys:
        return set()
    return {r["key"] for r in c.execute("SELECT key FROM money_orgs WHERE key = ANY(%s)", (keys,))}


def sitemap_orgs(c: psycopg.Connection, limit: int = 20000) -> list[dict]:
    return c.execute("SELECT key, name FROM money_orgs WHERE total > 0 ORDER BY total DESC LIMIT %s", (limit,)).fetchall()


def search_orgs(c: psycopg.Connection, q: str, limit: int = 6) -> list[dict]:
    return c.execute(
        "SELECT key, name, source_type, total, n_people FROM money_orgs"
        " WHERE total > 0 AND name ILIKE %(like)s ORDER BY total DESC LIMIT %(limit)s",
        {"like": f"%{q}%", "limit": limit},
    ).fetchall()


def search_officials(c: psycopg.Connection, q: str, limit: int = 8) -> list[dict]:
    """Officials who never served in the Legislature, so the members table doesn't have them:
    statewide officials, justices and members of Congress. Their page is /money/{id}."""
    return c.execute(
        "SELECT p.id, p.name, p.title, p.party FROM money_people p"
        " WHERE p.member_id IS NULL AND (p.name ILIKE %(like)s OR p.name %% %(q)s)"
        " ORDER BY (p.name ILIKE %(like)s) DESC, similarity(p.name, %(q)s) DESC LIMIT %(limit)s",
        {"like": f"%{q}%", "q": q, "limit": limit},
    ).fetchall()


def legislator_money(c: psycopg.Connection, member_id: int) -> dict | None:
    """State (PDC) and federal (FEC) totals stay separate: the site says they're never added together."""
    return c.execute(
        "SELECT id, total_raised AS state_raised, fed_raised, n_campaigns, fed_cycles FROM money_people WHERE member_id = %s",
        (member_id,),
    ).fetchone()
