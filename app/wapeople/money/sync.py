"""Load each elected official's campaign money from the Public Disclosure Commission.

Order of work: decide who the officials are and link them to PDC records, load their campaign
summaries, reconcile each itemized dataset against the PDC's own per-campaign counts and totals
(only campaigns that differ are reloaded), load the smaller datasets, then rebuild the rollups
the pages read. Safe to re-run and to interrupt: every batch is one transaction, and a reload
replaces a campaign's rows atomically, so visitors never see a campaign half-loaded.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable

import psycopg
from psycopg.types.json import Jsonb

from ..districts import current_biennium_with_roster, delegations
from . import clean as C
from . import client as P
from .disclosure import family_names, parse, sanitize
from .link import Link, group_by_person, link_member, surname_tokens

log = logging.getLogger(__name__)

ROSTER = Path(__file__).parent.parent / "data" / "money_officials.json"
NON_MEMBER_ID_BASE = 1_000_000
LEGISLATIVE_OFFICES = ["STATE REPRESENTATIVE", "STATE SENATOR"]
# Committees whose own donors we look up when they gave to an official.
TRAIL_SOURCE_TYPES = ("Political action committees", "Political parties", "Legislative caucuses",
                      "Other organizations", "Unions")
TRAIL_MIN_GIVEN = 1000
TRAIL_MAX_AGE_DAYS = 7
SHRINK_FLOOR = 10       # Tables and batches smaller than this can change completely in normal use.
# Shown in a committee's list of funders instead of names that could be people's. That page is indexed.
PEOPLE_CODED_AS_ORGS = "Others, who may be individuals"
# What filers type when they have no employer to give, or their job in place of one. These don't rank.
EMPLOYER_STOP = (
    r"^(N[./]?A\.?|NONE( GIVEN)?\.?|NOT APPLICABLE|NOT EMPLO[A-Z]*|UN-?EMPLOYED|UNKNOWN|VARIOUS|RET\.?|RETIRED"
    r"|SELF|SELF[- ]*EM[A-Z]*|SELF( -|,) .*|OWNER|BUSINESS OWNER|INDEPENDENT|FREELANCE|PRIVATE PRACTICE"
    r"|HOME|HOME ?MAKER|HOUSEWIFE|STUDENT|ATTORNEY|ATTORNEY AT LAW|CONSULTANT|CONTRACTOR|CEO|PRESIDENT"
    r"|EXECUTIVE DIRECTOR|MANAGER|ENGINEER|PHYSICIAN|REALTOR|FARMER|[-.?* ]+|(INFO(RMATION)? )?REQUESTED.*"
    # Federal files often join employer and occupation: 'RETIRED/RETIRED', 'SELF/DENTIST', 'BEST EFFORT'.
    r"|(.*/)?(RETIRED|NOT EMPLOYED)(/.*)?|BEST EFFORTS?(/.*)?|SELF/.*)$"
)
# So MICROSOFT and MICROSOFT CORPORATION rank as one employer.
EMPLOYER_GROUP = "regexp_replace(upper(employer), '[,.]*[[:space:]]+(CORP|CORPORATION|INC|INCORPORATED|LLC|LLP|CO|COMPANY|LTD)[.]?$', '')"
# Report lines that lump small payments together ('EXPENSES OF $50 OR LESS'): nobody was paid that name.
PAYEE_LUMP_SUM = r"\$[0-9]|^(PAST |CREDIT )?EXPENSES\M"
# A real employer has several employees giving; below this it is mostly someone's own small firm.
MIN_EMPLOYER_DONORS = 3


# ---- What to load from each itemized dataset -----------------------------------------------
# `select` never names an address, phone or email column, so those never leave the source.

def _contribution(r: dict, person: int, own_names: frozenset[str] = frozenset()) -> tuple:
    name = C.text(r.get("contributor_name")) or "(name not reported)"
    code, cat = r.get("code"), r.get("contributor_category")
    occupation = C.free_text(r.get("contributor_occupation"))
    org = C.is_org(code, cat, name, own_names, occupation)
    # A business can be named '515 Park Place LLC', so only an address typed after its name goes.
    name = (C.org_name(name) if org else C.person_name(name)) or "(name not reported)"
    return (int(r["id"]), person, int(r["committee_id"]), C.integer(r.get("election_year")),
            r.get("report_number"), C.text(r.get("origin")), C.money(r.get("amount")),
            C.text(r.get("cash_or_in_kind")), C.day(r.get("receipt_date")), C.text(r.get("primary_general")),
            C.source_type(code, cat, name, own_names), org, name, C.org_key(name) if org else C.name_key(name),
            C.place(r.get("contributor_city")), C.text(r.get("contributor_state")),
            C.zip_code(r.get("contributor_zip")), occupation,
            C.employer(r.get("contributor_employer_name")), C.place(r.get("contributor_employer_city")),
            C.text(r.get("contributor_employer_state")), C.free_text(r.get("description")),
            C.free_text(r.get("memo")), C.link(r.get("url")))


def _expenditure(r: dict, person: int, _own_names: frozenset[str] = frozenset()) -> tuple:
    return (int(r["id"]), person, int(r["committee_id"]), C.integer(r.get("election_year")),
            r.get("report_number"), C.text(r.get("origin")), C.money(r.get("amount")),
            C.text(r.get("itemized_or_non_itemized")), C.day(r.get("expenditure_date")),
            C.spend_category(r.get("code"), r.get("description"), r.get("recipient_name")),
            C.free_text(r.get("description")),
            C.name_text(r.get("recipient_name")), C.place(r.get("recipient_city")),
            C.text(r.get("recipient_state")), C.zip_code(r.get("recipient_zip")), C.link(r.get("url")))


def _loan(r: dict, person: int, _own_names: frozenset[str] = frozenset()) -> tuple:
    return (int(r["id"]), person, int(r["committee_id"]), C.integer(r.get("election_year")),
            r.get("report_number"), C.text(r.get("origin")), C.money(r.get("amount")),
            C.money_or_none(r.get("endorser_liable_amount")), C.text(r.get("cash_or_in_kind")),
            C.day(r.get("receipt_date")), C.day(r.get("loan_due_date")), C.text(r.get("repayment_schedule")),
            C.text(r.get("lender_or_endorser")), C.text(r.get("transaction_type")),
            C.text(r.get("primary_general")), C.name_text(r.get("lenders_name")), C.place(r.get("lenders_city")),
            C.text(r.get("lenders_state")), C.free_text(r.get("lenders_occupation")),
            C.employer(r.get("lenders_employer")), C.text(r.get("carry_forward_loan")),
            C.free_text(r.get("description")), C.link(r.get("url")))


def _debt(r: dict, person: int, _own_names: frozenset[str] = frozenset()) -> tuple:
    return (int(r["id"]), person, int(r["committee_id"]), C.integer(r.get("election_year")),
            r.get("report_number"), C.text(r.get("origin")), C.money(r.get("amount")),
            C.text(r.get("record_type")), C.day(r.get("from_date")), C.day(r.get("thru_date")),
            C.day(r.get("debt_date")), C.text(r.get("code")), C.free_text(r.get("description")),
            C.name_text(r.get("vendor_name")), C.place(r.get("vendor_city")), C.text(r.get("vendor_state")),
            C.link(r.get("url")))


def _pledge(r: dict, person: int, _own_names: frozenset[str] = frozenset()) -> tuple:
    return (int(r["id"]), person, int(r["committee_id"]), C.integer(r.get("election_year")),
            r.get("report_number"), C.text(r.get("origin")), C.money(r.get("amount")),
            C.day(r.get("receipt_date")), C.text(r.get("primary_general")), C.text(r.get("code")),
            C.name_text(r.get("contributor_name")), C.place(r.get("contributor_city")),
            C.text(r.get("contributor_state")), C.free_text(r.get("contributor_occupation")),
            C.employer(r.get("contributor_employer_name")), C.link(r.get("url")))


@dataclass(frozen=True)
class ItemSet:
    dataset: str
    table: str
    columns: tuple[str, ...]
    select: str
    convert: Callable[[dict, int, frozenset[str]], tuple]


COMMON = "id,committee_id,election_year,report_number,origin,amount,url"
ITEM_SETS = (
    ItemSet(P.CONTRIBUTIONS, "money_contributions",
            ("id", "person_pk", "committee_id", "election_year", "report_number", "origin", "amount",
             "cash_or_in_kind", "receipt_date", "primary_general", "source_type", "is_org",
             "contributor_name", "contributor_key", "city", "state", "zip", "occupation", "employer",
             "employer_city", "employer_state", "description", "memo", "url"),
            COMMON + ",cash_or_in_kind,receipt_date,primary_general,code,contributor_category,contributor_name,"
            "contributor_city,contributor_state,contributor_zip,contributor_occupation,contributor_employer_name,"
            "contributor_employer_city,contributor_employer_state,description,memo",
            _contribution),
    ItemSet(P.EXPENDITURES, "money_expenditures",
            ("id", "person_pk", "committee_id", "election_year", "report_number", "origin", "amount",
             "itemized", "expenditure_date", "category", "description", "recipient_name", "recipient_city",
             "recipient_state", "recipient_zip", "url"),
            COMMON + ",itemized_or_non_itemized,expenditure_date,code,description,recipient_name,"
            "recipient_city,recipient_state,recipient_zip",
            _expenditure),
    ItemSet(P.LOANS, "money_loans",
            ("id", "person_pk", "committee_id", "election_year", "report_number", "origin", "amount",
             "endorser_liable", "cash_or_in_kind", "receipt_date", "loan_due_date", "repayment_schedule",
             "lender_or_endorser", "transaction_type", "primary_general", "lender_name", "lender_city",
             "lender_state", "lender_occupation", "lender_employer", "carry_forward", "description", "url"),
            COMMON + ",endorser_liable_amount,cash_or_in_kind,receipt_date,loan_due_date,repayment_schedule,"
            "lender_or_endorser,transaction_type,primary_general,lenders_name,lenders_city,lenders_state,"
            "lenders_occupation,lenders_employer,carry_forward_loan,description",
            _loan),
    ItemSet(P.DEBTS, "money_debts",
            ("id", "person_pk", "committee_id", "election_year", "report_number", "origin", "amount",
             "record_type", "from_date", "thru_date", "debt_date", "code", "description", "vendor_name",
             "vendor_city", "vendor_state", "url"),
            COMMON + ",record_type,from_date,thru_date,debt_date,code,description,vendor_name,vendor_city,vendor_state",
            _debt),
    ItemSet(P.PLEDGES, "money_pledges",
            ("id", "person_pk", "committee_id", "election_year", "report_number", "origin", "amount",
             "receipt_date", "primary_general", "code", "contributor_name", "city", "state", "occupation",
             "employer", "url"),
            COMMON + ",receipt_date,primary_general,code,contributor_name,contributor_city,contributor_state,"
            "contributor_occupation,contributor_employer_name",
            _pledge),
)

CAMPAIGN_SELECT = (
    "id,filer_id,filer_type,election_year,filer_name,office,position,party,party_code,legislative_district,"
    "jurisdiction,person_id,committee_id,reporting_option,contributions_amount,carryforward_amount,"
    "expenditures_amount,loans_amount,pledges_amount,debts_amount,independent_expenditures_for_amount,"
    "independent_expenditures_against_amount,primary_election_status,general_election_status,updated_at,url"
)
LEGISLATIVE_CANDIDATE_SELECT = "id,filer_name,office,legislative_district,election_year,person_id"
ITEM_TOTALS_SELECT = "committee_id,election_year,count(id) as n,sum(amount) as s"
OUTSIDE_SELECT = ("id,candidate_entity_id,election_year,report_number,sponsor_id,sponsor_name,report_type,"
                  "report_date,for_or_against,portion_of_amount,url")
OUTSIDE_FUNDERS_SELECT = ("id,report_number,sponsor_id,sponsor_name,funders_name,funders_first_name,funders_city,"
                          "funders_state,funders_occupation,funders_employer,amount,date_received,url")
SURPLUS_SELECT = "id,person_id,filer_name,from_date,thru_date,year,raised,spent,balance,url"
SURPLUS_SPENDING_SELECT = ("id,person_id,election_year,report_number,amount,itemized_or_non_itemized,"
                           "expenditure_date,description,recipient_name,recipient_city,recipient_state,url")
DISCLOSURE_SELECT = "id,submission_id,person_id,receipt_date,period_start,period_end,url"
DISCLOSURE_DETAILS_SELECT = "id,json"      # Everything in `json` goes through disclosure.py's whitelist.
COMMITTEE_FILER_SELECT = "id,filer_id,filer_name,committee_acronym"
COMMITTEE_TOTALS_SELECT = "contributor_category,count(id) as n,sum(amount) as s"
COMMITTEE_FUNDERS_SELECT = "contributor_name,code,count(id) as n,sum(amount) as s"
# Every column list this module sends to the PDC. Tests check each one for private columns.
PDC_SELECTS = tuple(s.select for s in ITEM_SETS) + (
    CAMPAIGN_SELECT, LEGISLATIVE_CANDIDATE_SELECT, ITEM_TOTALS_SELECT, OUTSIDE_SELECT, OUTSIDE_FUNDERS_SELECT,
    SURPLUS_SELECT, SURPLUS_SPENDING_SELECT, DISCLOSURE_SELECT, DISCLOSURE_DETAILS_SELECT, COMMITTEE_FILER_SELECT,
    COMMITTEE_TOTALS_SELECT, COMMITTEE_FUNDERS_SELECT,
)


def _merge_funders(orgs: list[dict], limit: int = 30) -> list[dict]:
    """A committee's organizational funders, with spellings of one name combined and report
    lump-sum lines (miscellaneous receipts and the like) left out. Filers code some people as
    organizations; those are reported as one total, without their names."""
    merged: dict[str, dict] = {}
    for o in orgs:
        name = C.text(o.get("contributor_name"))
        if not name or not C.is_org(o.get("code"), "Organization", name):
            continue
        if C.keep_out_of_search(name, C.money(o.get("s")), C.SEARCHABLE_PEOPLE):
            people = merged.setdefault("", {"name": PEOPLE_CODED_AS_ORGS, "key": "", "total": 0.0, "n": 0,
                                            "type": C.SOURCE_LABELS["Individual"]})
            people["total"] = round(people["total"] + float(C.money(o.get("s"))), 2)
            people["n"] += int(o.get("n") or 0)
            continue
        key = C.org_key(name)
        m = merged.setdefault(key, {"name": name, "key": key, "total": 0.0, "n": 0,
                                    "type": C.source_type(o.get("code"), "Organization", name)})
        m["total"] = round(m["total"] + float(C.money(o.get("s"))), 2)
        m["n"] += int(o.get("n") or 0)
    return sorted(merged.values(), key=lambda m: -m["total"])[:limit]


def load_roster() -> dict:
    return json.loads(ROSTER.read_text())


class SuspiciousSourceData(RuntimeError):
    """The source returned far less than we already have: treat it as an outage, not as deletions."""


class MoneySync:
    def __init__(self, conn: psycopg.Connection, pdc: P.PDC | None = None):
        self.conn = conn
        self.pdc = pdc or P.PDC()

    def close(self) -> None:
        self.pdc.close()

    def _check_shrink(self, table: str, new_count: int) -> None:
        """Replacing a table is the one way a bad response could erase the site's data, so a
        result under half of what's stored stops the run. A real change that large is rare enough
        to deserve a person looking at it; the data stays as it was."""
        have = self.conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"]
        self.conn.commit()
        if have >= SHRINK_FLOOR and new_count < have / 2:
            raise SuspiciousSourceData(f"{table}: source returned {new_count} rows, we have {have}. Nothing changed.")

    # ---- Full run ---------------------------------------------------------------------------

    def run(self, full: bool = False, today: date | None = None) -> dict:
        today = today or date.today()
        stats: dict = {}
        t0 = time.time()
        stats.update(self.sync_people())
        stats.update(self.sync_campaigns())
        # Corrections that don't change a total (a fixed donor name, say) only show up in a reload,
        # so recent campaigns are refreshed once a week regardless.
        recent = today.year - 1 if today.weekday() == 6 else None
        for spec in ITEM_SETS:
            stats[spec.table] = self.sync_items(spec, full=full, reload_since=recent)
        stats.update(self.sync_outside())
        stats.update(self.sync_surplus())
        stats.update(self.sync_disclosures())
        stats.update(self.rebuild_rollups())
        stats.update(self.sync_committee_sources())
        stats["seconds"] = round(time.time() - t0)
        return stats

    # ---- Who the officials are --------------------------------------------------------------

    def sync_people(self) -> dict:
        roster = load_roster()
        offices = P.quoted(LEGISLATIVE_OFFICES)
        candidates = list(self.pdc.rows(
            P.SUMMARY, where=f"office in({offices}) AND filer_type='CA'",
            select=LEGISLATIVE_CANDIDATE_SELECT))
        have_links = self.conn.execute(
            "SELECT count(*) AS n FROM money_people WHERE cardinality(pdc_person_ids) > 0").fetchone()["n"]
        self.conn.commit()
        if have_links and not candidates:
            raise SuspiciousSourceData("The PDC returned no legislative campaigns. Nothing changed.")
        people = group_by_person(candidates)
        members = self.conn.execute(
            "SELECT id, name, first_name, last_name, agency, party, district, latest_biennium FROM members"
            " WHERE btrim(name) NOT IN ('Senator', 'Representative', '')"
        ).fetchall()
        terms_by_member: dict[int, list[dict]] = {}
        for t in self.conn.execute("SELECT member_id, biennium, agency, party, district FROM member_terms"):
            terms_by_member.setdefault(t["member_id"], []).append(t)
        biennium = current_biennium_with_roster(self.conn)
        seats = delegations(self.conn, biennium) if biennium else {}
        current = {m["id"] for d in seats.values() for m in d["senators"] + d["representatives"]}
        manual = roster.get("manual_links", {})
        member_ids = {r["id"] for r in self.conn.execute("SELECT id FROM members")}

        rows: dict[int, dict] = {}
        by_person: dict[int, int] = {}
        unlinked_current = []
        for m in members:
            terms = terms_by_member.get(m["id"], [])
            link: Link = link_member(m, terms, people, manual.get(str(m["id"])))
            for pid in link.person_ids:
                by_person[pid] = m["id"]
            is_current = m["id"] in current
            office = "State Senator" if m["agency"] == "Senate" else "State Representative"
            title = f"{office}, District {m['district']}" if m["district"] else office
            rows[m["id"]] = {
                "id": m["id"], "member_id": m["id"], "name": m["name"], "kind": "legislator",
                "title": title if is_current else f"Former {title}", "party": m["party"],
                "chamber": m["agency"], "district": m["district"], "is_current": is_current,
                "pdc_person_ids": link.person_ids, "link_method": link.method, "link_note": link.note,
                "source_url": None, "fec_candidate_ids": [],
            }
            if is_current and not link.person_ids:
                unlinked_current.append(m["name"])

        for o in roster["officials"]:
            pid = o["person_id"]
            row = rows.get(by_person.get(pid))
            if row:     # someone who served in the Legislature and now holds another office
                row.update(kind=o["kind"], title=o["title"], is_current=True, source_url=o["source_url"],
                           chamber=None, district=None)
            else:
                pk = NON_MEMBER_ID_BASE + pid
                rows[pk] = {"id": pk, "member_id": None, "name": o["name"], "kind": o["kind"],
                            "title": o["title"], "party": None, "chamber": None, "district": None,
                            "is_current": True, "pdc_person_ids": [pid], "link_method": "official roster",
                            "link_note": None, "source_url": o["source_url"], "fec_candidate_ids": []}

        # Members of Congress: someone who also served in the Legislature keeps one page for both.
        for o in roster.get("federal", []):
            member_id = o.get("member_id")
            row = rows.get(member_id)
            person_ids = o.get("pdc_person_ids", [])
            fields = dict(name=o["name"], kind="federal", title=o["title"], party=o["party"], is_current=True, chamber=None,
                          district=None, source_url=o["source_url"], fec_candidate_ids=o["fec_candidate_ids"])
            if row:
                if person_ids and not row["pdc_person_ids"]:    # The roster has what the name match couldn't find.
                    row.update(link_method="official roster", link_note=o.get("pdc_note"))
                row.update(fields, pdc_person_ids=sorted({*row["pdc_person_ids"], *person_ids}))
                continue
            # Keep the member's id when their row is missing or blank, as it is until the Legislature
            # step repairs it, so the page and its federal records stay put when the row comes back.
            pk = o.get("id") or member_id
            if pk is None:
                log.warning("federal roster entry for %s has no id or member_id; skipped", o["name"])
                continue
            if member_id:
                log.warning("no usable member row %s for %s; using the roster entry", member_id, o["name"])
            rows[pk] = {"id": pk, "member_id": member_id if member_id in member_ids else None,
                        "pdc_person_ids": list(person_ids), "link_method": "official roster",
                        "link_note": o.get("pdc_note"), **fields}

        # State officials who also ran for Congress. Their page stays a state page, with a federal tab.
        for o in roster.get("federal_runs", []):
            row = rows.get(o["id"])
            if row:
                row["fec_candidate_ids"] = sorted({*row["fec_candidate_ids"], *o["fec_candidate_ids"]})
            else:
                log.warning("federal run listed for %s, but no person %s", o["name"], o["id"])

        self._check_shrink("money_people", len(rows))
        with self.conn.cursor() as cur:
            cur.execute("DELETE FROM money_people WHERE NOT (id = ANY(%s))", (list(rows),))
            cur.executemany(
                "INSERT INTO money_people (id, member_id, name, kind, title, party, chamber, district,"
                " is_current, pdc_person_ids, link_method, link_note, source_url, fec_candidate_ids)"
                " VALUES (%(id)s, %(member_id)s, %(name)s, %(kind)s, %(title)s, %(party)s, %(chamber)s,"
                " %(district)s, %(is_current)s, %(pdc_person_ids)s, %(link_method)s, %(link_note)s, %(source_url)s,"
                " %(fec_candidate_ids)s)"
                " ON CONFLICT (id) DO UPDATE SET member_id = EXCLUDED.member_id, name = EXCLUDED.name,"
                " kind = EXCLUDED.kind, title = EXCLUDED.title, party = EXCLUDED.party,"
                " chamber = EXCLUDED.chamber, district = EXCLUDED.district, is_current = EXCLUDED.is_current,"
                " pdc_person_ids = EXCLUDED.pdc_person_ids, link_method = EXCLUDED.link_method,"
                " link_note = EXCLUDED.link_note, source_url = EXCLUDED.source_url,"
                " fec_candidate_ids = EXCLUDED.fec_candidate_ids, updated_at = now()",
                list(rows.values()),
            )
        self.conn.commit()
        linked = sum(1 for r in rows.values() if r["pdc_person_ids"])
        if unlinked_current:
            log.warning("current legislators with no PDC link: %s", ", ".join(unlinked_current))
        return {"people": len(rows), "people_linked": linked, "current_unlinked": unlinked_current}

    def _person_ids(self) -> dict[int, int]:
        """PDC person id -> our person key."""
        out: dict[int, int] = {}
        for r in self.conn.execute("SELECT id, pdc_person_ids FROM money_people"):
            for pid in r["pdc_person_ids"]:
                out[pid] = r["id"]
        self.conn.commit()
        return out

    # ---- Campaign summaries -----------------------------------------------------------------

    def sync_campaigns(self) -> dict:
        people = self._person_ids()
        seen: list[tuple] = []
        for batch in P.chunks(sorted(people), 150):
            where = f"person_id in({P.quoted(batch)}) AND filer_type='CA'"
            for r in self.pdc.rows(P.SUMMARY, where=where, select=CAMPAIGN_SELECT):
                pid = C.integer(r.get("person_id"))
                year = C.integer(r.get("election_year"))
                if pid not in people or year is None:
                    continue
                seen.append((
                    r["id"], people[pid], pid, C.text(r.get("filer_id")), C.integer(r.get("committee_id")), year,
                    C.text(r.get("filer_name")), C.text(r.get("office")), C.text(r.get("position")),
                    C.text(r.get("jurisdiction")), (r.get("legislative_district") or "").lstrip("0") or None,
                    C.text(r.get("party_code")) or C.text(r.get("party")), C.text(r.get("reporting_option")),
                    C.money_or_none(r.get("contributions_amount")), C.money_or_none(r.get("carryforward_amount")),
                    C.money_or_none(r.get("expenditures_amount")), C.money_or_none(r.get("loans_amount")),
                    C.money_or_none(r.get("pledges_amount")), C.money_or_none(r.get("debts_amount")),
                    C.money_or_none(r.get("independent_expenditures_for_amount")),
                    C.money_or_none(r.get("independent_expenditures_against_amount")),
                    C.text(r.get("primary_election_status")), C.text(r.get("general_election_status")),
                    C.link(r.get("url")), r.get("updated_at"),
                ))
        self._check_shrink("money_campaigns", len(seen))
        with self.conn.cursor() as cur:
            cur.execute("DELETE FROM money_campaigns")
            cur.executemany(
                "INSERT INTO money_campaigns (id, person_pk, pdc_person_id, filer_id, committee_id, election_year,"
                " filer_name, office, position, jurisdiction, district, party, reporting_option, contributions,"
                " carryforward, expenditures, loans, pledges, debts, ie_for, ie_against, primary_status,"
                " general_status, url, updated_at)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
                " ON CONFLICT (id) DO NOTHING",
                seen,
            )
            # Statewide officials and justices get their party from their most recent campaign.
            cur.execute(
                "UPDATE money_people p SET party = c.party FROM ("
                "  SELECT DISTINCT ON (person_pk) person_pk, party FROM money_campaigns"
                "  WHERE party IS NOT NULL ORDER BY person_pk, election_year DESC) c"
                " WHERE p.id = c.person_pk AND p.kind = 'statewide'"
            )
        self.conn.commit()
        return {"campaigns": len(seen)}

    def _own_names(self) -> dict[int, frozenset[str]]:
        """Each official's family name as words, for recognizing their own money among contributions."""
        rows = self.conn.execute(
            "SELECT p.id, p.name, m.last_name FROM money_people p LEFT JOIN members m ON m.id = p.member_id"
        ).fetchall()
        self.conn.commit()
        return {r["id"]: surname_tokens(r["name"], r["last_name"]) for r in rows}

    def _committee_people(self) -> dict[int, int]:
        rows = self.conn.execute(
            "SELECT committee_id, min(person_pk) AS person_pk FROM money_campaigns"
            " WHERE committee_id IS NOT NULL GROUP BY committee_id"
        ).fetchall()
        self.conn.commit()
        return {r["committee_id"]: r["person_pk"] for r in rows}

    # ---- Itemized records, reconciled against the PDC's own totals ---------------------------

    def _remote_totals(self, dataset: str, committees: list[int]) -> dict[tuple, tuple]:
        out: dict[tuple, tuple] = {}
        for batch in P.chunks(committees, 200):
            for r in self.pdc.get(
                dataset, select=ITEM_TOTALS_SELECT,
                where=f"committee_id in({P.quoted(batch)})", group="committee_id,election_year",
                limit=50000,
            ):
                key = (C.integer(r.get("committee_id")), C.integer(r.get("election_year")))
                out[key] = (int(r["n"]), C.money(r.get("s")))
        return out

    def _local_totals(self, table: str) -> dict[tuple, tuple]:
        rows = self.conn.execute(
            f"SELECT committee_id, election_year, count(*) AS n, sum(amount) AS s FROM {table}"
            " GROUP BY committee_id, election_year"
        ).fetchall()
        self.conn.commit()
        return {(r["committee_id"], r["election_year"]): (r["n"], C.money(r["s"])) for r in rows}

    def sync_items(self, spec: ItemSet, full: bool = False, reload_since: int | None = None) -> dict:
        owners = self._committee_people()
        own_names = self._own_names()
        committees = sorted(owners)
        remote = self._remote_totals(spec.dataset, committees)
        self._check_shrink(spec.table, sum(v[0] for v in remote.values()))
        with self.conn.cursor() as cur:
            # A committee can change hands only if a link was corrected: follow it, or drop it.
            cur.execute(f"DELETE FROM {spec.table} WHERE NOT (committee_id = ANY(%s))", (committees,))
            cur.execute(
                f"UPDATE {spec.table} t SET person_pk = c.person_pk FROM ("
                "  SELECT committee_id, min(person_pk) AS person_pk FROM money_campaigns GROUP BY committee_id) c"
                " WHERE t.committee_id = c.committee_id AND t.person_pk <> c.person_pk"
            )
        self.conn.commit()
        local = self._local_totals(spec.table)
        stale = {k[0] for k in set(remote) | set(local) if remote.get(k) != local.get(k)}
        if full:
            stale = set(committees)
        elif reload_since:
            stale |= {k[0] for k in remote if k[1] and k[1] >= reload_since}
        loaded = 0
        for batch in P.chunks(sorted(stale), 40):
            rows = []
            for r in self.pdc.rows(spec.dataset, where=f"committee_id in({P.quoted(batch)})", select=spec.select):
                cid = C.integer(r.get("committee_id"))
                if cid in owners and C.integer(r.get("id")) is not None:
                    person = owners[cid]
                    rows.append(spec.convert(r, person, own_names.get(person, frozenset())))
            self._check_batch(spec.table, batch, len(rows), remote, local)
            self._replace(spec, batch, rows)
            loaded += len(rows)
        after = self._local_totals(spec.table)
        mismatched = [k for k in set(remote) | set(after) if remote.get(k) != after.get(k)]
        if mismatched:
            # The source updates within minutes of a filing, so a few can differ for a moment.
            log.warning("%s: %d campaigns differ from the PDC after loading: %s",
                        spec.table, len(mismatched), mismatched[:5])
        return {"committees_reloaded": len(stale), "rows_loaded": loaded, "rows_total": sum(v[0] for v in after.values()),
                "mismatched": len(mismatched)}

    @staticmethod
    def _check_batch(table: str, batch: list[int], got: int, remote: dict[tuple, tuple], local: dict[tuple, tuple]) -> None:
        """The same stop for one batch of campaigns, because a whole-table count can't see one bad
        page of results. A few rows the PDC itself no longer lists are a real correction and go through."""
        committees = set(batch)
        expected = sum(v[0] for k, v in remote.items() if k[0] in committees)
        have = sum(v[0] for k, v in local.items() if k[0] in committees)
        if got < expected / 2 or (not got and have >= SHRINK_FLOOR):
            raise SuspiciousSourceData(
                f"{table}: source returned {got} rows for {len(batch)} campaigns; it reports {expected} and we have"
                f" {have}. This batch was not changed.")

    def _replace(self, spec: ItemSet, committees: list[int], rows: list[tuple]) -> None:
        columns = ", ".join(spec.columns)
        ids = [r[0] for r in rows]
        with self.conn.cursor() as cur:
            cur.execute(f"DELETE FROM {spec.table} WHERE committee_id = ANY(%s)", (committees,))
            cur.execute(f"DELETE FROM {spec.table} WHERE id = ANY(%s)", (ids,))
            with cur.copy(f"COPY {spec.table} ({columns}) FROM STDIN") as copy:
                seen = set()
                for row in rows:
                    if row[0] not in seen:
                        seen.add(row[0])
                        copy.write_row(row)
        self.conn.commit()

    # ---- Outside money ----------------------------------------------------------------------

    def sync_outside(self) -> dict:
        people = self._person_ids()
        rows, reports = [], set()
        for batch in P.chunks(sorted(people), 150):
            where = f"candidate_entity_id in({P.quoted(batch)}) AND origin like 'C6.3%'"
            for r in self.pdc.rows(P.OUTSIDE, where=where, select=OUTSIDE_SELECT):
                pid = C.integer(r.get("candidate_entity_id"))
                if pid in people:
                    rows.append((r["id"], people[pid], C.integer(r.get("election_year")), r.get("report_number"),
                                 r.get("sponsor_id"), C.text(r.get("sponsor_name")), C.text(r.get("report_type")),
                                 C.day(r.get("report_date")), C.text(r.get("for_or_against")),
                                 C.money(r.get("portion_of_amount")), C.link(r.get("url"))))
                    reports.add(r.get("report_number"))
        funders = []
        for batch in P.chunks(sorted(x for x in reports if x), 150):
            where = f"report_number in({P.quoted(batch)}) AND origin like 'C6.5%'"
            for r in self.pdc.rows(P.OUTSIDE, where=where, select=OUTSIDE_FUNDERS_SELECT):
                name = " ".join(x for x in (C.name_text(r.get("funders_first_name")),
                                            C.name_text(r.get("funders_name"))) if x)
                funders.append((r["id"], r["report_number"], r.get("sponsor_id"), C.text(r.get("sponsor_name")),
                                name or None, C.place(r.get("funders_city")), C.text(r.get("funders_state")),
                                C.free_text(r.get("funders_occupation")), C.employer(r.get("funders_employer")),
                                C.money(r.get("amount")), C.day(r.get("date_received")), C.link(r.get("url"))))
        self._check_shrink("money_outside", len(rows))
        self._check_shrink("money_outside_funders", len(funders))
        with self.conn.cursor() as cur:
            cur.execute("DELETE FROM money_outside")
            cur.execute("DELETE FROM money_outside_funders")
            cur.executemany(
                "INSERT INTO money_outside (id, person_pk, election_year, report_number, sponsor_id, sponsor_name,"
                " report_type, report_date, for_or_against, amount, url)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (id) DO NOTHING", rows)
            cur.executemany(
                "INSERT INTO money_outside_funders (id, report_number, sponsor_id, sponsor_name, funder_name, city,"
                " state, occupation, employer, amount, date_received, url)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (id) DO NOTHING", funders)
        self.conn.commit()
        return {"outside_rows": len(rows), "outside_funders": len(funders)}

    # ---- Surplus funds ----------------------------------------------------------------------

    def sync_surplus(self) -> dict:
        people = self._person_ids()
        accounts, spending = [], []
        for batch in P.chunks(sorted(people), 150):
            where = f"person_id in({P.quoted(batch)})"
            for r in self.pdc.rows(P.SURPLUS, where=where, select=SURPLUS_SELECT):
                accounts.append((r["id"], people[C.integer(r["person_id"])], C.text(r.get("filer_name")),
                                 C.day(r.get("from_date")), C.day(r.get("thru_date")), C.integer(r.get("year")),
                                 C.money_or_none(r.get("raised")), C.money_or_none(r.get("spent")),
                                 C.money_or_none(r.get("balance")), C.link(r.get("url"))))
            for r in self.pdc.rows(P.SURPLUS_SPENDING, where=where, select=SURPLUS_SPENDING_SELECT):
                spending.append((int(r["id"]), people[C.integer(r["person_id"])], C.integer(r.get("election_year")),
                                 r.get("report_number"), C.money(r.get("amount")),
                                 C.text(r.get("itemized_or_non_itemized")), C.day(r.get("expenditure_date")),
                                 C.free_text(r.get("description")), C.name_text(r.get("recipient_name")),
                                 C.place(r.get("recipient_city")), C.text(r.get("recipient_state")), C.link(r.get("url"))))
        self._check_shrink("money_surplus", len(accounts))
        self._check_shrink("money_surplus_spending", len(spending))
        with self.conn.cursor() as cur:
            cur.execute("DELETE FROM money_surplus")
            cur.execute("DELETE FROM money_surplus_spending")
            cur.executemany(
                "INSERT INTO money_surplus (id, person_pk, filer_name, from_date, thru_date, year, raised, spent,"
                " balance, url) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (id) DO NOTHING", accounts)
            cur.executemany(
                "INSERT INTO money_surplus_spending (id, person_pk, election_year, report_number, amount, itemized,"
                " expenditure_date, description, recipient_name, recipient_city, recipient_state, url)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (id) DO NOTHING", spending)
        self.conn.commit()
        return {"surplus_accounts": len(accounts), "surplus_spending": len(spending)}

    # ---- Personal financial affairs ---------------------------------------------------------

    def sync_disclosures(self) -> dict:
        people = self._person_ids()
        remote: dict[int, dict] = {}
        for batch in P.chunks(sorted(people), 150):
            for r in self.pdc.rows(P.DISCLOSURES, where=f"person_id in({P.quoted(batch)})", select=DISCLOSURE_SELECT):
                i = C.integer(r.get("id"))
                if i is not None:
                    remote[i] = r
        self._check_shrink("money_disclosures", len(remote))
        local = {r["id"]: r["submission_id"] for r in self.conn.execute("SELECT id, submission_id FROM money_disclosures")}
        self.conn.commit()
        # An amended statement keeps its id and gets a new submission_id, so that's how we notice it.
        changed = sorted(i for i, r in remote.items()
                         if i not in local or C.text(r.get("submission_id")) != local[i])
        owners = {i: people[C.integer(r["person_id"])] for i, r in remote.items()}
        # Someone can name a spouse on one statement and leave that field empty on another, so names are
        # checked across all of a person's statements. A new one can name someone the older ones mention,
        # so one changed statement rereads them all.
        touched = {owners[i] for i in changed}
        rereads = sorted(i for i in remote if owners[i] in touched)
        raw: dict[int, dict | None] = {}
        for batch in P.chunks(rereads, 100):
            for r in self.pdc.rows(P.DISCLOSURES, where=f"id in({P.quoted(batch)})", select=DISCLOSURE_DETAILS_SELECT):
                raw[int(r["id"])] = parse(r.get("json"))
        family: dict[int, list[str]] = {}
        for i, doc in raw.items():
            family.setdefault(owners[i], []).extend(family_names(doc) if doc else [])
        details = {i: sanitize(doc, family[owners[i]]) if doc else None for i, doc in raw.items()}
        with self.conn.cursor() as cur:
            cur.execute("DELETE FROM money_disclosures WHERE NOT (id = ANY(%s))", (list(remote),))
            # A statement the details request didn't return keeps what it has and is tried again next run.
            cur.executemany(
                "INSERT INTO money_disclosures (id, submission_id, person_pk, receipt_date, period_start, period_end,"
                " url, details) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (id) DO UPDATE SET"
                " submission_id = EXCLUDED.submission_id, receipt_date = EXCLUDED.receipt_date,"
                " period_start = EXCLUDED.period_start, period_end = EXCLUDED.period_end, url = EXCLUDED.url,"
                " details = coalesce(EXCLUDED.details, money_disclosures.details)",
                [(i, C.text(remote[i].get("submission_id")) if i in details else local.get(i), owners[i],
                  C.day(remote[i].get("receipt_date")), C.day(remote[i].get("period_start")),
                  C.day(remote[i].get("period_end")), C.link(remote[i].get("url")),
                  Jsonb(details[i]) if details.get(i) else None) for i in sorted({*changed, *details})],
            )
            # A corrected link moves every statement, including the ones that didn't change.
            cur.execute(
                "UPDATE money_disclosures d SET person_pk = v.pk FROM unnest(%s::bigint[], %s::int[]) AS v(id, pk)"
                " WHERE d.id = v.id AND d.person_pk <> v.pk",
                (list(owners), list(owners.values())),
            )
        self.conn.commit()
        return {"disclosures": len(remote), "disclosures_new": len(changed)}

    # ---- Rollups the pages read -------------------------------------------------------------

    def _people_coded_as_orgs(self) -> list[str]:
        """Names filers coded as organizations that are really people, so they aren't listed as
        organizations. One filer reporting a name as an individual doesn't make it a person: that would
        take away the House Democratic Campaign Committee, Microsoft and hundreds more. Most of them doing
        it does, unless the name says otherwise, as '26th Legislative District Democrats' does."""
        rows = self.conn.execute(
            "SELECT o.key, o.name, o.n, coalesce(i.n, 0) AS as_person FROM ("
            "  SELECT contributor_key AS key, mode() WITHIN GROUP (ORDER BY contributor_name) AS name, count(*) AS n"
            "  FROM money_contributions WHERE is_org AND contributor_key <> '' GROUP BY 1) o"
            " LEFT JOIN (SELECT contributor_key AS key, count(*) AS n FROM money_contributions"
            "  WHERE NOT is_org GROUP BY 1) i USING (key)"
        ).fetchall()
        # The filed name, not the key: the key drops LLC and INC, and that turns 'WHITNEY LANE LLC' into a person.
        return [r["key"] for r in rows if C.person_shaped(r["name"])
                or (r["as_person"] >= r["n"] and not C.reads_as_org(r["name"]))]

    def _names_kept_out_of_search(self) -> list[str]:
        """Organization names that stay off a person's overview, which search engines read. They're still
        in the Contributions tab, which they don't."""
        rows = self.conn.execute(
            "SELECT contributor_key AS key, mode() WITHIN GROUP (ORDER BY contributor_name) AS name,"
            " sum(amount) AS total, count(DISTINCT person_pk) AS people FROM money_contributions"
            " WHERE is_org AND contributor_key <> '' GROUP BY 1"
        ).fetchall()
        return [r["key"] for r in rows if C.keep_out_of_search(r["name"], r["total"], r["people"])]

    def rebuild_rollups(self) -> dict:
        c = self.conn
        roll: dict[int, dict] = {}
        people_as_orgs = self._people_coded_as_orgs()
        not_on_overview = sorted({*people_as_orgs, *self._names_kept_out_of_search()})

        def add(key: str, rows) -> None:
            for r in rows:
                roll.setdefault(r["person_pk"], {}).setdefault(key, []).append(
                    {k: (float(v) if k in ("total",) else v) for k, v in r.items() if k != "person_pk"})

        add("by_source", c.execute(
            "SELECT person_pk, source_type AS type, sum(amount) AS total, count(*) AS n FROM money_contributions"
            " GROUP BY 1, 2 ORDER BY 1, 3 DESC"))
        add("by_state", c.execute(
            "SELECT person_pk, state, total, n FROM ("
            " SELECT person_pk, upper(state) AS state, sum(amount) AS total, count(*) AS n,"
            " row_number() OVER (PARTITION BY person_pk ORDER BY sum(amount) DESC) AS rn"
            " FROM money_contributions WHERE state IS NOT NULL AND upper(state) NOT IN ('WA', 'WASHINGTON')"
            " GROUP BY 1, 2) t WHERE rn <= 12 ORDER BY 1, 3 DESC"))
        add("by_city", c.execute(
            "SELECT person_pk, city, state, total, n FROM ("
            " SELECT person_pk, initcap(city) || coalesce(', ' || upper(state), '') AS city, upper(state) AS state,"
            " sum(amount) AS total, count(*) AS n,"
            " row_number() OVER (PARTITION BY person_pk ORDER BY sum(amount) DESC) AS rn"
            " FROM money_contributions WHERE city IS NOT NULL GROUP BY person_pk, initcap(city), upper(state)) t"
            " WHERE rn <= 15 ORDER BY 1, 4 DESC"))
        add("top_orgs", c.execute(
            "SELECT person_pk, key, name, type, total, n FROM ("
            " SELECT person_pk, contributor_key AS key, mode() WITHIN GROUP (ORDER BY contributor_name) AS name,"
            " mode() WITHIN GROUP (ORDER BY source_type) AS type,"
            " sum(amount) AS total, count(*) AS n,"
            " row_number() OVER (PARTITION BY person_pk ORDER BY sum(amount) DESC) AS rn"
            " FROM money_contributions WHERE is_org AND contributor_key <> '' AND NOT (contributor_key = ANY(%s))"
            " GROUP BY 1, 2) t WHERE rn <= 25 ORDER BY 1, 5 DESC", (not_on_overview,)))
        # A self-employed donor often types their own name as the employer. Comparing words catches
        # 'Michael Murr' and 'Michael Murr LLC' for the donor MURR MICHAEL.
        add("top_employers", c.execute(
            "SELECT person_pk, employer, total, n FROM ("
            " SELECT person_pk, max(employer) AS employer, sum(amount) AS total, count(*) AS n,"
            " row_number() OVER (PARTITION BY person_pk ORDER BY sum(amount) DESC) AS rn"
            " FROM (SELECT *, string_to_array(contributor_key, ' ') AS donor,"
            "   array_remove(regexp_split_to_array(replace(upper(employer), chr(39), ''), '[^A-Z0-9]+'), '') AS works"
            "   FROM money_contributions WHERE NOT is_org AND employer IS NOT NULL AND upper(employer) !~ %s) m"
            " WHERE NOT (donor <@ works OR works <@ donor)"
            " GROUP BY person_pk, " + EMPLOYER_GROUP + " HAVING count(DISTINCT contributor_key) >= %s) t"
            " WHERE rn <= 15 ORDER BY 1, 3 DESC", (EMPLOYER_STOP, MIN_EMPLOYER_DONORS)))
        add("by_spend", c.execute(
            "SELECT person_pk, category, sum(amount) AS total, count(*) AS n FROM money_expenditures"
            " GROUP BY 1, 2 ORDER BY 1, 3 DESC"))
        add("top_payees", c.execute(
            "SELECT person_pk, name, total, n FROM ("
            " SELECT person_pk, max(recipient_name) AS name, sum(amount) AS total, count(*) AS n,"
            " row_number() OVER (PARTITION BY person_pk ORDER BY sum(amount) DESC) AS rn"
            " FROM money_expenditures WHERE recipient_name IS NOT NULL AND category <> 'Contribution Refunds'"
            " AND category <> %s"       # The name on an in-kind row gave to the campaign. It wasn't paid.
            " AND coalesce(description, '') !~* '\\mrefund' AND itemized IS DISTINCT FROM 'Non-itemized'"
            " AND upper(recipient_name) !~ %s"
            " GROUP BY person_pk, upper(recipient_name)) t WHERE rn <= 15 ORDER BY 1, 3 DESC", (C.IN_KIND, PAYEE_LUMP_SUM)))
        for person in roll.values():
            for org in person.get("top_orgs", []):
                if org["key"] in C.ORG_DISPLAY_NAMES:
                    org["name"] = org["key"]
        rows = c.execute(
            "WITH item AS ("
            "  SELECT person_pk, sum(amount) AS total, count(*) AS n,"
            "   sum(amount) FILTER (WHERE upper(state) IN ('WA', 'WASHINGTON')) AS wa,"
            "   sum(amount) FILTER (WHERE state IS NOT NULL AND upper(state) NOT IN ('WA', 'WASHINGTON')) AS out,"
            "   sum(amount) FILTER (WHERE state IS NULL) AS unknown"
            "  FROM money_contributions GROUP BY 1),"
            " camp AS ("
            "  SELECT person_pk, sum(coalesce(contributions, 0)) AS raised, sum(coalesce(expenditures, 0)) AS spent,"
            "   count(*) AS n, min(election_year) AS first_year, max(election_year) AS last_year"
            "  FROM money_campaigns GROUP BY 1),"
            " latest AS ("
            "  SELECT DISTINCT ON (person_pk) person_pk, election_year, contributions FROM money_campaigns"
            # A committee just opened for a later race isn't the latest campaign in any useful sense.
            "  WHERE coalesce(contributions, 0) <> 0 ORDER BY person_pk,"
            "  (election_year <= extract(year FROM current_date)) DESC, election_year DESC, contributions DESC)"
            " SELECT p.id, item.total, item.n AS items, item.wa, item.out, item.unknown, camp.raised, camp.spent,"
            "  camp.n AS campaigns, camp.first_year, camp.last_year, latest.election_year AS latest_year,"
            "  latest.contributions AS latest_raised"
            " FROM money_people p LEFT JOIN item ON item.person_pk = p.id LEFT JOIN camp ON camp.person_pk = p.id"
            " LEFT JOIN latest ON latest.person_pk = p.id"
        ).fetchall()
        updates = []
        for r in rows:
            rollup = roll.get(r["id"], {})
            sources = rollup.get("by_source") or []
            updates.append({
                "id": r["id"], "raised": r["raised"] or 0, "spent": r["spent"] or 0, "itemized": r["total"] or 0,
                "items": r["items"] or 0, "campaigns": r["campaigns"] or 0, "first": r["first_year"],
                "last": r["last_year"], "ly": r["latest_year"], "lr": r["latest_raised"], "wa": r["wa"] or 0,
                "out": r["out"] or 0, "unknown": r["unknown"] or 0,
                "top": sources[0]["type"] if sources else None, "rollup": Jsonb(rollup),
            })
        with c.cursor() as cur:
            cur.executemany(
                "UPDATE money_people SET total_raised = %(raised)s, total_spent = %(spent)s,"
                " total_itemized = %(itemized)s, n_contributions = %(items)s, n_campaigns = %(campaigns)s,"
                " first_year = %(first)s, last_year = %(last)s, latest_year = %(ly)s, latest_raised = %(lr)s,"
                " in_state = %(wa)s, out_of_state = %(out)s, not_stated = %(unknown)s, top_source = %(top)s,"
                " rollup = %(rollup)s, updated_at = now() WHERE id = %(id)s",
                updates,
            )
            cur.execute("DELETE FROM money_orgs")
            cur.execute(
                "INSERT INTO money_orgs (key, name, source_type, total, n_gifts, n_people, last_date)"
                " SELECT contributor_key, mode() WITHIN GROUP (ORDER BY contributor_name),"
                " mode() WITHIN GROUP (ORDER BY source_type),"
                " sum(amount), count(*), count(DISTINCT person_pk), max(receipt_date)"
                " FROM money_contributions WHERE is_org AND contributor_key <> '' AND NOT (contributor_key = ANY(%s))"
                " GROUP BY 1", (people_as_orgs,)
            )
            orgs = cur.rowcount
            cur.execute("UPDATE money_orgs SET name = key WHERE key = ANY(%s)", (list(C.ORG_DISPLAY_NAMES),))
        c.commit()
        return {"rollups": len(updates), "organizations": orgs}

    # ---- One step up the trail: who funds the committees that gave ---------------------------

    def sync_committee_sources(self, limit: int = 2500) -> dict:
        wanted = {r["key"]: r for r in self.conn.execute(
            "SELECT key, name FROM money_orgs WHERE source_type = ANY(%s) AND total >= %s",
            (list(TRAIL_SOURCE_TYPES), TRAIL_MIN_GIVEN))}
        fresh = {r["key"] for r in self.conn.execute(
            "SELECT key FROM money_committee_sources WHERE synced_at > now() - make_interval(days => %s)",
            (TRAIL_MAX_AGE_DAYS,))}
        with_money = {r["key"] for r in self.conn.execute("SELECT key FROM money_committee_sources WHERE total_received > 0")}
        self.conn.commit()
        if not wanted:
            return {"committee_sources": 0}
        filers: dict[str, dict] = {}
        acronyms: dict[str, set[str]] = {}
        for r in self.pdc.rows(P.SUMMARY, where="filer_type='CO'", select=COMMITTEE_FILER_SELECT):
            key = C.org_key(r.get("filer_name"))
            if key and r.get("filer_id"):
                f = filers.setdefault(key, {"name": C.text(r.get("filer_name")), "ids": set()})
                f["ids"].add(r["filer_id"])
                acronym = C.org_key(r.get("committee_acronym"))
                if len(acronym) >= 3:
                    acronyms.setdefault(acronym, set()).add(key)
        # A donor reported under a committee's acronym, when only one committee uses that acronym.
        for acronym, keys in acronyms.items():
            if acronym in wanted and acronym not in filers and len(keys) == 1:
                filers[acronym] = filers[next(iter(keys))]
        filers = {k: v for k, v in filers.items() if k in wanted}
        self._check_shrink("money_committee_sources", len(filers))
        self.conn.execute("DELETE FROM money_committee_sources WHERE NOT (key = ANY(%s))", (list(filers),))
        self.conn.commit()
        todo = [k for k in filers if k not in fresh][:limit]
        now = datetime.now(timezone.utc)
        fetched: dict[frozenset, tuple] = {}
        for key in todo:
            f = filers[key]
            group = frozenset(f["ids"])
            if group not in fetched:
                ids = P.quoted(sorted(group))
                totals = self.pdc.get(
                    P.CONTRIBUTIONS, select=COMMITTEE_TOTALS_SELECT,
                    where=f"filer_id in({ids})", group="contributor_category", limit=10)
                orgs = self.pdc.get(
                    P.CONTRIBUTIONS, select=COMMITTEE_FUNDERS_SELECT,
                    where=f"filer_id in({ids}) AND contributor_category='Organization'",
                    group="contributor_name,code", order="s DESC", limit=80)
                fetched[group] = (totals, _merge_funders(orgs))
            totals, funders = fetched[group]
            if not totals and key in with_money:    # An empty answer isn't news: keep what we have.
                continue
            people = next((t for t in totals if t.get("contributor_category") == "Individual"), {})
            self.conn.execute(
                "INSERT INTO money_committee_sources (key, committee_name, filer_ids, total_received,"
                " from_individuals, individual_gifts, funders, synced_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)"
                " ON CONFLICT (key) DO UPDATE SET committee_name = EXCLUDED.committee_name,"
                " filer_ids = EXCLUDED.filer_ids, total_received = EXCLUDED.total_received,"
                " from_individuals = EXCLUDED.from_individuals, individual_gifts = EXCLUDED.individual_gifts,"
                " funders = EXCLUDED.funders, synced_at = EXCLUDED.synced_at",
                (key, f["name"], sorted(f["ids"]), sum(C.money(t.get("s")) for t in totals),
                 C.money(people.get("s")), int(people.get("n") or 0), Jsonb(funders), now),
            )
            self.conn.commit()
        return {"committee_sources": len(filers), "committee_sources_refreshed": len(todo)}
