"""Collect Washington Supreme Court decisions about legislation and public policy.

Selection is rule-based and transparent: a decision is included when its text matches
at least one of the CATEGORIES queries below (run against CourtListener's full-text
search, restricted to the Washington Supreme Court). Each case keeps the matching
excerpt so readers can see why it is listed.
"""
from __future__ import annotations

import html
import logging
import re
import time
from datetime import date, timedelta

import psycopg
from psycopg.types.json import Jsonb

from ..config import COURT_CASES_SINCE
from ..legislature.parse import biennium_for_year
from .client import CourtListener, PdfFetcher
from .refs import extract_mentions

log = logging.getLogger(__name__)

CATEGORIES: dict[str, dict[str, str]] = {
    "legislation": {
        "label": "Cites a specific bill",
        "description": "The opinion discusses a House or Senate bill by name.",
        "query": '"House Bill" OR "Senate Bill"',
    },
    "ballot_measure": {
        "label": "Ballot initiative or referendum",
        "description": "The case involves a statewide initiative or referendum.",
        "query": (
            '"Initiative Measure" OR "Initiative No." OR "initiative to the people" OR '
            '"initiative to the legislature" OR "Referendum Measure" OR "Referendum No." OR '
            '"Referendum Bill"'
        ),
    },
    "constitutionality": {
        "label": "Constitutionality of a law",
        "description": "The court considers whether a statute or ordinance is constitutional.",
        "query": (
            '(unconstitutional OR "facially invalid" OR "facial challenge") AND '
            '(statute OR ordinance OR "Laws of" OR legislature)'
        ),
    },
    "legislative_power": {
        "label": "Legislative power & process",
        "description": "Single-subject and title rules, separation of powers, or veto power.",
        "query": (
            '"single subject" OR "subject-in-title" OR "article II, section 19" OR '
            '"article II, section 37" OR "separation of powers" OR "veto power"'
        ),
    },
}

# Not about public policy: lawyer/judge discipline.
EXCLUDED_NAME_RE = re.compile(r"disciplinary proceeding|discipline of|judicial conduct", re.I)


def normalize_docket(docket: str) -> str:
    return re.sub(r"\D", "", docket or "")


def clean_snippet(snippet: str) -> str:
    """Escape everything except the <mark> highlights CourtListener adds."""
    s = html.escape(html.unescape(" ".join((snippet or "").split())))
    return s.replace("&lt;mark&gt;", "<mark>").replace("&lt;/mark&gt;", "</mark>")


def case_from_result(r: dict, category: str) -> dict | None:
    name = (r.get("caseName") or r.get("caseNameFull") or "").strip()
    if not name or EXCLUDED_NAME_RE.search(name):
        return None
    docket = (r.get("docketNumber") or "").strip()
    filed = r.get("dateFiled")
    digits = normalize_docket(docket)
    key = f"{digits}:{filed}" if digits and filed else f"cl:{r.get('cluster_id')}"
    ops = r.get("opinions") or []
    snippet = next((o.get("snippet") for o in ops if "<mark>" in (o.get("snippet") or "")), None)
    snippet = snippet or (ops[0].get("snippet") if ops else "")
    pdf = next((o.get("download_url") for o in ops if (o.get("download_url") or "").endswith(".pdf")), None)
    if not pdf:
        lp = next((o.get("local_path") for o in ops if o.get("local_path")), None)
        pdf = f"https://storage.courtlistener.com/{lp}" if lp else None
    return {
        "case_key": key,
        "cluster_ids": [r["cluster_id"]] if r.get("cluster_id") else [],
        "opinion_ids": [o["id"] for o in ops if o.get("id")],
        "case_name": name,
        "case_name_full": (r.get("caseNameFull") or "").strip() or None,
        "date_filed": filed,
        "docket_number": docket or None,
        "citations": [c for c in (r.get("citation") or []) if c],
        "cite_count": int(r.get("citeCount") or 0),
        "status": r.get("status") or None,
        "judges": (r.get("judge") or "").strip() or None,
        "courtlistener_url": f"https://www.courtlistener.com{r['absolute_url']}" if r.get("absolute_url") else None,
        "pdf_url": pdf,
        "categories": [category],
        "snippets": {category: clean_snippet(snippet)} if snippet else {},
    }


