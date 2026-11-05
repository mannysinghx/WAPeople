"""End-to-end test of the money sync and pages against a throwaway database.

Runs only with TEST_DATABASE_URL set. It empties the tables it uses, so never point it at real data.
"""
import json
import os
import re

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
