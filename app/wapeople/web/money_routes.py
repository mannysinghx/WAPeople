"""The money pages: who funds each elected official, and where that money goes."""
from __future__ import annotations

from typing import Callable

import psycopg
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from ..db import conn
from ..money.clean import key_to_slug, person_shaped, slug_to_key
from . import fec_queries as F
from . import money_queries as M

FEC_VIEWS = {
    "overview": "Overview",
    "contributions": "Contributions",
    "spending": "Spending",
    "outside": "Outside money",
}
VIEWS = {
    "overview": "Overview",
    "contributions": "Contributions",
    "spending": "Spending",
    "debts": "Loans & debts",
    "outside": "Outside money",
    "surplus": "Surplus funds",
    "disclosures": "Personal finances",
}
# Itemized views name private donors: keep them out of search engines (see also robots.txt).
# Spending counts because refunds name the donors they went back to.
NOINDEX_VIEWS = {"contributions", "spending", "debts", "outside", "surplus", "disclosures"}
FEC_NOINDEX_VIEWS = {"contributions", "spending", "outside"}


def _page(page: int) -> int:
    return max(1, min(page, 10_000))


def _int(value: str) -> int | None:
    """A filter value as a number, or None. str.isdigit alone also accepts '²', which int() rejects,
    and nine digits always fit the int columns these filters compare against. No year, cycle or
    statement is 0, so 0 means no filter, not a filter that matches nothing."""
    return (int(value) or None) if value.isascii() and value.isdigit() and len(value) <= 9 else None


def _amount(value: str) -> float | None:
    try:
        v = float(value)
    except ValueError:
        return None
    return v if 0 < v < 1e9 else None


def _federal_page(c: psycopg.Connection, person: dict, view: str, q: str, source: str, state: str, min_amount: str,
                  category: str, sort: str, cycle: str, memo: str, page: int) -> dict:
    view = view if view in FEC_VIEWS else "overview"
    pid = person["id"]
    ctx: dict = {"counts": F.tab_counts(c, pid), "cycles": F.cycles(c, pid),
                 "other_committees": F.other_committees(c, pid)}
    ctx["cycle_years"] = sorted({k["cycle"] for k in ctx["cycles"]}, reverse=True)
    y = _int(cycle)
    if view == "contributions":
        amount = _amount(min_amount)
        ctx["listing"] = F.contributions(c, pid, cycle=y, source=source, state=state, q=q, min_amount=amount,
                                         memo=memo == "1", sort=sort, page=page)
        ctx["sources"] = F.source_types(c, pid)
        narrowed = source or state or q or amount
        ctx["individuals"] = [] if narrowed else F.top_individuals(c, pid, cycle=y)
    elif view == "spending":
        ctx["listing"] = F.spending(c, pid, cycle=y, category=category, q=q, sort=sort, page=page)
    elif view == "outside":
        ctx.update(F.outside(c, pid, page=page))
    return dict(p=person, view=view, VIEWS=FEC_VIEWS, noindex=view in FEC_NOINDEX_VIEWS,
                f=dict(cycle=str(y) if y else "", source=source, state=state, q=q, min=min_amount, category=category,
                       sort=sort, memo=memo), **ctx)


def _statement(c: psycopg.Connection, person: dict, statements: list[dict], wanted: str) -> dict | None:
    """The statement asked for, or the newest one with details when it isn't one of this person's."""
    pid, sid = person["id"], _int(wanted)
    found = M.disclosure(c, pid, sid) if sid else None
    if not found:
        newest = next((s["id"] for s in statements if s["has_details"]), None)
        found = M.disclosure(c, pid, newest) if newest else None
    # The flag is per statement, but a filing from before someone took the bench shouldn't show where
    # the property described in their later, flagged filings is.
    if found and (person["kind"] == "justice" or any(s["location_withheld"] for s in statements)):
        M.withhold_locations(found)
    return found


