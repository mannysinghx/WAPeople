"""WA People website (FastAPI + server-rendered Jinja2 templates)."""
from __future__ import annotations

import json
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import contact as C
from ..courts.sync import CATEGORIES
from ..db import conn
from ..districts import current_biennium_with_roster, delegations, map_geojson, party_mix
from ..legislature.parse import (
    FINAL_VOTE_KINDS, TYPE_NAMES, VOTE_KINDS, base_bill_id, biennium_for_year, session_law_text,
)
from ..legislature.stages import STAGES, next_session_start
from ..legislature.sync import current_biennium
from . import queries as Q
from .security import SecurityMiddleware

HERE = Path(__file__).parent
app = FastAPI(title="WA People", docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(GZipMiddleware, minimum_size=1024)
app.add_middleware(SecurityMiddleware)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")


# ---- Template helpers -----------------------------------------------------------

def fmt_date(d, long: bool = False) -> str:
    if not d:
        return ""
    if isinstance(d, datetime):
        d = d.date()
    return d.strftime("%B %-d, %Y" if long else "%b %-d, %Y")


def biennium_label(b: str | None) -> str:
    return b.replace("-", "–") if b else ""


def bill_url(biennium: str, bill_id: str) -> str:
    return f"/bills/{biennium}/{bill_id.replace(' ', '-')}"


_AMEND_RE = re.compile(r"^(\d+)\s+(.+?)\s+Pg\s+(\d+)\s+Ln\s+(\d+)$")


def motion_label(motion: str | None, kind: str | None = None) -> str:
    if not motion or not motion.strip():
        return VOTE_KINDS.get(kind or "", "Recorded vote")
    m = _AMEND_RE.match(" ".join(motion.split()))
    if m:
        return f"Amendment {m[1]} by {m[2]} (page {m[3]}, line {m[4]})"
    return motion


PARTY_NAMES = {"D": "Democrat", "R": "Republican", "I": "Independent", "?": "Unknown"}
STAGE_TONE = {
    "prefiled": "proposed", "introduced": "proposed", "committee": "proposed",
    "floor": "ongoing", "passed_origin": "ongoing", "failed_vote": "warn", "passed_legislature": "ongoing",
    "law": "law", "law_partial_veto": "law", "filed": "law", "adopted": "law", "confirmed": "law",
    "vetoed": "bad", "not_confirmed": "muted", "did_not_pass": "muted",
}


def leg_url(bill: dict) -> str:
    start = int(bill["biennium"][:4])
    params = {"BillNumber": bill["bill_number"], "Year": start,
              "Initiative": "true" if bill["type_code"] == "I" else "false"}
    return "https://app.leg.wa.gov/billsummary?" + urlencode(params)


def qs(request: Request, **overrides) -> str:
    """Current query string with some parameters replaced (None removes one)."""
    params = dict(request.query_params)
    for k, v in overrides.items():
        if v is None or v == "":
            params.pop(k, None)
        else:
            params[k] = v
    return "?" + urlencode(params) if params else "?"


templates.env.filters["date"] = fmt_date
templates.env.filters["biennium"] = biennium_label
templates.env.filters["motion"] = motion_label
templates.env.filters["first_bullet"] = lambda s: (s or "").split("\n", 1)[0]
templates.env.filters["bullets"] = lambda s: [b for b in (s or "").split("\n") if b.strip()]
templates.env.globals.update(
    bill_url=bill_url, qs=qs, leg_url=leg_url, PARTY_NAMES=PARTY_NAMES, STAGE_TONE=STAGE_TONE,
    STAGES=STAGES, CATEGORIES=CATEGORIES, TYPE_NAMES=TYPE_NAMES, TYPE_FILTERS=Q.TYPE_FILTERS,
    STAGE_FILTERS=Q.STAGE_FILTERS, BUCKETS=Q.BUCKETS, session_law_text=session_law_text,
    base_id=base_bill_id,
)


def render(request: Request, name: str, status_code: int = 200, **ctx) -> HTMLResponse:
    with conn() as c:
        ctx.setdefault("updated", Q.last_updates(c))
    ctx["current_biennium"] = current_biennium()
    ctx["path"] = request.url.path
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


def page_param(page: int) -> int:
    return max(1, min(page, 10_000))


# ---- Pages ------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    cur = current_biennium()
    with conn() as c:
        bienniums = Q.bienniums(c)
        counts = Q.home_counts(c, cur)
        cur_row = next((b for b in bienniums if b["id"] == cur), None)
        upcoming = [b for b in bienniums if b["id"] > cur]
        recent_laws = Q.search_bills(c, stage="enacted", sort="recent", page_size=6).items
        recent_votes = Q.list_roll_calls(c, final_only=True, bills_only=True).items[:6]
        cases = Q.search_cases(c, page_size=4).items
        moving = Q.search_bills(c, bucket="ongoing", sort="recent", page_size=6).items
        proposed = Q.search_bills(c, bucket="proposed", sort="introduced", page_size=6).items
    return render(
        request, "index.html", counts=counts, cur=cur_row, upcoming=upcoming,
        recent_laws=recent_laws, recent_votes=recent_votes, cases=cases, moving=moving,
        proposed=proposed, next_session=next_session_start(),
    )


@app.get("/bills", response_class=HTMLResponse)
def bills(
    request: Request, q: str = "", bucket: str = "", biennium: str = "", stage: str = "",
    chamber: str = "", type: str = "B", sponsor: int | None = None, sort: str = "",
    page: int = 1,
):
    q = q.strip()[:200]
    sort = sort or ("relevance" if q else "recent")
    with conn() as c:
        bienniums = Q.bienniums(c)
        result = Q.search_bills(
            c, q=q, bucket=bucket, biennium=biennium, stage=stage, chamber=chamber,
            type_code=type, sponsor=sponsor, sort=sort, page=page_param(page),
        )
        sponsor_row = Q.get_member(c, sponsor) if sponsor else None
        cur = next((b for b in bienniums if b["id"] == current_biennium()), None)
    return render(
        request, "bills.html", result=result, bienniums=bienniums, cur=cur, sponsor_row=sponsor_row,
        f=dict(q=q, bucket=bucket, biennium=biennium, stage=stage, chamber=chamber, type=type,
               sponsor=sponsor, sort=sort),
        next_session=next_session_start(),
    )


def progress_steps(bill: dict, actions: list[dict], rolls: list[dict]) -> list[dict]:
    """Milestones for the bill-page tracker. Empty for one-chamber items."""
    if bill["type_code"] not in ("B", "JR", "JM", "CR"):
        return []
    origin = bill["original_agency"] if bill["original_agency"] in ("House", "Senate") else "House"
    other = "Senate" if origin == "House" else "House"
    flags = {"House": bill["passed_house"], "Senate": bill["passed_senate"]}
    lines = [(a["action_date"], (a["description"] or "").lower()) for a in actions]

    def first(pred):
        return next((d for d, line in lines if pred(line)), None)

    def passed_on(chamber):
        return next((r["vote_date"] for r in rolls if r["agency"] == chamber
                     and r["vote_kind"] == "final_passage" and r["result"] in ("passed", None)), None)

    out_of_committee = flags[origin] or bill["stage"] in ("floor", "failed_vote") or any(
        ("rules" in line and ("passed to rules" in line or "referred to rules" in line))
        or "placed on second reading" in line for _, line in lines
    )
    stage = bill["stage"]
    law = stage in ("law", "law_partial_veto")
    steps = [
        {"label": "Introduced", "done": bool(bill["introduced_date"]), "when": bill["introduced_date"]},
        {"label": "Out of committee", "done": out_of_committee,
         "when": first(lambda l: "passed to rules" in l or "referred to rules" in l or "placed on second reading" in l)},
        {"label": f"Passed {origin}", "done": flags[origin], "when": passed_on(origin)},
        {"label": f"Passed {other}", "done": flags[other], "when": passed_on(other)},
    ]
    if bill["type_code"] == "B":
        gov_when = first(lambda l: l.startswith("governor"))
        gov_label = {"vetoed": "Vetoed", "law_partial_veto": "Partly vetoed"}.get(stage, "Governor")
        steps.append({"label": gov_label, "done": law or stage == "vetoed", "when": gov_when,
                      "stop": stage == "vetoed"})
        steps.append({"label": "Law", "done": law, "when": bill["status_date"] if law else None})
    else:
        steps.append({"label": "Sent to voters" if bill["type_code"] == "JR" else "Filed",
                      "done": stage == "filed", "when": bill["status_date"] if stage == "filed" else None})
    if stage in ("did_not_pass", "failed_vote"):
        reached = [s for s in steps if s["done"]]
        idx = len(reached)
        if idx < len(steps):
            steps[idx]["stop"] = True
    return steps


@app.get("/bills/{biennium}/{slug}", response_class=HTMLResponse)
def bill_detail(request: Request, biennium: str, slug: str):
    m = re.fullmatch(r"([A-Za-z]+)-(\d+)", slug)
    if not m or not re.fullmatch(r"\d{4}-\d{2}", biennium):
        raise HTTPException(404)
    with conn() as c:
        bill = Q.get_bill(c, biennium, m[1].upper(), int(m[2]))
        if not bill:
            raise HTTPException(404)
        sponsors = Q.bill_sponsors(c, bill)
        actions = Q.bill_actions(c, bill["id"])
        rolls = Q.bill_roll_calls(c, bill)
        cases = Q.bill_cases(c, bill["id"])
        others = Q.other_sessions(c, bill)
    final_rolls = [r for r in rolls if r["vote_kind"] in FINAL_VOTE_KINDS]
    other_rolls = [r for r in rolls if r not in final_rolls]
    return render(
        request, "bill.html", bill=bill, sponsors=sponsors, actions=actions,
        final_rolls=final_rolls, other_rolls=other_rolls, cases=cases, others=others,
        steps=progress_steps(bill, actions, rolls),
    )


@app.get("/legislators", response_class=HTMLResponse)
def legislators(request: Request, biennium: str = "", chamber: str = "", party: str = "",
                district: str = "", q: str = ""):
    with conn() as c:
        bienniums = Q.bienniums(c)
        biennium = biennium or (bienniums[0]["id"] if bienniums else current_biennium())
        if biennium > current_biennium():
            biennium = current_biennium()
        members = Q.list_members(c, biennium=biennium, chamber=chamber, party=party,
                                 district=district.strip(), q=q.strip()[:100])
    return render(request, "legislators.html", members=members, bienniums=bienniums,
                  f=dict(biennium=biennium, chamber=chamber, party=party, district=district, q=q))


@app.get("/legislators/{member_id}", response_class=HTMLResponse)
def legislator(request: Request, member_id: int, tab: str = "votes", biennium: str = "",
               vote: str = "", final: str = "1", q: str = "", page: int = 1):
    with conn() as c:
        member = Q.get_member(c, member_id)
        if not member:
            raise HTTPException(404)
        stats = Q.member_stats(c, member_id)
        if tab == "bills":
            listing = Q.search_bills(c, sponsor=member_id, biennium=biennium, q=q.strip()[:200],
                                     type_code="B", sort="recent", page=page_param(page))
        else:
            tab = "votes"
            listing = Q.member_votes(c, member_id, biennium=biennium, vote=vote,
                                     final_only=final == "1", q=q.strip()[:200], page=page_param(page))
        bienniums = [t["biennium"] for t in member["terms"]]
    return render(request, "legislator.html", m=member, stats=stats, tab=tab, listing=listing,
                  bienniums=bienniums, f=dict(biennium=biennium, vote=vote, final=final, q=q))


@app.get("/votes", response_class=HTMLResponse)
def votes(request: Request, biennium: str = "", chamber: str = "", final: str = "1",
          close: str = "", q: str = "", page: int = 1):
    with conn() as c:
        bienniums = Q.bienniums(c)
        result = Q.list_roll_calls(c, biennium=biennium, chamber=chamber, final_only=final == "1",
                                   close=close == "1", q=q.strip()[:200], page=page_param(page))
    return render(request, "votes.html", result=result, bienniums=bienniums,
                  f=dict(biennium=biennium, chamber=chamber, final=final, close=close, q=q))


@app.get("/votes/{rid}", response_class=HTMLResponse)
def vote_detail(request: Request, rid: int):
    with conn() as c:
        rc = Q.get_roll_call(c, rid)
    if not rc:
        raise HTTPException(404)
    return render(request, "vote.html", rc=rc)


@app.get("/court", response_class=HTMLResponse)
def court(request: Request, q: str = "", category: str = "", year_from: str = "", year_to: str = "",
          linked: str = "", sort: str = "", page: int = 1):
    q = q.strip()[:200]
    sort = sort or ("relevance" if q else "newest")
    yf = int(year_from) if year_from.isdigit() else None
    yt = int(year_to) if year_to.isdigit() else None
    category = category if category in CATEGORIES else ""
    with conn() as c:
        result = Q.search_cases(c, q=q, category=category, year_from=yf, year_to=yt,
                                linked=linked == "1", sort=sort, page=page_param(page))
        cat_counts = Q.case_category_counts(c)
    return render(request, "court.html", result=result, cat_counts=cat_counts,
                  f=dict(q=q, category=category, year_from=year_from, year_to=year_to,
                         linked=linked, sort=sort))


@app.get("/court/{case_id}", response_class=HTMLResponse)
def case_detail(request: Request, case_id: int):
    with conn() as c:
        cc = Q.get_case(c, case_id)
    if not cc:
        raise HTTPException(404)
    linked_texts = {b["mention"] for b in cc["bills"]}
    unresolved = [m for m in cc["bill_mentions"] if not m.get("bill_pk") and m["kind"] != "session_law"]
    for m in unresolved:
        if m.get("year"):
            m["biennium"] = biennium_for_year(m["year"])
    return render(request, "case.html", cc=cc, unresolved=unresolved, linked_texts=linked_texts)


@app.get("/search", response_class=HTMLResponse)
def search(request: Request, q: str = ""):
    q = q.strip()[:200]
    if not q:
        return RedirectResponse("/")
    ref = Q.parse_bill_ref(q)
    with conn() as c:
        bills_page = Q.search_bills(c, q=q, type_code="all" if ref else "B",
                                    sort="number" if ref else "relevance", page_size=10)
        if ref and bills_page.total == 1:
            b = bills_page.items[0]
            return RedirectResponse(bill_url(b["biennium"], b["bill_id"]), status_code=302)
        members = [] if ref else Q.search_members(c, q)
        cases = Q.search_cases(c, q=q, sort="relevance", page_size=6)
    return render(request, "search.html", q=q, bills=bills_page, members=members, cases=cases)


@app.get("/about", response_class=HTMLResponse)
def about(request: Request):
    with conn() as c:
        bienniums = Q.bienniums(c)
        counts = Q.home_counts(c, current_biennium())
        runs = c.execute(
            "SELECT job, scope, started_at, finished_at, status FROM sync_runs ORDER BY id DESC LIMIT 12"
        ).fetchall()
    return render(request, "about.html", bienniums=bienniums, counts=counts, runs=runs)


# ---- District map -------------------------------------------------------------------

_map_cache: dict = {"at": 0.0, "body": None}


@app.get("/api/districts.geojson")
def districts_geojson():
    """District boundaries with each district's current legislators (cached for 10 minutes)."""
    if _map_cache["body"] is None or time.time() - _map_cache["at"] > 600:
        with conn() as c:
            _map_cache["body"] = json.dumps(map_geojson(c), separators=(",", ":"), default=str)
        _map_cache["at"] = time.time()
    return Response(_map_cache["body"], media_type="application/geo+json",
                    headers={"Cache-Control": "public, max-age=900"})


@app.get("/map", response_class=HTMLResponse)
def district_map(request: Request):
    with conn() as c:
        biennium = current_biennium_with_roster(c)
        dels = delegations(c, biennium) if biennium else {}
        row = c.execute("SELECT source FROM legislative_districts LIMIT 1").fetchone()
    districts = []
    for d in range(1, 50):
        dl = dels.get(d, {"senators": [], "representatives": []})
        districts.append({"district": d, "mix": party_mix(dl), **dl})
    return render(request, "map.html", districts=districts, biennium=biennium,
                  source=row["source"] if row else None)


# ---- Contact form -------------------------------------------------------------------

def _deliver_pending() -> None:
    with conn() as c:
        C.send_pending(c, limit=5)


@app.get("/contact", response_class=HTMLResponse)
def contact_form(request: Request, topic: str = "", page: str = ""):
    f = {"topic": topic if topic in C.TOPICS else "", "page": page[:300], "name": "", "email": "", "message": ""}
    return render(request, "contact.html", f=f, errors={}, token=C.form_token(), TOPICS=C.TOPICS)


@app.post("/contact", response_class=HTMLResponse)
async def contact_submit(request: Request, background: BackgroundTasks):
    form = await request.form()
    data = {k: str(form.get(k) or "") for k in ("topic", "page", "name", "email", "message", "website", "token")}
    if data["website"].strip():
        # Honeypot field (hidden from people): only bots fill it. Pretend it worked.
        return RedirectResponse("/contact/sent", status_code=303)
    clean, errors = C.validate(data)
    if not C.token_ok(data["token"]):
        errors["form"] = ("This form expired or was sent too quickly. Please check your message and "
                          "send it again.")
    chash = C.client_hash(request.client.host if request.client else "")
    with conn() as c:
        if not errors and C.rate_limited(c, chash):
            errors["form"] = "Too many messages from your connection recently. Please try again in an hour."
        if not errors:
            C.save(c, clean, chash)
    if errors:
        return render(request, "contact.html", status_code=400, f=data, errors=errors,
                      token=C.form_token(), TOPICS=C.TOPICS)
    background.add_task(_deliver_pending)
    return RedirectResponse("/contact/sent", status_code=303)


@app.get("/contact/sent", response_class=HTMLResponse)
def contact_sent(request: Request):
    return render(request, "contact_sent.html")


# ---- Legal and security pages --------------------------------------------------------

LEGAL_UPDATED = date(2026, 10, 11)


@app.get("/privacy", response_class=HTMLResponse)
def privacy(request: Request):
    return render(request, "privacy.html", updated_on=LEGAL_UPDATED, retention_days=C.RETENTION_DAYS,
                  hash_days=C.CLIENT_HASH_DAYS)


@app.get("/terms", response_class=HTMLResponse)
def terms(request: Request):
    return render(request, "terms.html", updated_on=LEGAL_UPDATED)


@app.get("/disclaimer", response_class=HTMLResponse)
def disclaimer(request: Request):
    return render(request, "disclaimer.html", updated_on=LEGAL_UPDATED)


@app.get("/robots.txt", response_class=PlainTextResponse)
def robots():
    return ("User-agent: *\n"
            "Allow: /\n"
            "Disallow: /contact\n"
            "Disallow: /search\n")


@app.get("/.well-known/security.txt", response_class=PlainTextResponse)
def security_txt(request: Request):
    base = str(request.base_url).rstrip("/")
    expires = (datetime.now().astimezone() + timedelta(days=180)).strftime("%Y-%m-%dT00:00:00.000Z")
    return (f"Contact: {base}/contact?topic=security\n"
            f"Expires: {expires}\n"
            "Preferred-Languages: en\n"
            f"Policy: {base}/privacy#security\n"
            f"Canonical: {base}/.well-known/security.txt\n")


@app.get("/healthz", response_class=PlainTextResponse)
def healthz():
    with conn() as c:
        c.execute("SELECT 1")
    return "ok"


@app.exception_handler(404)
async def not_found(request: Request, exc):
    return templates.TemplateResponse(
        request, "404.html", {"current_biennium": current_biennium(), "path": request.url.path,
                              "updated": {}}, status_code=404,
    )
