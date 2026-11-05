"""The money pages: who funds each elected official, and where that money goes."""
from __future__ import annotations

from typing import Callable

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from ..db import conn
from ..money.clean import slug_to_key
from . import money_queries as M

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
NOINDEX_VIEWS = {"contributions", "debts", "outside", "surplus", "disclosures"}


def _page(page: int) -> int:
    return max(1, min(page, 10_000))


def _int(value: str) -> int | None:
    return int(value) if value.isdigit() else None


def _amount(value: str) -> float | None:
    try:
        v = float(value)
    except ValueError:
        return None
    return v if 0 < v < 1e9 else None


def build_router(render: Callable) -> APIRouter:
    router = APIRouter()

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
        return render(request, "money_org.html", org=org, known=known)

    @router.get("/money/{person_id:int}", response_class=HTMLResponse)
    def money_person(request: Request, person_id: int, view: str = "overview", year: str = "", source: str = "",
                     state: str = "", q: str = "", min: str = "", donor: str = "", category: str = "",
                     sort: str = "", statement: str = "", page: int = 1):
        view = view if view in VIEWS else "overview"
        y = _int(year)
        q = q.strip()[:100]
        ctx: dict = {}
        with conn() as c:
            person = M.get_person(c, person_id)
            if not person:
                raise HTTPException(404)
            ctx["campaigns"] = M.campaigns(c, person_id)
            ctx["years"] = M.years(c, person_id)
            ctx["counts"] = M.tab_counts(c, person_id)
            ctx["surplus_accounts"] = c.execute(
                "SELECT balance, raised, spent FROM money_surplus WHERE person_pk = %s", (person_id,)).fetchall()
            if view == "contributions":
                ctx["listing"] = M.contributions(c, person_id, year=y, source=source, state=state, q=q,
                                                 min_amount=_amount(min), donor=donor[:200], sort=sort,
                                                 page=_page(page))
                # The summary table only makes sense when the list below isn't narrowed further.
                narrowed = source or state or q or _amount(min) or donor
                ctx["individuals"] = [] if narrowed else M.top_individuals(c, person_id, year=y)
            elif view == "spending":
                ctx["listing"] = M.spending(c, person_id, year=y, category=category, q=q, sort=sort, page=_page(page))
            elif view == "debts":
                ctx.update(M.debts_and_loans(c, person_id, page=_page(page)))
            elif view == "outside":
                ctx.update(M.outside(c, person_id))
            elif view == "surplus":
                ctx.update(M.surplus(c, person_id, page=_page(page)))
            elif view == "disclosures":
                ctx["statements"] = M.disclosures(c, person_id)
                sid = _int(statement)
                if sid is None:
                    sid = next((s["id"] for s in ctx["statements"] if s["has_details"]), None)
                ctx["statement"] = M.disclosure(c, person_id, sid) if sid else None
        return render(request, "money_person.html", p=person, view=view, VIEWS=VIEWS, noindex=view in NOINDEX_VIEWS,
                      f=dict(year=year, source=source, state=state, q=q, min=min, donor=donor, category=category,
                             sort=sort), **ctx)

    return router