def merge_case(a: dict, b: dict) -> dict:
    """Combine two hits for the same decision (CourtListener sometimes has duplicates)."""
    out = dict(a)
    for field in ("cluster_ids", "opinion_ids", "citations", "categories"):
        out[field] = list(dict.fromkeys(a[field] + b[field]))
    out["snippets"] = {**b["snippets"], **a["snippets"]}
    out["cite_count"] = max(a["cite_count"], b["cite_count"])
    for field in ("pdf_url", "courtlistener_url", "case_name_full", "judges", "docket_number", "status"):
        out[field] = a.get(field) or b.get(field)
    return out


UPSERT_CASE_SQL = """
INSERT INTO court_cases (case_key, cluster_ids, opinion_ids, case_name, case_name_full, date_filed,
    docket_number, citations, cite_count, status, judges, courtlistener_url, pdf_url, categories,
    snippets, snippet_text)
VALUES (%(case_key)s, %(cluster_ids)s, %(opinion_ids)s, %(case_name)s, %(case_name_full)s, %(date_filed)s,
    %(docket_number)s, %(citations)s, %(cite_count)s, %(status)s, %(judges)s, %(courtlistener_url)s,
    %(pdf_url)s, %(categories)s, %(snippets)s, %(snippet_text)s)
ON CONFLICT (case_key) DO UPDATE SET
    cluster_ids = ARRAY(SELECT DISTINCT unnest(court_cases.cluster_ids || EXCLUDED.cluster_ids)),
    opinion_ids = ARRAY(SELECT DISTINCT unnest(court_cases.opinion_ids || EXCLUDED.opinion_ids)),
    citations   = ARRAY(SELECT DISTINCT unnest(court_cases.citations || EXCLUDED.citations)),
    categories  = ARRAY(SELECT DISTINCT unnest(court_cases.categories || EXCLUDED.categories)),
    snippets    = court_cases.snippets || EXCLUDED.snippets,
    snippet_text = (SELECT string_agg(regexp_replace(v, '</?mark>', '', 'g'), ' ')
                    FROM jsonb_each_text(court_cases.snippets || EXCLUDED.snippets) AS s(k, v)),
    cite_count  = GREATEST(court_cases.cite_count, EXCLUDED.cite_count),
    pdf_url     = COALESCE(court_cases.pdf_url, EXCLUDED.pdf_url),
    courtlistener_url = COALESCE(court_cases.courtlistener_url, EXCLUDED.courtlistener_url),
    case_name_full = COALESCE(court_cases.case_name_full, EXCLUDED.case_name_full),
    judges      = COALESCE(court_cases.judges, EXCLUDED.judges),
    status      = COALESCE(EXCLUDED.status, court_cases.status),
    updated_at  = now()
"""


