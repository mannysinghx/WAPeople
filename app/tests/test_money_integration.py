"""End-to-end test of the money sync and pages against a throwaway database.

Runs only with TEST_DATABASE_URL set. It empties the tables it uses, so never point it at real data.
"""
import json
import os
import re
from contextlib import contextmanager
from datetime import date, datetime, timezone

import pytest

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="TEST_DATABASE_URL not set")

CAMPAIGNS = [
    {"id": "ca-2024-1", "filer_id": "DOEJ 1", "filer_type": "CA", "election_year": "2024", "filer_name": "Jane Doe",
     "office": "STATE SENATOR", "legislative_district": "03", "party_code": "D", "person_id": "501",
     "committee_id": "9001", "reporting_option": "full", "contributions_amount": "3050.00", "carryforward_amount": "0",
     "expenditures_amount": "1200.00", "debts_amount": "100.00", "general_election_status": "Won in general",
     "url": {"url": "https://my.pdc.wa.gov/registration/1"}},
    {"id": "ca-2024-2", "filer_id": "ROEB 2", "filer_type": "CA", "election_year": "2024", "filer_name": "Bob Roe",
     "office": "GOVERNOR", "party_code": "R", "person_id": "777", "committee_id": "9002", "reporting_option": "full",
     "contributions_amount": "500.00", "expenditures_amount": "0", "general_election_status": "Won in general"},
]
CONTRIBUTIONS = [
    {"id": "1", "committee_id": "9001", "election_year": "2024", "report_number": "R1", "origin": "C3", "amount": "2000.00",
     "receipt_date": "2024-05-01T00:00:00.000", "code": "Business", "contributor_category": "Organization",
     "contributor_name": "ACME &AMP; SONS", "contributor_city": "Spokane", "contributor_state": "WA",
     "contributor_address": "1 PRIVATE ST", "url": {"url": "https://my.pdc.wa.gov/public/r/1"}},
    {"id": "2", "committee_id": "9001", "election_year": "2024", "report_number": "R1", "origin": "C3", "amount": "1000.00",
     "receipt_date": "2024-05-02T00:00:00.000", "code": "Individual", "contributor_category": "Individual",
     "contributor_name": "Private Donor", "contributor_city": "Portland", "contributor_state": "OR",
     "contributor_employer_name": "Boeing", "contributor_occupation": "Engineer",
     "contributor_address": "2 PRIVATE ST"},
    {"id": "3", "committee_id": "9001", "election_year": "2024", "report_number": "R2", "origin": "C3", "amount": "50.00",
     "receipt_date": "2024-06-01T00:00:00.000", "code": "Other", "contributor_category": "Other",
     "contributor_name": "SMALL CONTRIBUTIONS"},
    {"id": "4", "committee_id": "9002", "election_year": "2024", "report_number": "R3", "origin": "C3", "amount": "500.00",
     "receipt_date": "2024-07-01T00:00:00.000", "code": "Business", "contributor_category": "Organization",
     "contributor_name": "Acme & Sons", "contributor_city": "Spokane", "contributor_state": "WA"},
]
EXPENDITURES = [
    {"id": "11", "committee_id": "9001", "election_year": "2024", "report_number": "R1", "origin": "A", "amount": "1200.00",
     "expenditure_date": "2024-08-01T00:00:00.000", "code": "Radio advertising", "recipient_name": "KXLY",
     "recipient_city": "Spokane", "recipient_state": "WA", "recipient_address": "3 PRIVATE ST"},
]
DEBTS = [{"id": "21", "committee_id": "9001", "election_year": "2024", "amount": "100.00", "record_type": "DEBT",
          "debt_date": "2024-09-01T00:00:00.000", "code": "Other", "vendor_name": "Printer Co"}]
OUTSIDE = [
    {"id": "o1.entity", "origin": "C6.3 - Identified Entities", "candidate_entity_id": "501", "election_year": "2024",
     "report_number": "C61", "sponsor_id": "S1", "sponsor_name": "Friends PAC", "report_type": "Independent Expenditure Ad",
     "report_date": "2024-10-01T00:00:00.000", "for_or_against": "For", "portion_of_amount": "750.00"},
    {"id": "o2.fund", "origin": "C6.5 - Funding Sources", "report_number": "C61", "sponsor_id": "S1",
     "sponsor_name": "Friends PAC", "funders_name": "Big Union", "funders_city": "Seattle", "funders_state": "WA",
     "amount": "750.00"},
]
SURPLUS = [{"id": "s1", "person_id": "501", "filer_name": "DOE JANE SURPLUS", "raised": "900.00", "spent": "400.00",
            "balance": "500.00"}]
SURPLUS_SPENDING = [{"id": "31", "person_id": "501", "election_year": "2024", "amount": "400.00",
                     "expenditure_date": "2025-01-01T00:00:00.000", "description": "CHARITY", "recipient_name": "Food Bank"}]
DISCLOSURES = [{"id": "41", "person_id": "501", "receipt_date": "2025-04-01T00:00:00.000",
                "period_start": "2024-01-01T00:00:00.000", "period_end": "2024-12-31T00:00:00.000",
                "url": {"url": "https://my.pdc.wa.gov/fa/41"},
                "json": json.dumps({"statement": {"spouse": "Secret Spouse", "income": {"data": [
                    {"who": ["filer"], "name": "Legislature", "why": "Salary", "income": "30000-59999",
                     "address": "4 PRIVATE ST", "city": "Olympia", "state": "WA"}]}}})},
               {"id": "42", "person_id": "501", "receipt_date": "2012-04-01T00:00:00.000",
                "period_start": "2011-01-01T00:00:00.000", "period_end": "2011-12-31T00:00:00.000"}]