def _state_page(c: psycopg.Connection, person: dict, view: str, y: int | None, q: str, source: str, state: str,
                min_amount: str, donor: str, category: str, sort: str, statement: str, page: int) -> dict:
    pid = person["id"]
    ctx: dict = {
        "campaigns": M.campaigns(c, pid),
        "years": M.years(c, pid),
        "counts": M.tab_counts(c, pid),
        "surplus_paid": M.surplus_paid(c, pid),
    }
    if view == "contributions":
        ctx["listing"] = M.contributions(c, pid, year=y, source=source, state=state, q=q,
                                         min_amount=_amount(min_amount), donor=donor[:200], sort=sort, page=page)
        # The summary table only makes sense when the list below isn't narrowed further.
        narrowed = source or state or q or _amount(min_amount) or donor
        ctx["individuals"] = [] if narrowed else M.top_individuals(c, pid, year=y)
    elif view == "spending":
        ctx["listing"] = M.spending(c, pid, year=y, category=category, q=q, sort=sort, page=page)
    elif view == "debts":
        ctx.update(M.debts_and_loans(c, pid, page=page))
    elif view == "outside":
        ctx.update(M.outside(c, pid))
    elif view == "surplus":
        ctx.update(M.surplus(c, pid, page=page))
    elif view == "disclosures":
        ctx["statements"] = M.disclosures(c, pid)
        ctx["statement"] = _statement(c, person, ctx["statements"], statement)
    return dict(p=person, view=view, VIEWS=VIEWS, noindex=view in NOINDEX_VIEWS,
                f=dict(year=str(y) if y else "", source=source, state=state, q=q, min=min_amount, donor=donor,
                       category=category, sort=sort), **ctx)


def build_router(render: Callable) -> APIRouter:
    router = APIRouter()

    # render() takes its own pooled connection for the footer dates, so every route here calls it
    # after its `with conn()` block. Holding one connection while waiting for another deadlocks the
    # pool as soon as ten requests overlap.

    @router.get("/money", response_class=HTMLResponse)
    def money_index(request: Request, group: str = "all", party: str = "", q: str = "", former: str = "",
                    sort: str = "raised"):
        group = group if group in M.GROUPS else "all"
        q = q.strip()[:100]
        with conn() as c:
            stats = M.overview(c)
            people = M.list_people(c, group=group, party=party, q=q, former=former == "1", sort=sort)
        return render(request, "money_index.html", stats=stats, people=people, GROUPS=M.GROUPS,
                      f=dict(group=group, party=party, q=q, former=former, sort=sort))

    @router.get("/money/organizations", response_class=HTMLResponse)
    def money_organizations(request: Request, q: str = "", source: str = "", page: int = 1):
        q = q.strip()[:100]
        with conn() as c:
            result = M.organizations(c, q=q, source=source, page=_page(page))
            types = M.org_source_types(c)
        return render(request, "money_orgs.html", result=result, types=types, f=dict(q=q, source=source))

    @router.get("/money/organizations/{slug}", response_class=HTMLResponse)
    def money_organization(request: Request, slug: str):
        with conn() as c:
            org = M.organization(c, slug_to_key(slug))
            if not org:
                raise HTTPException(404)
            upstream = org.get("upstream")
            known = M.known_org_keys(c, [f["key"] for f in upstream["funders"]]) if upstream else set()
        # A filer can code a person as a business. Such a name may still be reached by a link, but
        # it must not be indexed: the site promises not to list individual donors in search engines.
        noindex = person_shaped(org["name"])
        response = render(request, "money_org.html", org=org, known=known, noindex=noindex,
                          canonical_path=f"/money/organizations/{key_to_slug(org['key'])}")
        if noindex:
            response.headers["X-Robots-Tag"] = "noindex"
        return response

    @router.get("/money/{person_id}", response_class=HTMLResponse)
    def money_person(request: Request, person_id: int, view: str = "overview", year: str = "", source: str = "",
                     state: str = "", q: str = "", min: str = "", donor: str = "", category: str = "",
                     sort: str = "", statement: str = "", cycle: str = "", memo: str = "", records: str = "",
                     page: int = 1):
        view = view if view in VIEWS else "overview"
        q = q.strip()[:100]
        with conn() as c:
            person = M.get_person(c, person_id)
            if not person:
                raise HTTPException(404)
            if person["fec_candidate_ids"] and records != "state":
                template = "money_person_fec.html"
                ctx = _federal_page(c, person, view, q, source, state, min, category, sort, cycle, memo, _page(page))
            else:
                template = "money_person.html"
                ctx = _state_page(c, person, view, _int(year), q, source, state, min, donor, category, sort,
                                  statement, _page(page))
        return render(request, template, canonical_path=f"/money/{person_id}", **ctx)

    return router