class CourtSync:
    def __init__(self, conn: psycopg.Connection, cl: CourtListener | None = None):
        self.conn = conn
        self.cl = cl or CourtListener()

    def collect(self, filed_after: str | None = None) -> dict:
        """Run every category query and upsert the matching decisions."""
        filed_after = filed_after or COURT_CASES_SINCE
        stats: dict = {"filed_after": filed_after}
        keys: set[str] = set()
        for cat, spec in CATEGORIES.items():
            # Saved per category (the upsert merges categories), so an interrupted
            # run keeps what it collected.
            cases: dict[str, dict] = {}
            n = 0
            t0 = time.time()
            for i, r in enumerate(self.cl.search(spec["query"], filed_after=filed_after), 1):
                if i % 200 == 0:
                    log.info("court category %s: %d results read (%.0fs)", cat, i, time.time() - t0)
                c = case_from_result(r, cat)
                if not c:
                    continue
                n += 1
                cases[c["case_key"]] = merge_case(cases[c["case_key"]], c) if c["case_key"] in cases else c
            self._upsert(cases.values())
            keys.update(cases)
            stats[f"hits_{cat}"] = n
            log.info("court category %s: %d hits (%.0fs)", cat, n, time.time() - t0)
        stats["cases"] = len(keys)
        return stats

    def _upsert(self, cases) -> None:
        rows = []
        for c in cases:
            c = dict(c)
            c["snippet_text"] = " ".join(re.sub(r"</?mark>", "", s) for s in c["snippets"].values()) or None
            c["snippets"] = Jsonb(c["snippets"])
            rows.append(c)
        with self.conn.cursor() as cur:
            cur.executemany(UPSERT_CASE_SQL, rows)
        self.conn.commit()

    def fetch_texts(self, limit: int | None = None) -> dict:
        """Full opinion text from the CourtListener API (with token) or the official PDF."""
        q = (
            "SELECT id, opinion_ids, pdf_url FROM court_cases WHERE text_checked_at IS NULL"
            " ORDER BY date_filed DESC"
        )
        todo = list(self.conn.execute(q + (f" LIMIT {int(limit)}" if limit else "")))
        self.conn.commit()
        pdf = PdfFetcher()
        got = 0
        try:
            for i, row in enumerate(todo, 1):
                text, source = None, None
                try:
                    if self.cl.has_token and row["opinion_ids"]:
                        parts = [t for t in (self.cl.opinion_text(o) for o in row["opinion_ids"]) if t]
                        if parts:
                            text, source = "\n\n".join(parts), "courtlistener"
                    if not text and row["pdf_url"]:
                        text = pdf.text(row["pdf_url"])
                        source = "official_pdf" if text else None
                except Exception as e:
                    log.warning("text fetch failed for case %s: %s", row["id"], e)
                    continue
                if text:
                    text = text.replace("\x00", "")
                    got += 1
                self.conn.execute(
                    "UPDATE court_cases SET full_text = %s, full_text_source = %s, text_checked_at = now()"
                    " WHERE id = %s",
                    (text, source, row["id"]),
                )
                if i % 25 == 0:
                    self.conn.commit()
                    log.info("opinion texts: %d/%d checked, %d with text", i, len(todo), got)
        finally:
            pdf.close()
        self.conn.commit()
        return {"texts_checked": len(todo), "texts_found": got}

    def extract_and_link(self) -> dict:
        """Find legislation mentioned in each decision and link it to bills in the database."""
        cases = list(self.conn.execute(
            "SELECT id, date_filed, coalesce(full_text, snippet_text, '') AS text FROM court_cases"
        ))
        self.conn.commit()
        n_links = 0
        with self.conn.cursor() as cur:
            for c in cases:
                mentions = extract_mentions(c["text"])
                links: dict[int, tuple[str, str]] = {}
                for m in mentions:
                    if m["kind"] == "session_law":
                        rows = cur.execute(
                            "SELECT id FROM bills WHERE session_law_year = %s AND session_law_chapter = %s"
                            " AND session_law_special IS NOT DISTINCT FROM %s",
                            (m["year"], m["chapter"], m["special"]),
                        ).fetchall()
                        if len(rows) == 1:
                            m["bill_pk"] = rows[0]["id"]
                            links.setdefault(rows[0]["id"], ("session_law", m["text"]))
                    elif m["kind"] == "bill" and m.get("year"):
                        if c["date_filed"] and m["year"] > c["date_filed"].year:
                            continue
                        rows = cur.execute(
                            "SELECT id FROM bills WHERE biennium = %s AND bill_number = %s AND type_code = %s"
                            " AND left(bill_id, 1) = %s",
                            (biennium_for_year(m["year"]), m["number"], m["type_code"], m["chamber"]),
                        ).fetchall()
                        if len(rows) == 1:
                            m["bill_pk"] = rows[0]["id"]
                            links.setdefault(rows[0]["id"], ("bill_number", m["text"]))
                cur.execute(
                    "UPDATE court_cases SET bill_mentions = %s WHERE id = %s", (Jsonb(mentions), c["id"])
                )
                cur.execute("DELETE FROM case_bill_links WHERE case_id = %s", (c["id"],))
                if links:
                    cur.executemany(
                        "INSERT INTO case_bill_links (case_id, bill_pk, link_type, mention) VALUES (%s,%s,%s,%s)",
                        [(c["id"], pk, lt, text) for pk, (lt, text) in links.items()],
                    )
                    n_links += len(links)
        self.conn.commit()
        return {"cases_scanned": len(cases), "bill_links": n_links}

    def latest_filed(self) -> date | None:
        row = self.conn.execute("SELECT max(date_filed) AS d FROM court_cases").fetchone()
        self.conn.commit()
        return row["d"] if row else None

    def refresh(self) -> dict:
        """Daily: re-query the last ~90 days of decisions, fetch new texts, relink."""
        latest = self.latest_filed()
        since = (latest - timedelta(days=90)).isoformat() if latest else None
        stats = self.collect(filed_after=since)
        stats.update(self.fetch_texts())
        stats.update(self.extract_and_link())
        return stats