DATA = {"3h9x-7bvm": CAMPAIGNS, "kv7h-kjye": CONTRIBUTIONS, "tijg-9zyp": EXPENDITURES, "3r6b-hsaa": DEBTS,
        "d2ig-r3q4": [], "8bva-rkeb": [], "67cp-h962": OUTSIDE, "3cbn-54c3": SURPLUS, "ti55-mvy5": SURPLUS_SPENDING,
        "ehbc-shxw": DISCLOSURES}


class FakePDC:
    """Answers the handful of SoQL shapes the sync sends, from the rows above."""

    def __init__(self):
        self.requests = []

    def close(self):
        pass

    @staticmethod
    def _matches(row, where):
        for field, values in re.findall(r"(\w+) in\(([^)]*)\)", where or ""):
            if str(row.get(field)) not in re.findall(r"'([^']*)'", values):
                return False
        for field, value in re.findall(r"(\w+)='([^']*)'", where or ""):
            if str(row.get(field)) != value:
                return False
        for field, value in re.findall(r"(\w+) like '([^']*)%'", where or ""):
            if not str(row.get(field) or "").startswith(value):
                return False
        return True

    def rows(self, dataset, where, select, order="id"):
        self.requests.append((dataset, select))
        keep = set(select.split(","))
        for r in DATA[dataset]:
            if self._matches(r, where):
                yield {k: v for k, v in r.items() if k in keep}

    def get(self, dataset, **p):
        self.requests.append((dataset, p.get("select")))
        groups = {}
        for r in DATA[dataset]:
            if self._matches(r, p.get("where")):
                key = (r.get("committee_id"), r.get("election_year"))
                n, s = groups.get(key, (0, 0.0))
                groups[key] = (n + 1, s + float(r.get("amount") or 0))
        if "contributor_category" in (p.get("select") or "") or "contributor_name" in (p.get("select") or ""):
            return []
        return [{"committee_id": k[0], "election_year": k[1], "n": str(v[0]), "s": f"{v[1]:.2f}"} for k, v in groups.items()]


@pytest.fixture(scope="module")
def client():
    import psycopg
    from psycopg.rows import dict_row

    from wapeople import db, migrate
    from wapeople.money import sync as money_sync

    db.DATABASE_URL = TEST_DB
    migrate.connect = lambda: psycopg.connect(TEST_DB, row_factory=dict_row)
    migrate.migrate()
    conn = psycopg.connect(TEST_DB, row_factory=dict_row)
    conn.execute("TRUNCATE members, bienniums, money_people, money_orgs, money_committee_sources, money_outside_funders CASCADE")
    conn.execute("INSERT INTO bienniums (id, start_year, end_year, status, bills_synced_at) VALUES ('2025-26', 2025, 2026, 'adjourned_final', now())")
    conn.execute("INSERT INTO members (id, name, first_name, last_name, agency, party, district, latest_biennium)"
                 " VALUES (100, 'Jane Doe', 'Jane', 'Doe', 'Senate', 'D', '3', '2025-26'),"
                 " (200, 'Old Timer', 'Old', 'Timer', 'House', 'R', '9', '2025-26')")
    conn.execute("INSERT INTO member_terms (member_id, biennium, agency, party, district) VALUES"
                 " (100, '2025-26', 'Senate', 'D', '3'), (200, '2025-26', 'House', 'R', '9')")
    conn.commit()
    original = money_sync.load_roster
    money_sync.load_roster = lambda: {"officials": [
        {"person_id": 777, "name": "Bob Roe", "title": "Governor", "kind": "statewide", "source_url": "https://governor.wa.gov/"}
    ], "manual_links": {}}
    fake = FakePDC()
    stats = money_sync.MoneySync(conn, fake).run()
    money_sync.load_roster = original

    from starlette.testclient import TestClient

    from wapeople.web.main import app

    tc = TestClient(app, base_url="https://testserver")
    tc.stats, tc.conn, tc.fake = stats, conn, fake
    yield tc
    conn.close()


def test_itemized_rows_match_the_source_totals(client):
    assert client.stats["money_contributions"]["mismatched"] == 0
    assert client.stats["money_contributions"]["rows_total"] == 4
    row = client.conn.execute("SELECT total_raised, total_itemized, in_state, out_of_state, not_stated, top_source FROM money_people WHERE id = 100").fetchone()
    assert float(row["total_raised"]) == 3050 and float(row["total_itemized"]) == 3050
    assert float(row["in_state"]) == 2000 and float(row["out_of_state"]) == 1000 and float(row["not_stated"]) == 50
    assert row["top_source"] == "Businesses"


def test_a_second_run_reloads_nothing(client):
    from wapeople.money.sync import ITEM_SETS, MoneySync

    ms = MoneySync(client.conn, FakePDC())
    stats = ms.sync_items(ITEM_SETS[0])
    assert stats["committees_reloaded"] == 0 and stats["mismatched"] == 0


def test_changed_source_reloads_only_that_campaign(client):
    from wapeople.money.sync import ITEM_SETS, MoneySync

    added = {"id": "5", "committee_id": "9002", "election_year": "2024", "amount": "25.00", "code": "Individual",
             "contributor_category": "Individual", "contributor_name": "New Donor"}
    CONTRIBUTIONS.append(added)
    try:
        stats = MoneySync(client.conn, FakePDC()).sync_items(ITEM_SETS[0])
        assert stats["committees_reloaded"] == 1 and stats["rows_loaded"] == 2 and stats["mismatched"] == 0
    finally:
        CONTRIBUTIONS.remove(added)
    stats = MoneySync(client.conn, FakePDC()).sync_items(ITEM_SETS[0])
    assert stats["rows_total"] == 4


def test_nobody_is_linked_without_a_matching_record(client):
    assert client.stats["people_linked"] == 2
    person = client.conn.execute("SELECT pdc_person_ids, link_note FROM money_people WHERE id = 200").fetchone()
    assert person["pdc_person_ids"] == [] and "No matching campaign" in person["link_note"]


