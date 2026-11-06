"""The rules the money sync applies: who links to which record, what a bad source answer may not erase,
what the rollups leave out, and when the scheduler runs. Database tests need TEST_DATABASE_URL and empty
the tables they use, so never point them at real data."""
import json
import os
import re
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from wapeople.money import clean as C
from wapeople.money import sync as S
from wapeople.money.link import surname_tokens
from wapeople.money.sync import MoneySync, SuspiciousSourceData

TEST_DB = os.environ.get("TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not TEST_DB, reason="TEST_DATABASE_URL not set")
ROSTER = S.load_roster()
CONTRIBUTIONS, EXPENDITURES = S.ITEM_SETS[0], S.ITEM_SETS[1]
PRIVATE_COLUMN = re.compile(r"address|street|phone|email|treasurer", re.I)
NO_NAMES = frozenset()


# ---- Every request to the PDC ---------------------------------------------------------------

@pytest.mark.parametrize("select", S.PDC_SELECTS)
def test_no_select_names_a_private_column(select):
    assert not PRIVATE_COLUMN.search(select)


def test_every_select_is_a_named_constant_the_test_above_sees():
    assert not re.search(r"select=[\"f(]", Path(S.__file__).read_text())
    named = {v for k, v in vars(S).items() if k.endswith("_SELECT") and isinstance(v, str)}
    assert named <= set(S.PDC_SELECTS)


# ---- Each row as it's stored ----------------------------------------------------------------

def contribution(own_names=NO_NAMES, **fields) -> dict:
    raw = {"id": "1", "committee_id": "9001", "amount": "5.00", **fields}
    return dict(zip(CONTRIBUTIONS.columns, S._contribution(raw, 1, own_names)))


def test_a_self_code_needs_the_officials_own_name():
    doe = frozenset({"DOE"})
    assert contribution(doe, code="Self", contributor_name="DOE JANE")["source_type"] == C.SELF_FUNDED
    other = contribution(doe, code="Self", contributor_category="Organization", contributor_name="Fair Roads Fund")
    assert other["source_type"] == "Other organizations" and other["is_org"]


def test_the_officials_own_accounts_are_not_organizations():
    own = contribution(frozenset({"DOE"}), code="Business", contributor_category="Organization",
                       contributor_name="DOE JANE SURPLUS ACCOUNT")
    assert own["source_type"] == C.OWN_ACCOUNTS and not own["is_org"]


def test_a_person_coded_as_a_business_with_an_occupation_is_a_person():
    row = contribution(code="Business", contributor_category="Organization", contributor_name="KUPER GLENN",
                       contributor_occupation="Nurse")
    assert not row["is_org"]


def test_a_person_loses_a_trailing_house_number_from_the_stored_name_and_key():
    row = contribution(code="Individual", contributor_category="Individual", contributor_name="KUPER GLENN 1542")
    assert row["contributor_name"] == "KUPER GLENN" and row["contributor_key"] == "KUPER GLENN"


@pytest.mark.parametrize("raw, stored", [
    ("515 Park Place LLC", "515 Park Place LLC"),
    ("Alliance for Automotive Innovation 1050 K St. NW, Suite 650", "Alliance for Automotive Innovation"),
])
def test_an_organization_keeps_its_name_but_not_an_address_after_it(raw, stored):
    row = contribution(code="Business", contributor_category="Organization", contributor_name=raw)
    assert row["is_org"] and row["contributor_name"] == stored


@pytest.mark.parametrize("recipient, category", [
    ("HOUSE DEMOCRATIC CAUCUS", C.PARTY_TRANSFER),
    ("DOE JANE SURPLUS ACCOUNT", C.SURPLUS_TRANSFER),
])
def test_spending_is_categorized_with_its_recipient(recipient, category):
    raw = {"id": "1", "committee_id": "9001", "amount": "5.00", "description": "SURPLUS CONTRIBUTION",
           "recipient_name": recipient}
    assert dict(zip(EXPENDITURES.columns, S._expenditure(raw, 1)))["category"] == category


@pytest.mark.parametrize("name, last_name, expected", [
    ("Jane Doe", "Doe", {"DOE"}),
    ("Steven C. González", None, {"GONZALEZ"}),
    ("Marie Gluesenkamp Perez", None, {"GLUESENKAMP", "PEREZ"}),
    ("Peter von Reichbauer", "von Reichbauer", {"REICHBAUER"}),
])
def test_own_names_are_family_names(name, last_name, expected):
    assert surname_tokens(name, last_name) == expected


def test_people_coded_as_organizations_are_a_total_without_names():
    funders = S._merge_funders([
        {"contributor_name": "Shirley Keen", "code": "Business", "s": "2834.00", "n": "26"},
        {"contributor_name": "ANNE STAFFORD", "code": "Other", "s": "2475.00", "n": "3"},
        {"contributor_name": "EUNDUK CHOI", "code": "Business", "s": "900.00", "n": "2"},   # not a name our list knows
        {"contributor_name": "ANHEUSER BUSCH", "code": "Business", "s": "9000.00", "n": "2"},
        {"contributor_name": "MISCELLANEOUS RECEIPTS", "code": "Other", "s": "5000.00", "n": "1"},
    ])
    assert [(f["name"], f["key"], f["total"], f["n"]) for f in funders] == [
        ("ANHEUSER BUSCH", "ANHEUSER BUSCH", 9000.0, 2), (S.PEOPLE_CODED_AS_ORGS, "", 6209.0, 31)]
    assert "KEEN" not in json.dumps(funders).upper() and "CHOI" not in json.dumps(funders).upper()


@pytest.mark.parametrize("placeholder", [
    "REQUESTED", "INFO REQUESTED", "INFORMATION REQUESTED PER BEST EFFORT", "SELF", "SELF-EMPLOYED", "SELF EMPLOYEED",
    "SELF, DBA GARY LOCKE", "N/A", "NA", "NONE", "NOT EMPLOYED", "RETIRED", "--", "CEO", "FARMER", "ATTORNEY AT LAW",
])
def test_placeholders_and_job_titles_are_not_employers(placeholder):
    assert re.search(S.EMPLOYER_STOP, placeholder)


@pytest.mark.parametrize("employer", [
    "MICROSOFT", "ATTORNEY GENERALS OFFICE", "HOME DEPOT", "HOMESTREET BANK", "FARMERS INSURANCE", "RETIRECORP SPC",
    "INDEPENDENT COLLEGES OF WASHINGTON", "HOUSE OF REPRESENTATIVES", "EXECUTIVE REAL ESTATE",
])
def test_real_employers_are_not_placeholders(employer):
    assert not re.search(S.EMPLOYER_STOP, employer)


# ---- Who links to which record --------------------------------------------------------------

def test_every_link_by_hand_says_why():
    for link in ROSTER["manual_links"].values():
        assert link["person_ids"] and link["note"]
    for entry in ROSTER["federal"]:
        assert not entry.get("pdc_person_ids") or entry["pdc_note"]


def test_no_pdc_person_is_linked_twice():
    ids = [o["person_id"] for o in ROSTER["officials"]]
    ids += [i for link in ROSTER["manual_links"].values() for i in link["person_ids"]]
    ids += [i for f in ROSTER["federal"] for i in f.get("pdc_person_ids", [])]
    assert len(ids) == len(set(ids))


def test_same_person_links_are_there_and_same_surname_people_are_not():
    federal = {f["name"]: f.get("pdc_person_ids", []) for f in ROSTER["federal"]}
    assert federal["Marilyn Strickland"] == [6974] and federal["Marie Gluesenkamp Perez"] == [1015]
    assert ROSTER["manual_links"]["205"]["person_ids"] == [1099]        # Jay Inslee
    # In the PDC these names are a Spokane Valley council member, and a judge and a candidate named Newhouse.
    assert federal["Adam Smith"] == [] and federal["Dan Newhouse"] == []


def test_roulstones_note_matches_his_campaign_table():
    assert "only legislative campaign" in ROSTER["manual_links"]["18701"]["note"]


# ---- With a database ------------------------------------------------------------------------

class StubPDC:
    """Answers from two functions; by default every answer is an empty HTTP 200."""

    def __init__(self, rows=None, totals=None):
        self.answer_rows = rows or (lambda dataset, where, select: [])
        self.answer_totals = totals or (lambda dataset, params: [])
        self.requests = []

    def close(self):
        pass

    def rows(self, dataset, where, select, order="id"):
        self.requests.append((select, where))
        return iter(self.answer_rows(dataset, where, select))

    def get(self, dataset, **params):
        return self.answer_totals(dataset, params)


@pytest.fixture
def conn():
    import psycopg
    from psycopg.rows import dict_row

    from wapeople import db, migrate

    db.DATABASE_URL = TEST_DB
    migrate.connect = lambda: psycopg.connect(TEST_DB, row_factory=dict_row)
    migrate.migrate()
    c = psycopg.connect(TEST_DB, row_factory=dict_row)
    c.execute("TRUNCATE members, bienniums, money_people, money_orgs, money_committee_sources, money_outside_funders CASCADE")
    c.commit()
    yield c
    c.rollback()
    c.close()


def person(c, pk=1, name="Jane Doe", pids=(501,)):
    c.execute("INSERT INTO money_people (id, name, kind, pdc_person_ids) VALUES (%s, %s, 'legislator', %s)", (pk, name, list(pids)))


def campaign(c, pk=1, committee=9001):
    c.execute("INSERT INTO money_campaigns (id, person_pk, committee_id, election_year) VALUES (%s, %s, %s, 2024)",
              (f"ca-{committee}", pk, committee))


def gift(c, row_id, name, employer=None, org=False, key=None, amount=100):
    c.execute("INSERT INTO money_contributions (id, person_pk, committee_id, election_year, amount, source_type, is_org,"
              " contributor_name, contributor_key, employer) VALUES (%s, 1, 9001, 2024, %s, 'Businesses', %s, %s, %s, %s)",
              (row_id, amount, org, name, key or (C.org_key(name) if org else C.name_key(name)), employer))


def paid(c, row_id, name, category="Radio advertising", description=None, itemized="Itemized"):
    c.execute("INSERT INTO money_expenditures (id, person_pk, committee_id, election_year, amount, category, description,"
              " recipient_name, itemized) VALUES (%s, 1, 9001, 2024, 100, %s, %s, %s, %s)",
              (row_id, category, description, name, itemized))


def count(c, table) -> int:
    n = c.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"]
    c.commit()
    return n


def seeded_gifts(c, n=20):
    person(c)
    campaign(c)
    for i in range(n):
        gift(c, i + 1, f"DONOR NUMBER {i}")
    c.commit()


@needs_db
def test_private_columns_are_in_no_money_table(conn):
    rows = conn.execute("SELECT table_name, column_name FROM information_schema.columns"
                        " WHERE table_name LIKE 'money_%'").fetchall()
    assert rows and not [r for r in rows if PRIVATE_COLUMN.search(r["column_name"])]


@needs_db
def test_an_empty_itemized_answer_stops_the_run_before_any_delete(conn):
    seeded_gifts(conn)
    with pytest.raises(SuspiciousSourceData):
        MoneySync(conn, StubPDC()).sync_items(CONTRIBUTIONS)
    conn.rollback()
    assert count(conn, "money_contributions") == 20


@needs_db
def test_a_much_smaller_itemized_answer_stops_the_run(conn):
    seeded_gifts(conn)
    totals = lambda dataset, params: [{"committee_id": "9001", "election_year": "2024", "n": "9", "s": "900.00"}]
    with pytest.raises(SuspiciousSourceData):
        MoneySync(conn, StubPDC(totals=totals)).sync_items(CONTRIBUTIONS)
    conn.rollback()
    assert count(conn, "money_contributions") == 20


@needs_db
def test_an_empty_page_for_a_batch_the_pdc_says_has_rows_changes_nothing(conn):
    seeded_gifts(conn)
    totals = lambda dataset, params: [{"committee_id": "9001", "election_year": "2024", "n": "20", "s": "2000.00"}]
    with pytest.raises(SuspiciousSourceData):
        MoneySync(conn, StubPDC(totals=totals)).sync_items(CONTRIBUTIONS, full=True)
    conn.rollback()
    assert count(conn, "money_contributions") == 20


@needs_db
def test_a_small_correction_the_pdc_agrees_with_goes_through(conn):
    seeded_gifts(conn, n=3)
    stats = MoneySync(conn, StubPDC()).sync_items(CONTRIBUTIONS)
    assert count(conn, "money_contributions") == 0 and stats["mismatched"] == 0


@needs_db
def test_sync_items_recognizes_the_officials_own_money(conn):
    person(conn)
    campaign(conn)
    conn.commit()
    base = {"committee_id": "9001", "election_year": "2024", "amount": "100.00", "code": "Self"}
    pdc = StubPDC(
        totals=lambda dataset, params: [{"committee_id": "9001", "election_year": "2024", "n": "2", "s": "200.00"}],
        rows=lambda dataset, where, select: [
            {**base, "id": "1", "contributor_name": "DOE JANE"},
            {**base, "id": "2", "contributor_category": "Organization", "contributor_name": "Fair Roads Fund"}])
    assert MoneySync(conn, pdc).sync_items(CONTRIBUTIONS)["mismatched"] == 0
    rows = conn.execute("SELECT id, source_type FROM money_contributions ORDER BY id").fetchall()
    assert [r["source_type"] for r in rows] == [C.SELF_FUNDED, "Other organizations"]


@needs_db
def test_empty_funding_sources_keep_the_outside_funders(conn):
    person(conn)
    conn.execute("INSERT INTO money_outside (id, person_pk, report_number) VALUES ('o1', 1, 'C61')")
    for i in range(20):
        conn.execute("INSERT INTO money_outside_funders (id, report_number) VALUES (%s, 'C61')", (f"f{i}",))
    conn.commit()
    pdc = StubPDC(rows=lambda dataset, where, select: (
        [{"id": "o1", "candidate_entity_id": "501", "report_number": "C61", "portion_of_amount": "5"}] if "C6.3" in where else []))
    with pytest.raises(SuspiciousSourceData):
        MoneySync(conn, pdc).sync_outside()
    conn.rollback()
    assert count(conn, "money_outside_funders") == 20 and count(conn, "money_outside") == 1


@needs_db
def test_an_empty_filer_list_keeps_the_committee_sources(conn):
    conn.execute("INSERT INTO money_orgs (key, name, source_type, total, n_gifts, n_people)"
                 " VALUES ('BIG PAC', 'Big PAC', 'Political action committees', 5000, 1, 1)")
    for i in range(20):
        conn.execute("INSERT INTO money_committee_sources (key) VALUES (%s)", (f"COMMITTEE {i}",))
    conn.commit()
    with pytest.raises(SuspiciousSourceData):
        MoneySync(conn, StubPDC()).sync_committee_sources()
    conn.rollback()
    assert count(conn, "money_committee_sources") == 20


def statement(employer: str) -> str:
    return json.dumps({"statement": {"income": {"data": [{"who": ["filer"], "name": employer, "why": "Salary",
                                                          "income": "30000-59999"}]}}})


class StatementsPDC(StubPDC):
    """Statements 41 and 42. `details` is what a request for the JSON answers."""

    def __init__(self, submissions, details, person_id="501"):
        super().__init__(rows=self.answer)
        self.submissions, self.details, self.person_id = submissions, details, person_id

    def answer(self, dataset, where, select):
        if select == S.DISCLOSURE_SELECT:
            return [{"id": i, "submission_id": s, "person_id": self.person_id} for i, s in self.submissions.items()]
        return [{"id": i, "json": j} for i, j in self.details.items() if f"'{i}'" in where]

    def asked_for_details(self) -> bool:
        return any(select == S.DISCLOSURE_DETAILS_SELECT for select, _ in self.requests)


def stored(c, statement_id) -> dict:
    row = c.execute("SELECT submission_id, person_pk, details FROM money_disclosures WHERE id = %s", (statement_id,)).fetchone()
    c.commit()
    return row


@needs_db
def test_an_amended_statement_rereads_that_persons_others_and_no_one_elses(conn):
    person(conn)
    conn.commit()
    both = {"41": statement("First Job"), "42": statement("Second Job")}
    pdc = StatementsPDC({"41": "A", "42": "A"}, both)
    assert MoneySync(conn, pdc).sync_disclosures() == {"disclosures": 2, "disclosures_new": 2}

    pdc = StatementsPDC({"41": "A", "42": "A"}, both)
    assert MoneySync(conn, pdc).sync_disclosures() == {"disclosures": 2, "disclosures_new": 0}
    assert not pdc.asked_for_details()

    person(conn, 2, "Other Person", (502,))
    conn.execute("INSERT INTO money_disclosures (id, submission_id, person_pk, details) VALUES (43, 'A', 2, %s)",
                 (json.dumps({"income": [{"source": "Untouched"}]}),))
    conn.commit()

    class TwoPeople(StatementsPDC):
        def answer(self, dataset, where, select):
            if select == S.DISCLOSURE_SELECT:
                return [*super().answer(dataset, where, select), {"id": "43", "submission_id": "A", "person_id": "502"}]
            return super().answer(dataset, where, select)

    pdc = TwoPeople({"41": "B", "42": "A"}, {"41": statement("Port of Tacoma"), "42": statement("Reread"),
                                             "43": statement("Should Not Be Asked For")})
    assert MoneySync(conn, pdc).sync_disclosures() == {"disclosures": 3, "disclosures_new": 1}
    amended, same_person, someone_else = stored(conn, 41), stored(conn, 42), stored(conn, 43)
    assert amended["submission_id"] == "B" and "Port of Tacoma" in json.dumps(amended["details"])
    assert "Reread" in json.dumps(same_person["details"]) and same_person["submission_id"] == "A"
    assert "Untouched" in json.dumps(someone_else["details"])


@needs_db
def test_a_spouse_named_on_one_statement_is_removed_from_the_persons_others(conn):
    person(conn)
    conn.commit()
    names = json.dumps({"person": {"name": "Jane Doe"}, "statement": {"spouse": "Paula Rivers"}})
    other = json.dumps({"person": {"name": "Jane Doe"}, "statement": {"income": {"data": [
        {"who": ["spouse"], "name": "Paula Rivers Consulting", "why": "Wages", "income": "30000-59999"}]}}})
    MoneySync(conn, StatementsPDC({"41": "A", "42": "A"}, {"41": names, "42": other})).sync_disclosures()
    assert "Rivers" not in json.dumps(stored(conn, 42)["details"])


@needs_db
def test_an_amendment_whose_details_did_not_arrive_is_tried_again(conn):
    person(conn)
    conn.commit()
    MoneySync(conn, StatementsPDC({"41": "A"}, {"41": statement("First Job")})).sync_disclosures()
    MoneySync(conn, StatementsPDC({"41": "B"}, {})).sync_disclosures()
    row = stored(conn, 41)
    assert row["submission_id"] == "A" and "First Job" in json.dumps(row["details"])
    assert MoneySync(conn, StatementsPDC({"41": "B"}, {"41": statement("New Job")})).sync_disclosures()["disclosures_new"] == 1
    assert stored(conn, 41)["submission_id"] == "B"


@needs_db
def test_statements_loaded_before_submission_ids_are_fetched_once_more(conn):
    person(conn)
    conn.execute("INSERT INTO money_disclosures (id, person_pk) VALUES (41, 1)")
    conn.commit()
    pdc = StatementsPDC({"41": "A"}, {"41": statement("First Job")})
    assert MoneySync(conn, pdc).sync_disclosures()["disclosures_new"] == 1
    assert stored(conn, 41)["submission_id"] == "A"


@needs_db
def test_a_corrected_link_moves_a_statement_without_refetching_it(conn):
    person(conn, 1, "Jane Doe", (501,))
    person(conn, 2, "Jane Roe", (502,))
    conn.commit()
    MoneySync(conn, StatementsPDC({"41": "A"}, {"41": statement("First Job")})).sync_disclosures()
    pdc = StatementsPDC({"41": "A"}, {}, person_id="502")
    assert MoneySync(conn, pdc).sync_disclosures()["disclosures_new"] == 0
    assert not pdc.asked_for_details() and stored(conn, 41)["person_pk"] == 2


def federal_entry(name, member_id=None, pk=None, person_ids=None, note=None) -> dict:
    entry = {"id": pk, "member_id": member_id, "name": name, "title": "U.S. Representative", "party": "D",
             "fec_candidate_ids": [f"H{name[:3]}"], "source_url": "https://clerk.house.gov/"}
    if person_ids:
        entry.update(pdc_person_ids=person_ids, pdc_note=note)
    return entry


def members(c, *rows):
    c.execute("INSERT INTO bienniums (id, start_year, end_year, status, bills_synced_at) VALUES ('2025-26', 2025, 2026, 'adjourned_final', now())")
    for member_id, name in rows:
        c.execute("INSERT INTO members (id, name, first_name, last_name, agency, party, district, latest_biennium)"
                  " VALUES (%s, %s, %s, %s, 'Senate', 'R', '6', '2025-26')",
                  (member_id, name, name.split()[0], name.split()[-1]))
    c.commit()


def people(c) -> dict:
    rows = {r["id"]: r for r in c.execute("SELECT * FROM money_people")}
    c.commit()
    return rows


@needs_db
def test_a_federal_entry_with_no_usable_member_row_does_not_stop_the_step(conn, monkeypatch, caplog):
    members(conn, (15780, "Senator"))
    monkeypatch.setattr(S, "load_roster", lambda: {"officials": [], "manual_links": {}, "federal": [
        federal_entry("Michael Baumgartner", 15780, person_ids=[25683], note="His Senate campaigns."),
        federal_entry("Patty Murray", 299),
        federal_entry("Marilyn Strickland", pk=2000010, person_ids=[6974], note="Her Tacoma mayor campaigns."),
        federal_entry("Nobody Known"),
    ]})
    with caplog.at_level("WARNING"):
        stats = MoneySync(conn, StubPDC()).sync_people()
    rows = people(conn)
    assert stats["people"] == 3 and set(rows) == {15780, 299, 2000010}
    assert rows[15780]["member_id"] == 15780 and rows[15780]["pdc_person_ids"] == [25683]
    assert rows[299]["member_id"] is None and rows[299]["name"] == "Patty Murray"
    assert rows[2000010]["pdc_person_ids"] == [6974] and rows[2000010]["link_note"] == "Her Tacoma mayor campaigns."
    assert "Nobody Known" in caplog.text and "Patty Murray" in caplog.text


@needs_db
def test_a_repaired_member_row_keeps_the_same_page(conn, monkeypatch):
    members(conn, (15780, "Senator"))
    roster = {"officials": [], "manual_links": {}, "federal": [
        federal_entry("Michael Baumgartner", 15780, person_ids=[25683], note="His Senate campaigns.")]}
    monkeypatch.setattr(S, "load_roster", lambda: roster)
    MoneySync(conn, StubPDC()).sync_people()
    conn.execute("UPDATE members SET name = 'Michael Baumgartner', last_name = 'Baumgartner' WHERE id = 15780")
    conn.commit()
    candidate = {"id": "c1", "filer_name": "Other Person", "office": "STATE SENATOR", "person_id": "9"}
    MoneySync(conn, StubPDC(rows=lambda dataset, where, select: [candidate])).sync_people()
    rows = people(conn)
    assert set(rows) == {15780} and rows[15780]["pdc_person_ids"] == [25683] and rows[15780]["kind"] == "federal"


@needs_db
def test_a_roster_link_replaces_the_no_match_note(conn, monkeypatch):
    members(conn, (205, "Jay Inslee"))
    monkeypatch.setattr(S, "load_roster", lambda: {"officials": [], "manual_links": {}, "federal": [
        federal_entry("Jay Inslee", 205, person_ids=[1099], note="His campaigns for governor.")]})
    MoneySync(conn, StubPDC()).sync_people()
    row = people(conn)[205]
    assert row["pdc_person_ids"] == [1099] and row["link_note"] == "His campaigns for governor."
    assert row["link_method"] == "official roster"


def rollup(c) -> dict:
    MoneySync(c, StubPDC()).rebuild_rollups()
    row = c.execute("SELECT rollup FROM money_people WHERE id = 1").fetchone()["rollup"]
    c.commit()
    return row


@needs_db
def test_top_employers_leave_out_donors_own_names_and_small_groups(conn):
    person(conn)
    campaign(conn)
    gift(conn, 1, "MURR MICHAEL", employer="Michael Murr", amount=4400)
    gift(conn, 2, "MURR MICHAEL", employer="Michael Murr LLC", amount=4400)
    for i, donor in enumerate(("ANN ONE", "BEN TWO", "CAL THREE")):
        gift(conn, 10 + i, donor, employer="Boeing")
        gift(conn, 20 + i, donor, employer="REQUESTED", amount=500)
        gift(conn, 30 + i, donor, employer="CEO", amount=500)
    gift(conn, 40, "ANN ONE", employer="Small Shop", amount=900)
    gift(conn, 41, "BEN TWO", employer="Small Shop", amount=900)
    conn.commit()
    assert [e["employer"] for e in rollup(conn)["top_employers"]] == ["Boeing"]


@needs_db
def test_a_firm_stays_when_others_work_there_but_its_namesakes_gifts_do_not_count(conn):
    person(conn)
    campaign(conn)
    gift(conn, 1, "MURR MICHAEL", employer="Michael Murr LLC", amount=4400)
    for i, donor in enumerate(("ANN ONE", "BEN TWO", "CAL THREE")):
        gift(conn, 10 + i, donor, employer="Michael Murr LLC")
    conn.commit()
    assert [(e["employer"], e["total"], e["n"]) for e in rollup(conn)["top_employers"]] == [("Michael Murr LLC", 300.0, 3)]


@needs_db
def test_top_payees_leave_out_refunded_donors_and_lump_sum_lines(conn):
    person(conn)
    campaign(conn)
    paid(conn, 1, "MOLEN BRUCE", category="Contribution Refunds", description="CONTRIBUTION REFUND")
    paid(conn, 2, "Sembhi Gurdeep", category="Other", description="Contribution refund")
    paid(conn, 3, "EXPENSES OF $50 OR LESS", itemized="Non-itemized")
    paid(conn, 4, "CREDIT EXPENSES OF $200.00 OR LESS")
    paid(conn, 5, "OR LESS EXPENSES OF $50")
    paid(conn, 6, "PAST EXPENSES")
    paid(conn, 7, "KXLY")
    paid(conn, 8, "TEXT FOR LESS")
    paid(conn, 9, "EXPENSE REDUCTION PARTNERS")
    conn.commit()
    assert {p["name"] for p in rollup(conn)["top_payees"]} == {"KXLY", "TEXT FOR LESS", "EXPENSE REDUCTION PARTNERS"}


@needs_db
def test_people_are_not_listed_as_organizations_and_organizations_are(conn):
    person(conn)
    campaign(conn)
    orgs = {1: "SHIRLEY KEEN", 2: "KEEN, SHIRLEY", 3: "Microsoft", 4: "Microsoft", 5: "26th Legislative District Democrats",
            6: "Wells Fargo", 7: "515 Park Place LLC"}
    for i, name in orgs.items():
        gift(conn, i, name, org=True)
    gift(conn, 10, "Microsoft")     # One filer's mistake: Microsoft stays.
    for i in (11, 12):
        gift(conn, i, "26th Legislative District Democrats")    # Mostly filed as individuals, but the name says otherwise.
        gift(conn, i + 10, "Wells Fargo")       # Mostly filed as individuals, with nothing in the name to say otherwise.
    conn.commit()
    expected = {"MICROSOFT", "26TH LEGISLATIVE DISTRICT DEMOCRATS", "515 PARK PLACE"}
    assert {o["key"] for o in rollup(conn)["top_orgs"]} == expected
    assert {r["key"] for r in conn.execute("SELECT key FROM money_orgs")} == expected


# ---- The long-running scheduler --------------------------------------------------------------

class CyclesDone(BaseException):
    """Ends schedule()'s endless loop. Not an Exception, so its own error handling can't catch it."""


class Scheduler:
    """Runs wapeople.sync.schedule() on a fake clock, for a set number of 15-minute cycles."""

    def __init__(self, monkeypatch, backfill_pending=False, records_finish=True):
        from wapeople import sync as app_sync

        self.app_sync = app_sync
        self.now, self.cycles_left = 1_000_000.0, 0
        self.courts, self.refreshes = [], []
        self.courts_fail, self.refresh_fails = False, False
        self.finished = None
        self.records_finish = records_finish
        for name, value in dict(
            time=SimpleNamespace(time=lambda: self.now, sleep=self.sleep),
            datetime=SimpleNamespace(now=self.clock),
            migrate=lambda: [], incomplete_bienniums=lambda: [], SYNC_HOUR_LOCAL=0,
            court_backfill_due=lambda: backfill_pending, last_finished=lambda job: self.finished,
            sync_courts=self.sync_courts, refresh=self.refresh, tracked_run=self.tracked_run,
        ).items():
            monkeypatch.setattr(app_sync, name, value)

    def clock(self):
        return datetime.fromtimestamp(self.now, timezone.utc)

    def sleep(self, seconds):
        self.now += seconds
        if seconds == 900:
            self.cycles_left -= 1
            if not self.cycles_left:
                raise CyclesDone

    def sync_courts(self):
        self.courts.append(self.now)
        if self.courts_fail:
            raise self.app_sync.AuthRequired("no token")

    def refresh(self):
        self.refreshes.append(self.now)
        if self.refresh_fails:
            raise RuntimeError("refresh steps failed: courts")

    @contextmanager
    def tracked_run(self, job, scope=None):
        try:
            yield {}
        finally:
            if self.records_finish:
                self.finished = self.clock()

    def run(self, cycles):
        self.cycles_left = cycles
        with pytest.raises(CyclesDone):
            self.app_sync.schedule()


def test_a_court_failure_does_not_block_the_refresh(monkeypatch):
    s = Scheduler(monkeypatch, backfill_pending=True)
    s.courts_fail = True
    s.run(cycles=6)
    assert len(s.refreshes) == 1
    assert len(s.courts) == 1       # And it waits a few hours before trying the courts again.


def test_court_attempts_resume_after_the_wait(monkeypatch):
    s = Scheduler(monkeypatch, backfill_pending=True)
    s.courts_fail = True
    s.run(cycles=14)        # Three and a half hours.
    assert len(s.courts) == 2


def test_a_working_court_backfill_keeps_going_every_cycle(monkeypatch):
    s = Scheduler(monkeypatch, backfill_pending=True)
    s.run(cycles=4)
    assert len(s.courts) == 4


def test_a_refresh_with_a_failed_step_runs_once_a_day_not_every_few_hours(monkeypatch):
    s = Scheduler(monkeypatch)
    s.refresh_fails = True
    s.run(cycles=88)        # Over 22 hours: only the first refresh, then the next one once 20 hours have passed.
    assert len(s.refreshes) == 2 and s.refreshes[1] - s.refreshes[0] > timedelta(hours=20).total_seconds()


def test_a_refresh_that_finished_is_not_repeated_within_the_day(monkeypatch):
    s = Scheduler(monkeypatch)
    s.run(cycles=40)
    assert len(s.refreshes) == 1


def test_a_refresh_that_recorded_no_finish_waits_a_few_hours_before_another(monkeypatch):
    s = Scheduler(monkeypatch, records_finish=False)
    s.run(cycles=8)
    assert len(s.refreshes) == 1


@needs_db
def test_a_failed_refresh_still_counts_as_finished(conn):
    from wapeople import sync as app_sync

    conn.execute("DELETE FROM sync_runs WHERE job = 'refresh'")
    conn.commit()
    assert app_sync.last_finished("refresh") is None
    with pytest.raises(RuntimeError):
        with app_sync.tracked_run("refresh", "daily"):
            raise RuntimeError("refresh steps failed: courts")
    assert app_sync.last_finished("refresh") is not None
    conn.execute("DELETE FROM sync_runs WHERE job = 'refresh'")
    conn.commit()


def test_a_business_is_judged_by_its_filed_name_not_its_key():
    """The key drops LLC and INC, so judging by the key turns 'WHITNEY LANE LLC' into a person."""
    from wapeople.money.sync import MoneySync

    rows = [{"key": "WHITNEY LANE", "name": "WHITNEY LANE LLC", "n": 3, "as_person": 0},
            {"key": "CANDACE G INAGI", "name": "CANDACE G. INAGI", "n": 1, "as_person": 0},
            {"key": "HOUSE DEMOCRATIC CAMPAIGN COMMITTEE", "name": "HOUSE DEMO CAMP COMM", "n": 9, "as_person": 2}]

    class Conn:
        def execute(self, sql, params=()):
            return type("R", (), {"fetchall": lambda self: rows})()

    ms = MoneySync.__new__(MoneySync)
    ms.conn = Conn()
    assert ms._people_coded_as_orgs() == ["CANDACE G INAGI"]


def test_a_link_resting_only_on_name_and_district_was_removed():
    from wapeople.money.sync import load_roster

    assert "2148" not in load_roster()["manual_links"]