def test_private_fields_never_reach_the_database(client):
    for table in ("money_contributions", "money_expenditures", "money_disclosures"):
        columns = [r["column_name"] for r in client.conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = %s", (table,))]
        assert not any("address" in c or "phone" in c or "email" in c for c in columns)
    details = client.conn.execute("SELECT details::text AS d FROM money_disclosures WHERE id = 41").fetchone()["d"]
    assert "PRIVATE" not in details and "Secret Spouse" not in details and "Legislature" in details


def test_index_lists_officials_with_totals(client):
    r = client.get("/money")
    assert r.status_code == 200
    assert "Jane Doe" in r.text and "$3,050" in r.text and "Bob Roe" in r.text and "Governor" in r.text
    assert "Bob Roe" not in client.get("/money?group=senate").text
    assert "Jane Doe" not in client.get("/money?group=statewide").text


def test_person_views_render(client):
    pages = {v: client.get(f"/money/100?view={v}") for v in
             ("overview", "contributions", "spending", "debts", "outside", "surplus", "disclosures")}
    assert all(r.status_code == 200 for r in pages.values())
    assert "Businesses" in pages["overview"].text and "Radio advertising" in pages["overview"].text
    assert "ACME & SONS" in pages["contributions"].text.replace("&amp;", "&")
    assert "Private Donor" in pages["contributions"].text and "Boeing" in pages["contributions"].text
    assert "KXLY" in pages["spending"].text and "Printer Co" in pages["debts"].text
    assert "Friends PAC" in pages["outside"].text and "Big Union" in pages["outside"].text
    assert "Food Bank" in pages["surplus"].text
    assert "Legislature" in pages["disclosures"].text and "$30,000–$59,999" in pages["disclosures"].text


def test_addresses_appear_nowhere(client):
    for path in ("/money/100?view=contributions", "/money/100?view=spending", "/money/100?view=disclosures"):
        text = client.get(path).text
        assert "PRIVATE ST" not in text and "Secret Spouse" not in text


def test_private_donor_pages_are_kept_out_of_search(client):
    assert 'name="robots" content="noindex"' in client.get("/money/100?view=contributions").text
    assert "noindex" not in client.get("/money/100").text
    assert "Private Donor" not in client.get("/money/100").text
    assert "Disallow: /money/*?" in client.get("/robots.txt").text


def test_filters_narrow_the_list(client):
    assert "Private Donor" not in client.get("/money/100?view=contributions&source=Businesses").text
    assert "Private Donor" in client.get("/money/100?view=contributions&state=OUT").text
    assert "ACME" not in client.get("/money/100?view=contributions&q=boeing").text
    assert "Private Donor" not in client.get("/money/100?view=contributions&min=1500").text
    assert "Private Donor" not in client.get("/money/100?view=contributions&year=2020").text
    # Nonsense filter values are ignored instead of failing.
    assert client.get("/money/100?view=contributions&year=abc&min=abc&sort=DROP").status_code == 200
    assert client.get("/money/100?view=nope&page=-5").status_code == 200


def test_organization_groups_spellings_and_shows_recipients(client):
    r = client.get("/money/organizations/ACME-AND-SONS")
    assert r.status_code == 200
    assert "Jane Doe" in r.text and "Bob Roe" in r.text and "$2,500" in r.text
    assert client.get("/money/organizations?q=acme").status_code == 200


def test_individuals_have_no_organization_page(client):
    assert client.get("/money/organizations/PRIVATE-DONOR").status_code == 404
    r = client.get("/money/organizations?q=Private+Donor")
    assert "No organizations match" in r.text and "/money/organizations/PRIVATE" not in r.text


def test_unknown_people_are_404_and_empty_people_explain_why(client):
    assert client.get("/money/999999").status_code == 404
    r = client.get("/money/200")
    assert r.status_code == 200 and "No campaign records on file" in r.text


def test_sitemap_and_head_and_canonical(client):
    assert client.head("/money").status_code == 200
    index = client.get("/sitemap.xml")
    assert index.status_code == 200 and "sitemap-people.xml" in index.text
    people = client.get("/sitemap-people.xml").text
    assert "/money/100<" in people and "/legislators/100<" in people and "/money/organizations/ACME-AND-SONS<" in people
    assert client.get("/sitemap-nope.xml").status_code == 404
    assert client.get("/favicon.ico").status_code == 200
    assert '<link rel="canonical" href="https://testserver/money/100">' in client.get("/money/100?view=spending").text
    assert 'property="og:title"' in client.get("/money").text


def test_an_empty_source_response_erases_nothing(client):
    from wapeople.money.sync import MoneySync, SuspiciousSourceData

    class EmptyPDC(FakePDC):
        def rows(self, dataset, where, select, order="id"):
            return iter(())

    before = client.conn.execute("SELECT count(*) AS n FROM money_contributions").fetchone()["n"]
    client.conn.commit()
    with pytest.raises(SuspiciousSourceData):
        MoneySync(client.conn, EmptyPDC()).run()
    client.conn.rollback()
    assert client.conn.execute("SELECT count(*) AS n FROM money_contributions").fetchone()["n"] == before
    links = client.conn.execute("SELECT count(*) AS n FROM money_people WHERE cardinality(pdc_person_ids) > 0").fetchone()["n"]
    assert links == 2
    client.conn.commit()


def test_repair_restores_a_real_name_and_drops_the_empty_term(client):
    from wapeople.legislature.sync import LegislatureSync

    class Roster:
        def members(self, biennium):
            assert biennium == "2025-26"
            return [{"Id": "300", "Name": "Ann Rivers", "FirstName": "Ann", "LastName": "Rivers", "Agency": "Senate",
                     "Party": "R", "District": "18"}]

    c = client.conn
    c.execute("INSERT INTO bienniums (id, start_year, end_year, status) VALUES ('2027-28', 2027, 2028, 'closed') ON CONFLICT DO NOTHING")
    c.execute("INSERT INTO members (id, name, agency, latest_biennium) VALUES (300, 'Senator', 'Senate', '2027-28')")
    c.execute("INSERT INTO member_terms (member_id, biennium, agency, party, district, long_name) VALUES"
              " (300, '2025-26', 'Senate', 'R', '18', 'Senator Rivers'),"
              " (300, '2027-28', 'Senate', NULL, NULL, 'Senator')")
    c.commit()
    leg = LegislatureSync.__new__(LegislatureSync)
    leg.conn, leg.client = c, Roster()
    stats = leg.repair_members()
    assert stats["member_names_restored"] == 1 and stats["member_terms_removed"] == 1
    m = c.execute("SELECT name, party, district, latest_biennium FROM members WHERE id = 300").fetchone()
    assert m == {"name": "Ann Rivers", "party": "R", "district": "18", "latest_biennium": "2025-26"}
    terms = c.execute("SELECT biennium FROM member_terms WHERE member_id = 300").fetchall()
    assert [t["biennium"] for t in terms] == ["2025-26"]
    assert leg.repair_members()["member_names_restored"] == 0      # nothing left to do
    c.execute("DELETE FROM members WHERE id = 300")
    c.execute("DELETE FROM bienniums WHERE id = '2027-28'")
    c.commit()


def test_a_person_gets_no_street_address_but_a_business_keeps_its_name(client):
    from wapeople.money.sync import _contribution

    base = {"id": "9", "committee_id": "9001", "election_year": "2024", "amount": "5.00"}
    person = _contribution({**base, "code": "Individual", "contributor_category": "Individual",
                            "contributor_name": "Pat Doe 123 Main St", "contributor_city": "123 Main St",
                            "contributor_zip": "+90", "contributor_employer_name": "13420 339TH AVE SE"}, 100)
    assert "123 Main" not in " ".join(str(v) for v in person)
    assert "13420" not in " ".join(str(v) for v in person)
    business = _contribution({**base, "code": "Business", "contributor_category": "Organization",
                              "contributor_name": "515 Park Place LLC"}, 100)
    assert "515 Park Place LLC" in business


def test_shorthand_committee_names_show_their_full_name(client):
    from wapeople.money.sync import MoneySync

    c = client.conn
    c.execute("INSERT INTO money_contributions (id, person_pk, committee_id, election_year, amount, source_type, is_org,"
              " contributor_name, contributor_key) VALUES (777001, 100, 9001, 2024, 10, 'Legislative caucuses', true,"
              " 'HOUSE DEMO CAMP COMM', 'HOUSE DEMOCRATIC CAMPAIGN COMMITTEE')")
    c.commit()
    MoneySync(c, FakePDC()).rebuild_rollups()
    name = c.execute("SELECT name FROM money_orgs WHERE key = 'HOUSE DEMOCRATIC CAMPAIGN COMMITTEE'").fetchone()["name"]
    assert name == "HOUSE DEMOCRATIC CAMPAIGN COMMITTEE"
    rollup = c.execute("SELECT rollup FROM money_people WHERE id = 100").fetchone()["rollup"]
    assert any(o["name"] == "HOUSE DEMOCRATIC CAMPAIGN COMMITTEE" for o in rollup["top_orgs"])
    c.execute("DELETE FROM money_contributions WHERE id = 777001")
    c.commit()
    MoneySync(c, FakePDC()).rebuild_rollups()


# ---- Fixes from the November 2026 review ------------------------------------------------------

FED, FED_MEMBER = 2000555, 400


@pytest.fixture
def federal(client):
    """A member of Congress who also has state records and once served in the Legislature."""
    c = client.conn
    c.execute("INSERT INTO members (id, name, first_name, last_name, agency, party, district, latest_biennium)"
              " VALUES (%s, 'Fed Person', 'Fed', 'Person', 'House', 'D', '5', '2025-26')", (FED_MEMBER,))
    c.execute("INSERT INTO money_people (id, member_id, name, kind, title, party, is_current, fec_candidate_ids, fed_cycles,"
              " fed_raised, fed_spent, total_raised, total_spent, n_campaigns, source_url) VALUES"
              " (%s, %s, 'Fed Person', 'federal', 'U.S. Representative', 'D', true, '{H0XX00000}', 2, 4751762.65, 100,"
              " 1275465.52, 900000, 1, 'https://clerk.house.gov/')", (FED, FED_MEMBER))
    c.commit()
    yield FED
    c.rollback()
    c.execute("DELETE FROM fec_contributions WHERE person_pk = %s", (FED,))
    c.execute("DELETE FROM money_people WHERE id = %s", (FED,))
    c.execute("DELETE FROM members WHERE id = %s", (FED_MEMBER,))
    c.commit()


class CountingPool:
    """Stands in for the pool and records how many connections one request holds at once."""

    def __init__(self, real):
        self.real, self.in_use, self.peak, self.checkouts = real, 0, 0, 0

    @contextmanager
    def connection(self):
        self.in_use += 1
        self.checkouts += 1
        self.peak = max(self.peak, self.in_use)
        try:
            with self.real.connection() as c:
                yield c
        finally:
            self.in_use -= 1


def test_no_page_holds_two_pooled_connections(client, federal, monkeypatch):
    """Holding one connection while render() waits for another froze the site at ten overlapping requests."""
    from wapeople import db

    counting = CountingPool(db.pool())
    monkeypatch.setattr(db, "pool", lambda: counting)
    paths = ["/", "/money", "/money/organizations", "/money/organizations/ACME-AND-SONS", "/money/100", "/legislators",
             "/legislators/100", "/bills", "/votes", "/court", "/about", "/map", "/search?q=Jane", "/contact"]
    paths += [f"/money/100?view={v}" for v in ("contributions", "spending", "debts", "outside", "surplus", "disclosures")]
    paths += [f"/money/{federal}?view={v}" for v in ("overview", "contributions", "spending", "outside")]
    paths += [f"/money/{federal}?records=state"]
    for path in paths:
        counting.peak = counting.checkouts = 0
        r = client.get(path)
        assert r.status_code == 200, path
        assert counting.checkouts >= 1 and counting.peak == 1, path
    # The check can fail: a nested checkout is exactly what it looks for.
    counting.peak = 0
    with db.conn(), db.conn():
        pass
    assert counting.peak == 2


def test_state_and_federal_money_are_never_added(client, federal):
    from wapeople.web import money_queries as M

    money = M.legislator_money(client.conn, FED_MEMBER)
    client.conn.commit()
    assert float(money["state_raised"]) == 1275465.52 and float(money["fed_raised"]) == 4751762.65
    assert set(money) == {"id", "state_raised", "fed_raised", "n_campaigns", "fed_cycles"}
    text = client.get(f"/legislators/{FED_MEMBER}").text
    assert "$6.0M" not in text and "6,027,228" not in text
    assert "$4.8M" in text and "$1.3M" in text      # each figure shown on its own


def test_people_rank_by_the_figure_the_table_shows(client, federal):
    """A federal member ranks by the FEC figure alone, not FEC plus PDC."""
    c = client.conn
    c.execute("INSERT INTO money_people (id, name, kind, title, is_current, total_raised, total_spent, n_campaigns) VALUES"
              " (2000556, 'Statewide Spender', 'statewide', 'Auditor', true, 5000000, 950000, 1)")
    c.commit()
    try:
        raised = client.get("/money?sort=raised").text
        assert raised.index("Statewide Spender") < raised.index("Fed Person")      # 5.0M vs 4.75M, not 6.0M
        spent = client.get("/money?sort=spent").text
        assert spent.index("Statewide Spender") < spent.index("Fed Person")        # 950K vs 100, not 900,100
    finally:
        c.execute("DELETE FROM money_people WHERE id = 2000556")
        c.commit()


def test_contribution_count_is_people_in_office(client):
    from wapeople.web import money_queries as M

    c = client.conn
    before = M.overview(c)["contributions"]
    c.execute("INSERT INTO money_people (id, name, kind, is_current, n_contributions, fed_contributions) VALUES"
              " (2000557, 'Former Person', 'legislator', false, 1000, 500)")
    c.commit()
    try:
        assert M.overview(c)["contributions"] == before
    finally:
        c.rollback()
        c.execute("DELETE FROM money_people WHERE id = 2000557")
        c.commit()


@pytest.mark.parametrize("url", [
    "/money/100?view=contributions&year=99999999999", "/money/100?view=contributions&year=2147483648",
    "/money/100?view=contributions&year=%C2%B2", "/money/100?view=spending&year=%C2%B2",
    "/money/100?view=disclosures&statement=%C2%B2", "/money/100?view=disclosures&statement=99999999999",
    f"/money/100?view=spending&year={'9' * 5000}",
    "/court?year_from=%C2%B2", "/court?year_from=99999999999", "/court?year_to=999999", "/court?year_from=2020&year_to=2021",
])
def test_bad_numeric_filters_are_ignored(client, url):
    assert client.get(url).status_code == 200


@pytest.mark.parametrize("view, field", [("contributions", "cycle"), ("spending", "cycle")])
def test_bad_numeric_filters_are_ignored_on_federal_pages(client, federal, view, field):
    for value in ("99999999999", "%C2%B2", "abc"):
        assert client.get(f"/money/{federal}?view={view}&{field}={value}").status_code == 200


@pytest.mark.parametrize("url", ["/money?q=%00", "/money/organizations?source=%00", "/money/organizations/a%00b",
                                 "/money/100?view=contributions&donor=%00", "/search?q=%00", "/bills?q=%00"])
def test_a_nul_in_a_url_is_a_400(client, url):
    r = client.get(url)
    assert r.status_code == 400
    assert r.headers["x-content-type-options"] == "nosniff"     # still gets the security headers


@pytest.mark.parametrize("url", ["/money/100?view=contributions&page=abc", "/money/100?page=1.5", "/money/organizations?page=abc",
                                 "/legislators/abc", "/votes/abc", "/court/abc", "/bills?sponsor=abc", "/money/abc",
                                 "/money/" + "9" * 5000, "/legislators/" + "9" * 5000])
def test_bad_values_get_the_normal_404_page(client, url):
    r = client.get(url)
    assert r.status_code == 404 and r.headers["content-type"].startswith("text/html")
    assert "find that page" in r.text


@pytest.mark.parametrize("url", ["/money/99999999999999999999", "/legislators/99999999999999999999",
                                 "/votes/99999999999999999999", "/court/99999999999999999999",
                                 "/bills/2025-26/HB-99999999999", "/bills/2025-26/HB-" + "9" * 5000])
def test_ids_too_big_to_exist_are_a_404(client, url):
    assert client.get(url).status_code == 404


def test_huge_sponsor_and_page_numbers_are_handled(client):
    assert client.get("/bills?sponsor=99999999999999999999").status_code == 200
    assert client.get("/money/100?view=contributions&page=99999999999999999999").status_code == 200


def test_a_bad_form_token_gets_the_form_again_not_a_crash(client):
    form = {"topic": "other", "message": "This is a long enough message to pass the check."}
    for token in ("1.é", "9" * 400 + ".abc", "².abc"):
        r = client.post("/contact", data={**form, "token": token}, follow_redirects=False)
        assert r.status_code == 400 and "form expired" in r.text


def test_a_page_past_the_end_shows_the_last_page(client):
    r = client.get("/money/100?view=contributions&page=999999")
    assert r.status_code == 200 and "Private Donor" in r.text and "No contributions match" not in r.text
    assert "Printer Co" in client.get("/money/100?view=debts&page=999999").text
    assert "/money/organizations/ACME-AND-SONS" in client.get("/money/organizations?page=999999").text
    assert "KXLY" in client.get("/money/100?view=spending&page=999999").text
    assert "Food Bank" in client.get("/money/100?view=surplus&page=999999").text


def test_anothers_statement_id_falls_back_to_this_persons_newest(client):
    for statement in ("999999", "0", "abc"):
        r = client.get(f"/money/100?view=disclosures&statement={statement}")
        assert "Legislature" in r.text and "No statement here" not in r.text


def test_a_year_that_was_not_applied_is_not_in_the_heading(client):
    for year, shown in (("abc", "in abc"), ("0", "in 0"), ("99999999999", "in 9")):
        text = client.get(f"/money/100?view=contributions&year={year}").text
        assert "Top individual donors" in text and shown not in text and "Private Donor" in text
    assert "Top individual donors in 2024" in client.get("/money/100?view=contributions&year=2024").text


def _fec_gift(c, sub_id, person, name, amount, cycle=2024):
    c.execute("INSERT INTO fec_contributions (sub_id, person_pk, committee_id, cycle, dataset, source_type, is_memo, is_refund,"
              " name, amount) VALUES (%s, %s, 'C00000001', %s, 'indiv', 'Individuals (itemized)', false, false, %s, %s)",
              (sub_id, person, cycle, name, amount))


def test_a_cycle_that_was_not_applied_is_not_in_the_heading(client, federal):
    _fec_gift(client.conn, 990001, federal, "SOMEONE, ANY", 500)
    client.conn.commit()
    text = client.get(f"/money/{federal}?view=contributions&cycle=abc").text
    assert "Top individual donors" in text and "in abc" not in text
    assert "Top individual donors in 2024" in client.get(f"/money/{federal}?view=contributions&cycle=2024").text


def test_federal_donors_are_not_split_by_letter_case(client, federal):
    from wapeople.web import fec_queries as F

    c = client.conn
    _fec_gift(c, 990002, federal, "ROBINS, MICHAEL", 38722)
    _fec_gift(c, 990003, federal, "Robins, Michael", 2000)
    _fec_gift(c, 990004, federal, "OTHER, PERSON", 39000)
    c.commit()
    rows = F.top_individuals(c, federal)
    c.commit()
    assert [(r["name"].upper(), float(r["total"]), r["n"]) for r in rows] == [("ROBINS, MICHAEL", 40722.0, 2), ("OTHER, PERSON", 39000.0, 1)]


def test_links_to_state_records_use_records_state(client, federal):
    text = client.get(f"/money/{federal}").text
    assert f'href="/money/{federal}?records=state"' in text and "?source=state" not in text
    state = client.get(f"/money/{federal}?records=state").text
    assert "Earlier state campaigns" in state and "No campaign records on file" not in state


def test_org_recipients_say_who_also_has_federal_records(client, federal):
    """An organization's gifts are state records, so the link has to land on that person's state page."""
    from wapeople.web import money_queries as M

    c = client.conn
    org = M.organization(c, "ACME AND SONS")
    assert all("fec_candidate_ids" in r for r in org["recipients"]) and org["recipients"]
    c.execute("INSERT INTO money_contributions (id, person_pk, committee_id, election_year, amount, source_type, is_org,"
              " contributor_name, contributor_key) VALUES (777002, %s, 9001, 2024, 10, 'Businesses', true,"
              " 'ACME & SONS', 'ACME AND SONS')", (federal,))
    c.commit()
    try:
        page = client.get("/money/organizations/ACME-AND-SONS").text
        assert f"/money/{federal}?records=state&amp;view=contributions&amp;donor=ACME%20AND%20SONS" in page
        assert "/money/100?view=contributions&amp;donor=" in page       # state-only people need no such flag
        filtered = client.get(f"/money/{federal}?records=state&view=contributions&donor=ACME%20AND%20SONS").text
        assert "Showing one donor only" in filtered
    finally:
        c.rollback()
        c.execute("DELETE FROM money_contributions WHERE id = 777002")
        c.commit()


def test_surplus_paid_is_the_sum_of_surplus_spending(client):
    from wapeople.web import money_queries as M

    assert float(M.surplus_paid(client.conn, 100)) == 400
    assert float(M.surplus_paid(client.conn, 200)) == 0
    client.conn.commit()


def test_spending_tabs_are_kept_out_of_search(client, federal):
    assert 'name="robots" content="noindex"' in client.get("/money/100?view=spending").text
    assert 'name="robots" content="noindex"' in client.get(f"/money/{federal}?view=spending").text
    assert "noindex" not in client.get(f"/money/{federal}").text


def test_one_gift_on_many_reports_counts_once(client):
    from wapeople.web import money_queries as M

    c = client.conn
    c.execute("INSERT INTO money_outside (id, person_pk, report_number, sponsor_id, sponsor_name, amount) VALUES"
              " ('o-a', 100, 'RA', 'S9', 'Repeat PAC', 10), ('o-b', 100, 'RB', 'S9', 'Repeat PAC', 10)")
    c.execute("INSERT INTO money_outside_funders (id, report_number, sponsor_id, sponsor_name, funder_name, amount, date_received) VALUES"
              " ('f1', 'RA', 'S9', 'Repeat PAC', 'Same Giver', 200, '2016-10-11'),"
              " ('f2', 'RB', 'S9', 'Repeat PAC', 'Same Giver', 200, '2016-10-11'),"
              " ('f3', 'RB', 'S9', 'Repeat PAC', 'Same Giver', 200, '2016-10-12'),"
              " ('f4', 'RB', 'S9', 'Repeat PAC', 'Same Giver', 75, '2016-10-12')")
    c.commit()
    try:
        funders = {f["funder_name"]: f for f in M.outside(c, 100)["funders"]}
        assert float(funders["Same Giver"]["total"]) == 475 and funders["Same Giver"]["n"] == 3
        assert float(funders["Big Union"]["total"]) == 750
        assert "$475" in client.get("/money/100?view=outside").text
    finally:
        c.rollback()
        c.execute("DELETE FROM money_outside_funders WHERE id IN ('f1', 'f2', 'f3', 'f4')")
        c.execute("DELETE FROM money_outside WHERE id IN ('o-a', 'o-b')")
        c.commit()


def test_person_shaped_names_and_net_negative_orgs(client):
    """A person a filer coded as a business stays out of search engines and the sitemap. An organization
    with a net total of zero or less didn't 'give', so it isn't listed, though its page still works."""
    from wapeople.web import money_queries as M

    c = client.conn
    orgs_before = M.overview(c)["orgs"]
    c.execute("INSERT INTO money_orgs (key, name, source_type, total, n_gifts, n_people) VALUES"
              " ('CANDACE G INAGI', 'CANDACE G. INAGI', 'Businesses', 100, 1, 1),"
              " ('INAGI CANDACE', 'INAGI, CANDACE', 'Businesses', 90, 1, 1),"
              " ('SURPLUS FUNDS BACK', 'SURPLUS FUNDS BACK LLC', 'Businesses', -76636, 1, 1),"
              " ('515 PARK PLACE', '515 Park Place LLC', 'Businesses', 25, 1, 1)")
    c.commit()
    try:
        assert M.overview(c)["orgs"] == orgs_before + 3         # the net-negative one isn't counted
        c.commit()
        sitemap = client.get("/sitemap-people.xml").text
        assert "CANDACE-G-INAGI" not in sitemap and "INAGI-CANDACE" not in sitemap and "SURPLUS-FUNDS-BACK" not in sitemap
        assert "/money/organizations/515-PARK-PLACE<" in sitemap and "/money/organizations/ACME-AND-SONS<" in sitemap
        person = client.get("/money/organizations/CANDACE-G-INAGI")
        assert person.status_code == 200 and person.headers["x-robots-tag"] == "noindex"
        assert client.get("/money/organizations/INAGI-CANDACE").headers["x-robots-tag"] == "noindex"
        business = client.get("/money/organizations/515-PARK-PLACE")
        assert business.status_code == 200 and "x-robots-tag" not in business.headers and "noindex" not in business.text
        assert "x-robots-tag" not in client.get("/money/organizations/ACME-AND-SONS").headers
        listing = client.get("/money/organizations").text
        assert "SURPLUS-FUNDS-BACK" not in listing and "515-PARK-PLACE" in listing
        assert client.get("/money/organizations/SURPLUS-FUNDS-BACK").status_code == 200
    finally:
        c.execute("DELETE FROM money_orgs WHERE key IN ('CANDACE G INAGI', 'INAGI CANDACE', 'SURPLUS FUNDS BACK', '515 PARK PLACE')")
        c.commit()


def test_canonical_is_the_records_normal_address(client):
    def canonical(path):
        return re.search(r'<link rel="canonical" href="([^"]+)"', client.get(path).text)[1]

    assert canonical("/money/00100") == "https://testserver/money/100"
    assert canonical("/legislators/0100") == "https://testserver/legislators/100"
    assert canonical("/money/organizations/acme-and-sons") == "https://testserver/money/organizations/ACME-AND-SONS"
    assert canonical("/money") == "https://testserver/money"


def test_footer_dates_mean_data_was_loaded(client):
    from wapeople.web import queries as Q

    c = client.conn
    loaded = datetime(2031, 1, 1, tzinfo=timezone.utc)
    checked = datetime(2032, 1, 1, tzinfo=timezone.utc)
    c.execute("INSERT INTO fec_sync_state (dataset, cycle, etag, rows, loaded_at) VALUES ('testds', 1, 'x', 1, %s)", (loaded,))
    runs = [("fec", checked, "{}"), ("money", loaded, '{"money_contributions": {"rows_loaded": 5}}'),
            ("money", checked, '{"money_contributions": {"rows_loaded": 0}, "people": 3}')]
    ids = [c.execute("INSERT INTO sync_runs (job, scope, finished_at, status, stats) VALUES (%s, 'test', %s, 'ok', %s) RETURNING id",
                     (job, when, stats)).fetchone()["id"] for job, when, stats in runs]
    c.commit()
    try:
        updated = Q.last_updates(c)
        assert updated["fec"] == loaded and updated["money"] == loaded
    finally:
        c.rollback()
        c.execute("DELETE FROM sync_runs WHERE id = ANY(%s)", (ids,))
        c.execute("DELETE FROM fec_sync_state WHERE dataset = 'testds'")
        c.commit()


def test_search_finds_officials_who_never_served_in_the_legislature(client, monkeypatch):
    from wapeople.web import main
    from wapeople.web import money_queries as M

    found = M.search_officials(client.conn, "Bob Roe")
    assert [p["name"] for p in found] == ["Bob Roe"]
    assert M.search_officials(client.conn, "Jane Doe") == []      # already found as a legislator
    client.conn.commit()
    real, seen = main.render, {}

    def spy(request, name, **ctx):
        seen.update(ctx)
        return real(request, name, **ctx)

    monkeypatch.setattr(main, "render", spy)
    assert client.get("/search?q=Bob+Roe").status_code == 200
    assert [p["id"] for p in seen["officials"]] == [found[0]["id"]]
    assert client.get("/search?q=HB+1217").status_code == 200
    assert seen["officials"] == []      # a bill number isn't a name


def test_every_statement_of_a_judge_withholds_property_location(client):
    c = client.conn
    details = {"income": [], "accounts": [], "businesses": [], "creditors": [], "gifts": [], "lobbying": [],
               "real_estate": [{"who": ["Official"], "assessed_value": "750000-999999", "purchased": False, "sold": False,
                                "city": "Gig Harbor", "county": "Pierce", "lenders": []}]}
    c.execute("INSERT INTO money_people (id, name, kind, title, is_current, n_campaigns) VALUES"
              " (2000560, 'Plain Filer', 'statewide', 'Auditor', true, 1), (2000561, 'Later Judge', 'statewide', 'Auditor', true, 1),"
              " (2000562, 'A Justice', 'justice', 'Justice', true, 1)")
    rows = [(990010, 2000560, False, date(2023, 12, 31)), (990011, 2000561, False, date(2023, 12, 31)),
            (990012, 2000561, True, date(2024, 12, 31)), (990013, 2000562, False, date(2023, 12, 31))]
    for sid, person, flagged, end in rows:
        c.execute("INSERT INTO money_disclosures (id, person_pk, period_end, details) VALUES (%s, %s, %s, %s)",
                  (sid, person, end, json.dumps({**details, "location_withheld": flagged})))
    c.commit()
    try:
        assert "Gig Harbor" in client.get("/money/2000560?view=disclosures").text
        for url in ("/money/2000561?view=disclosures&statement=990011", "/money/2000561?view=disclosures&statement=990012",
                    "/money/2000562?view=disclosures"):
            text = client.get(url).text
            assert "Gig Harbor" not in text and "Pierce" not in text and "property locations are left out" in text
    finally:
        c.execute("DELETE FROM money_disclosures WHERE id = ANY(%s)", ([r[0] for r in rows],))
        c.execute("DELETE FROM money_people WHERE id IN (2000560, 2000561, 2000562)")
        c.commit()


def test_left_during_term_needs_the_record_to_show_it(client):
    from wapeople.web import queries as Q

    c = client.conn
    c.execute("INSERT INTO bienniums (id, start_year, end_year, status) VALUES ('2011-12', 2011, 2012, 'closed'),"
              " ('2013-14', 2013, 2014, 'closed') ON CONFLICT DO NOTHING")
    bill = c.execute("INSERT INTO bills (biennium, bill_number, type_code, type_name, bill_id, stage, stage_label, bucket)"
                     " VALUES ('2011-12', 9999, 'B', 'Bill', 'HB 9999', 'law', 'Law', 'past') RETURNING id").fetchone()["id"]
    # id -> (chamber, district, last vote date)
    people = {
        9001: ("Senate", "7", "2011-02-01"), 9002: ("Senate", "7", "2012-03-01"),   # replaced mid-term
        9003: ("Senate", "7", None),                                               # no votes: no label
        9004: ("Senate", "8", "2012-03-01"), 9005: ("Senate", "8", "2012-03-01"),   # tied on the last day
        9006: ("Senate", "9", "2012-02-01"), 9007: ("Senate", "9", "2012-03-01"),   # earlier, but holds the seat next session
        9008: ("House", "7", "2012-03-01"), 9009: ("House", "7", "2012-03-01"), 9010: ("House", "7", "2011-05-01"),  # 3 names, 2 seats
        9011: ("House", "8", "2012-03-01"), 9012: ("House", "8", "2012-02-27"),     # only 2 names for 2 seats
        9013: ("House", "9", "2012-03-01"), 9014: ("House", "9", "2012-02-27"), 9015: ("House", "9", "2012-02-27"),  # tie for the second seat
    }
    roll_ids = []
    for mid, (agency, district, last) in people.items():
        c.execute("INSERT INTO members (id, name, agency, district) VALUES (%s, %s, %s, %s)", (mid, f"Member {mid}", agency, district))
        c.execute("INSERT INTO member_terms (member_id, biennium, agency, party, district) VALUES (%s, '2011-12', %s, 'D', %s)",
                  (mid, agency, district))
        if last:
            roll = c.execute("INSERT INTO roll_calls (bill_pk, biennium, agency, vote_date) VALUES (%s, '2011-12', %s, %s) RETURNING id",
                             (bill, agency, last)).fetchone()["id"]
            roll_ids.append(roll)
            c.execute("INSERT INTO votes (roll_call_id, member_id, vote) VALUES (%s, %s, 'Yea')", (roll, mid))
    c.execute("INSERT INTO member_terms (member_id, biennium, agency, party, district) VALUES (9006, '2013-14', 'Senate', 'D', '9')")
    c.commit()
    try:
        assert Q.left_during_term(c, "2011-12") == {9001, 9010}
        assert Q.left_during_term(c, "not-a-biennium") == set()
        c.commit()
        text = client.get("/legislators?biennium=2011-12&district=7").text
        assert text.count("Left during term") == 2 and "2 left during the term" in text
    finally:
        c.rollback()
        c.execute("DELETE FROM votes WHERE roll_call_id = ANY(%s)", (roll_ids,))
        c.execute("DELETE FROM roll_calls WHERE id = ANY(%s)", (roll_ids,))
        c.execute("DELETE FROM bills WHERE id = %s", (bill,))
        c.execute("DELETE FROM member_terms WHERE member_id BETWEEN 9001 AND 9015")
        c.execute("DELETE FROM members WHERE id BETWEEN 9001 AND 9015")
        c.execute("DELETE FROM bienniums WHERE id IN ('2011-12', '2013-14')")
        c.commit()


def test_sorting_by_spending_shows_the_spending(client):
    assert "Spent, all campaigns" in client.get("/money?sort=spent").text
    assert "Spent, all campaigns" not in client.get("/money").text
