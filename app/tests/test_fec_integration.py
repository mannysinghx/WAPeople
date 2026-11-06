"""End-to-end test of the federal sync and pages against a throwaway database.

Runs only with TEST_DATABASE_URL set. It empties the tables it uses, so never point it at real data.
"""
import os
import re
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest

from wapeople.money.fec_bulk import FecBulk, FileInfo

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="TEST_DATABASE_URL not set")
CAND, CMTE, PAC = "H6WA07458", "C00605592", "C00238725"


def weball(receipts="1000", spent="400", individuals="700", pacs="200"):
    return [CAND, "JAYAPAL, PRAMILA", "I", "1", "DEM", receipts, "0", spent, "0", "50", "650", "0", "0", "0", "0", "0", "0",
            individuals, "WA", "07", "", "", "", "", "", pacs, "0", "12/31/2024", "0", "0"]


def gift(sub, amount, tx="15", memo="", name="PRIVATE, DONOR", state="OR", tran=None):
    return [CMTE, "N", "Q3", "G2024", "2024IMG", tx, "IND", name, "PORTLAND", state, "972041234", "BOEING", "ENGINEER",
            "08052024", amount, "", tran or f"T{sub}", "1", memo, "", str(sub)]


FILES = {
    "weball": [weball()],
    "ccl": [[CAND, "2024", "2024", CMTE, "H", "P", "1"], [CAND, "2024", "2024", "C00999999", "N", "D", "2"]],
    "cm": [[CMTE, "PRAMILA FOR CONGRESS", "SECRET TREASURER", "1 SECRET ST", "", "SEATTLE", "WA", "98101", "P", "H", "DEM", "Q", "", "", CAND],
           ["C00999999", "A LEADERSHIP PAC", "SECRET TREASURER", "2 SECRET ST", "", "SEATTLE", "WA", "98101", "D", "N", "", "Q", "", "", ""],
           [PAC, "BOEING COMPANY PAC", "SECRET TREASURER", "3 SECRET ST", "", "ARLINGTON", "VA", "22202", "B", "Q", "", "M", "C", "THE BOEING COMPANY", ""]],
    "cn": [[CAND, "JAYAPAL, PRAMILA", "DEM", "2024", "WA", "H", "07", "I", "C", CMTE, "", "", "", "", ""]],
    "indiv": [gift(1, "400"), gift(2, "150"), gift(3, "50", tx="22Y"), gift(4, "999", memo="X"), gift(5, "100", state="WA"),
              gift(6, "5", name="SOMEONE, ELSE"), gift(7, "300", tx="15C", name="JAYAPAL, PRAMILA"),
              gift(8, "400", tran="T1")],   # the first gift again, under a new SUB_ID
    "oth": [[CMTE, "N", "Q3", "G2024", "2024IMG", "24K", "PTY", "DCCC", "WASHINGTON", "DC", "20003", "", "", "09012024", "75", "C00000935", "T11", "1", "", "CONTRIBUTION", "11"],
            [CMTE, "N", "Q3", "G2024", "2024IMG", "18G", "PAC", "A JOINT FUND", "SEATTLE", "WA", "98101", "", "", "09012024", "10", "C00888888", "T12", "1", "", "", "12"],
            [CMTE, "N", "Q3", "G2024", "2024IMG", "18K", "PAC", "BOEING COMPANY PAC", "ARLINGTON", "VA", "22202", "", "", "01282024", "200", PAC, "T13", "1", "", "", "13"],
            [CMTE, "N", "Q3", "G2024", "2024IMG", "15J", "IND", "JOINT, GIVER", "SEATTLE", "WA", "98101", "", "", "09012024", "6", "", "T14", "1", "", "", "14"]],
    "oppexp": [[CMTE, "N", "2024", "Q3", "2024IMG", "17", "F3", "SB", "PRINT SHOP", "SEATTLE", "WA", "981011234", "09/01/2024", "300", "G2024", "PRINTING", "", "", "", "", "ORG", "21", "1", "T21", "", ""],
               [CMTE, "N", "2024", "Q3", "2024IMG", "17", "F3", "SB", "PRINT SHOP", "SEATTLE", "WA", "981011234", "09/01/2024", "300", "G2024", "PRINTING SUBITEM", "", "", "X", "", "ORG", "22", "1", "T22", "", ""],
               [CMTE, "N", "2024", "Q3", "2024IMG", "17", "F3", "SB", "PRINT SHOP", "SEATTLE", "WA", "981011234", "09/01/2024", "300", "G2024", "PRINTING", "", "", "", "", "ORG", "23", "1", "T21", "", ""]],
    "pas2": [[PAC, "N", "M2", "G2024", "2024IMG", "24K", "CCM", "PRAMILA FOR CONGRESS", "SEATTLE", "WA", "98101", "", "", "01242024", "200", CMTE, CAND, "T31", "1", "", "CONTRIBUTION", "31"],
             [PAC, "N", "M2", "G2024", "2024IMG", "24E", "ORG", "AD VENDOR", "SEATTLE", "WA", "98101", "", "", "01242024", "5000", "", CAND, "T32", "1", "", "IE", "32"],
             [PAC, "N", "M2", "G2024", "2024IMG", "24K", "CCM", "SOMEONE ELSE", "SEATTLE", "WA", "98101", "", "", "01242024", "7", "C00777777", "H0XX00000", "T33", "1", "", "", "33"],
             [PAC, "N", "M2", "G2024", "2024IMG", "24K", "CCM", "PRAMILA FOR CONGRESS", "SEATTLE", "WA", "98101", "", "", "01242024", "90", CMTE, CAND, "T34", "1", "X", "MEMO", "34"]],
}
IE = [{"cand_id": CAND, "spe_id": "C00111111", "spe_nam": "Big Super PAC", "tran_id": "A", "file_num": "1", "exp_amo": "900",
       "sup_opp": "S", "exp_date": "01-OCT-24", "pur": "TV AD", "pay": "AD VENDOR", "image_num": "2024IE"}]


class FakeBulk:
    """Serves only the cycles it's told to; everything else is 'not published'. Counts how often each file is read."""

    def __init__(self, etag="v1", files=None, missing=(), cycles=("2024",)):
        self.etag, self.files, self.reads, self.missing, self.cycles = etag, files or FILES, [], set(missing), cycles

    def close(self):
        pass

    def info(self, name):
        if name.split("/")[0] in self.cycles and self._key(name) not in self.missing:
            return FileInfo("https://fec.test/" + name, 1, self.etag, None)
        return None

    @staticmethod
    def _key(name):
        return re.sub(r"\d\d\.zip$", "", name.split("/")[1])

    def rows(self, name, wanted=None, info=None):
        self.reads.append(name)
        if self._key(name) in self.missing:
            return iter(())
        return (r for r in self.files[self._key(name)] if wanted is None or r[0].encode() in wanted)

    def csv_rows(self, name, info=None):
        self.reads.append(name)
        return iter(IE)


@pytest.fixture(scope="module")
def client():
    import psycopg
    from psycopg.rows import dict_row
    from starlette.testclient import TestClient

    from wapeople import db, migrate
    from wapeople.money.fec import FecSync

    db.DATABASE_URL = TEST_DB
    migrate.connect = lambda: psycopg.connect(TEST_DB, row_factory=dict_row)
    migrate.migrate()
    conn = psycopg.connect(TEST_DB, row_factory=dict_row)
    request_cleanup = conn.close
    conn.execute("TRUNCATE fec_sync_state, fec_cycles, fec_committees, fec_contributions, fec_disbursements, fec_outside")
    conn.execute("DELETE FROM money_people WHERE id IN (2000099, 2000098)")
    conn.execute("INSERT INTO money_people (id, name, kind, title, party, is_current, fec_candidate_ids, source_url, total_raised, n_campaigns)"
                 " VALUES (2000099, 'Pramila Jayapal', 'federal', 'U.S. Representative, WA-7', 'D', true, %s, 'https://clerk.house.gov/Members/J000298', 54321, 1)",
                 ([CAND],))
    conn.commit()
    try:
        stats = FecSync(conn, FakeBulk()).run(today=date(2024, 11, 5))

        from wapeople.web.main import app

        tc = TestClient(app, base_url="https://testserver")
        tc.stats, tc.conn = stats, conn
        yield tc
        conn.rollback()
        conn.execute("DELETE FROM money_people WHERE id = 2000099")
        conn.commit()
    finally:
        request_cleanup()   # a crash here must not leave locks that block the next test module


def test_reported_totals_are_stored_exactly(client):
    k = client.conn.execute("SELECT * FROM fec_cycles WHERE candidate_id = %s", (CAND,)).fetchone()
    assert float(k["receipts"]) == 1000 and float(k["disbursements"]) == 400 and float(k["cash_end"]) == 650
    p = client.conn.execute("SELECT * FROM money_people WHERE id = 2000099").fetchone()
    assert float(p["fed_raised"]) == 1000 and float(p["fed_spent"]) == 400 and p["fed_cycles"] == 1


def test_totals_skip_memo_entries_and_refunds(client):
    k = client.conn.execute("SELECT itemized_individuals, itemized_committees, itemized_operating FROM fec_cycles").fetchone()
    assert float(k["itemized_individuals"]) == 655     # 400 + 150 + 100 + 5; not the 999 memo or the refund
    assert float(k["itemized_committees"]) == 200      # the 24K gift to this candidate only
    assert float(k["itemized_operating"]) == 300       # not the X subitem


def test_where_it_came_from_adds_up_to_the_reported_total(client):
    roll = client.conn.execute("SELECT fed_rollup FROM money_people WHERE id = 2000099").fetchone()["fed_rollup"]
    assert round(sum(p["total"] for p in roll["by_source"]), 2) == 1000
    assert {p["type"]: p["total"] for p in roll["by_source"]}["Individuals (small gifts, not itemized)"] == 45


def test_largest_source_counts_individuals_as_one(client):
    p = client.conn.execute("SELECT fed_top_source FROM money_people WHERE id = 2000099").fetchone()
    assert p["fed_top_source"] == "Individuals"


def test_a_line_repeated_under_a_new_sub_id_counts_once(client):
    found = {r["sub_id"] for r in client.conn.execute("SELECT sub_id FROM fec_contributions WHERE sub_id IN (1, 8)")}
    assert len(found) == 1
    found = {r["sub_id"] for r in client.conn.execute("SELECT sub_id FROM fec_disbursements WHERE sub_id IN (21, 23)")}
    assert len(found) == 1


def test_gifts_the_giver_reports_and_joint_fundraising_shares_are_not_counted_again(client):
    rows = {r["sub_id"]: r["is_memo"] for r in client.conn.execute(
        "SELECT sub_id, is_memo FROM fec_contributions WHERE sub_id IN (12, 13, 14, 31, 34)")}
    assert rows == {12: False, 13: True, 14: True, 31: False, 34: True}
    assert client.stats["reconciliation"]["cycles_operating_above_reported"] == 0


def test_candidates_own_money_is_not_counted_as_itemized_individuals(client):
    row = client.conn.execute("SELECT source_type FROM fec_contributions WHERE sub_id = 7").fetchone()
    assert row["source_type"] == "The candidate's own money"
    assert float(client.conn.execute("SELECT itemized_individuals FROM fec_cycles").fetchone()["itemized_individuals"]) == 655


def test_older_cycles_without_a_linkage_file_still_find_the_main_committee(client):
    from wapeople.money.fec import FecSync

    c = client.conn
    try:
        committees = FecSync(c, FakeBulk(missing={"ccl"}, cycles=("1998",)))._sync_reference(1998, {CAND: 2000099}, True, date(2024, 11, 5))
        assert committees == {CMTE: 2000099}
    finally:
        c.rollback()
        for table in ("fec_cycles", "fec_committees", "fec_sync_state"):
            c.execute(f"DELETE FROM {table} WHERE cycle = 1998")
        c.commit()


@pytest.mark.parametrize("missing", ["ccl", "cm"])
def test_a_missing_committee_file_stops_the_load_in_later_cycles(client, missing):
    from wapeople.money.fec import FecSync
    from wapeople.money.sync import SuspiciousSourceData

    before = client.conn.execute("SELECT count(*) AS n, count(name) AS named FROM fec_committees").fetchone()
    client.conn.commit()
    with pytest.raises(SuspiciousSourceData):
        FecSync(client.conn, FakeBulk(missing={missing}))._sync_reference(2024, {CAND: 2000099}, True, date(2024, 11, 5))
    assert client.conn.execute("SELECT count(*) AS n, count(name) AS named FROM fec_committees").fetchone() == before
    client.conn.commit()


def test_a_file_that_goes_away_during_the_read_changes_nothing(client):
    from wapeople.money.fec import FecSync

    class Vanishing(FakeBulk):
        def rows(self, name, wanted=None, info=None):
            if info is None:        # the real client looks again, finds nothing and reads as empty
                return iter(())
            raise httpx.HTTPStatusError("403", request=httpx.Request("GET", name), response=httpx.Response(403))

    c = client.conn
    before = c.execute("SELECT etag, rows FROM fec_sync_state WHERE dataset = 'weball' AND cycle = 2024").fetchone()
    c.commit()
    with pytest.raises(httpx.HTTPStatusError):
        FecSync(c, Vanishing(etag="gone"))._sync_reference(2024, {CAND: 2000099}, True, date(2024, 11, 5))
    c.rollback()
    assert c.execute("SELECT count(*) AS n FROM fec_cycles WHERE cycle = 2024").fetchone()["n"] == 1
    assert c.execute("SELECT etag, rows FROM fec_sync_state WHERE dataset = 'weball' AND cycle = 2024").fetchone() == before
    c.commit()


@pytest.mark.parametrize("empty", [{"weball": []}, {"ccl": [], "cn": []}])
def test_a_totals_or_committee_file_that_shrinks_is_refused(client, empty):
    from wapeople.money.fec import FecSync
    from wapeople.money.sync import SuspiciousSourceData

    c = client.conn
    before = [c.execute(f"SELECT count(*) AS n FROM {t}").fetchone()["n"] for t in ("fec_cycles", "fec_committees")]
    c.commit()
    with pytest.raises(SuspiciousSourceData):
        FecSync(c, FakeBulk(files={**FILES, **empty}))._sync_reference(2024, {CAND: 2000099}, True, date(2024, 11, 5))
    c.rollback()
    assert [c.execute(f"SELECT count(*) AS n FROM {t}").fetchone()["n"] for t in ("fec_cycles", "fec_committees")] == before
    c.commit()


def test_a_loaded_file_the_fec_stops_publishing_is_an_error_not_a_quiet_run(client):
    from wapeople.money.fec import FecSync
    from wapeople.money.sync import SuspiciousSourceData

    class Forbidden(FakeBulk):
        def info(self, name):
            return None

    bulk = Forbidden()
    with pytest.raises(SuspiciousSourceData):
        FecSync(client.conn, bulk).run(today=date(2024, 11, 5))
    assert bulk.reads == []


def test_outside_spending_is_not_counted_as_a_gift(client):
    n = client.conn.execute("SELECT count(*) AS n FROM fec_contributions WHERE sub_id = 32").fetchone()["n"]
    assert n == 0
    r = client.conn.execute("SELECT amount, spender_name FROM fec_outside").fetchone()
    assert float(r["amount"]) == 900 and r["spender_name"] == "Big Super PAC"


def test_only_campaign_committees_are_itemized_but_others_are_listed(client):
    cm = {r["committee_id"]: r["is_campaign"] for r in client.conn.execute("SELECT committee_id, is_campaign FROM fec_committees")}
    assert cm == {CMTE: True, "C00999999": False}
    assert "A LEADERSHIP PAC" in client.get("/money/2000099").text


def test_no_private_fields_reach_the_database_or_the_pages(client):
    columns = [r["column_name"] for r in client.conn.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name LIKE 'fec_%'")]
    assert not any(w in c for c in columns for w in ("address", "street", "treasurer", "phone", "email"))
    for view in ("overview", "contributions", "spending", "outside"):
        text = client.get(f"/money/2000099?view={view}").text
        assert "SECRET" not in text and "972041234" not in text


def test_unchanged_files_are_not_read_again(client):
    from wapeople.money.fec import FecSync

    bulk = FakeBulk()
    stats = FecSync(client.conn, bulk).run(today=date(2024, 11, 5))
    assert bulk.reads == [] and stats["files_loaded"] == 0 and stats["files_unchanged"] > 0


def test_a_roster_change_reloads_files_that_have_not_changed(client):
    from wapeople.money.fec import FecSync

    c, new_id = client.conn, "H0WA99999"
    files = {**FILES, "weball": FILES["weball"] + [[new_id, *weball()[1:]]]}
    today = date(2024, 11, 5)
    c.execute("UPDATE money_people SET fec_candidate_ids = %s WHERE id = 2000099", ([CAND, new_id],))
    c.commit()
    try:
        bulk = FakeBulk(files=files)
        stats = FecSync(c, bulk).run(today=today)
        assert stats["files_loaded"] > 0 and any("indiv" in n for n in bulk.reads)
        assert c.execute("SELECT count(*) AS n FROM fec_cycles WHERE candidate_id = %s", (new_id,)).fetchone()["n"] == 1
        c.commit()
        again = FakeBulk(files=files)
        FecSync(c, again).run(today=today)
        assert again.reads == []    # recorded with the new roster, so it isn't read twice
    finally:
        c.rollback()
        c.execute("UPDATE money_people SET fec_candidate_ids = %s WHERE id = 2000099", ([CAND],))
        c.commit()
    stats = FecSync(c, FakeBulk()).run(today=today)
    assert stats["files_loaded"] > 0
    assert c.execute("SELECT count(*) AS n FROM fec_cycles WHERE candidate_id = %s", (new_id,)).fetchone()["n"] == 0
    c.commit()


def test_rows_of_someone_off_the_roster_do_not_trip_the_shrink_check(client):
    from wapeople.money.fec import FecSync

    c = client.conn
    c.execute("INSERT INTO money_people (id, name, kind, is_current, fec_candidate_ids) VALUES (2000098, 'Gone Member', 'federal', false, '{}')")
    with c.cursor() as cur:
        cur.executemany(
            "INSERT INTO fec_contributions (sub_id, person_pk, committee_id, cycle, dataset, source_type, is_memo, is_refund, name, amount)"
            " VALUES (%s, 2000098, 'C00000000', 2024, 'indiv', 'Individuals (itemized)', false, false, 'A, B', 1)",
            [(5000 + i,) for i in range(40)])
    c.commit()
    try:
        FecSync(c, FakeBulk()).run(full=True, today=date(2024, 11, 5))
        assert c.execute("SELECT count(*) AS n FROM fec_contributions WHERE person_pk = 2000098").fetchone()["n"] == 0
    finally:
        c.rollback()
        c.execute("DELETE FROM money_people WHERE id = 2000098")
        c.commit()


def test_rollups_leave_out_refunds_lump_sums_and_employers_that_do_not_rank(client):
    from wapeople.money.fec import FecSync

    c = client.conn
    donors = [("ACME", "ONE, DONOR"), ("ACME", "TWO, DONOR"), ("ACME", "THREE, DONOR"),     # three donors: ranks
              ("SMALL FIRM", "ONE, DONOR"), ("SMALL FIRM", "TWO, DONOR"),                   # two donors: doesn't
              ("MICHAEL MURR LLC", "MURR, MICHAEL"), ("MICHAEL MURR LLC", "MURR, MICHAEL J"),   # their own names
              ("MICHAEL MURR LLC", "MICHAEL, MURR"),
              ("ATTORNEY", "ONE, DONOR"), ("ATTORNEY", "TWO, DONOR"), ("ATTORNEY", "THREE, DONOR")]     # not an employer
    payees = [("EXPENSES OF $200 OR LESS", "Not categorized by the filer", ""), ("EXPENSES OF 50 OR LESS", "Not categorized by the filer", ""),
              ("A REFUNDED DONOR", "Refunds of contributions", ""), ("ANOTHER DONOR", "Other", "CONTRIBUTION REFUND"),
              ("TOTAL WINE", "Other", "EVENT DRINKS")]
    with c.cursor() as cur:
        cur.executemany(
            "INSERT INTO fec_contributions (sub_id, person_pk, committee_id, cycle, dataset, source_type, is_memo, is_refund, name, employer, amount)"
            " VALUES (%s, 2000099, %s, 2024, 'indiv', 'Individuals (itemized)', false, false, %s, %s, 1000)",
            [(6000 + i, CMTE, name, employer) for i, (employer, name) in enumerate(donors)])
        cur.executemany(
            "INSERT INTO fec_disbursements (sub_id, person_pk, committee_id, cycle, dataset, category, purpose, is_memo, name, amount)"
            " VALUES (%s, 2000099, %s, 2024, 'oppexp', %s, %s, false, %s, 5000)",
            [(6000 + i, CMTE, category, purpose, name) for i, (name, category, purpose) in enumerate(payees)])
    c.commit()
    fs = FecSync(c, FakeBulk())
    try:
        fs.rebuild_rollups()
        roll = c.execute("SELECT fed_rollup FROM money_people WHERE id = 2000099").fetchone()["fed_rollup"]
        assert [e["employer"] for e in roll["top_employers"]] == ["ACME"]
        names = {p["name"] for p in roll["top_payees"]}
        assert "TOTAL WINE" in names and "PRINT SHOP" in names
        assert not names & {"EXPENSES OF $200 OR LESS", "EXPENSES OF 50 OR LESS", "A REFUNDED DONOR", "ANOTHER DONOR"}
        assert fs.reconciliation()["cycles_operating_above_reported"] == 1      # $25,000 of rows against $400 reported
    finally:
        c.rollback()
        c.execute("DELETE FROM fec_contributions WHERE sub_id BETWEEN 6000 AND 6999")
        c.execute("DELETE FROM fec_disbursements WHERE sub_id BETWEEN 6000 AND 6999")
        c.commit()
        fs.rebuild_rollups()


def test_a_changed_file_waits_its_turn_then_reloads(client):
    from wapeople.money.fec import FecSync

    bulk = FakeBulk(etag="v2")
    FecSync(client.conn, bulk).run(today=date(2024, 11, 5))
    assert bulk.reads == []     # loaded minutes ago: not due yet
    client.conn.execute("UPDATE fec_sync_state SET loaded_at = %s", (datetime.now(timezone.utc) - timedelta(days=7),))
    client.conn.commit()
    FecSync(client.conn, bulk).run(today=date(2024, 11, 5))
    assert any("indiv" in n for n in bulk.reads)
    state = client.conn.execute("SELECT DISTINCT etag FROM fec_sync_state").fetchall()
    assert [s["etag"] for s in state] == ["v2"]


def test_a_nearly_empty_file_is_refused_and_erases_nothing(client):
    from wapeople.money.fec import FecSync
    from wapeople.money.sync import SuspiciousSourceData

    filler = [gift(1000 + i, "1") for i in range(20)]
    files = {**FILES, "indiv": FILES["indiv"] + filler}
    FecSync(client.conn, FakeBulk(etag="v3", files=files)).run(full=True, today=date(2024, 11, 5))
    before = client.conn.execute("SELECT count(*) AS n FROM fec_contributions WHERE dataset = 'indiv'").fetchone()["n"]
    client.conn.commit()
    with pytest.raises(SuspiciousSourceData):
        FecSync(client.conn, FakeBulk(etag="v4", files={**FILES, "indiv": []})).run(full=True, today=date(2024, 11, 5))
    client.conn.rollback()
    assert client.conn.execute("SELECT count(*) AS n FROM fec_contributions WHERE dataset = 'indiv'").fetchone()["n"] == before
    # Put the fixture data back. The guard would rightly refuse this too, so remove the filler first.
    client.conn.execute("DELETE FROM fec_contributions WHERE sub_id BETWEEN 1000 AND 1999")
    client.conn.commit()
    FecSync(client.conn, FakeBulk(etag="v5")).run(full=True, today=date(2024, 11, 5))
    # One fixture row is a repeat of another, so it isn't stored.
    assert client.conn.execute("SELECT count(*) AS n FROM fec_contributions WHERE dataset = 'indiv'").fetchone()["n"] == len(FILES["indiv"]) - 1
    client.conn.commit()


def test_federal_pages_label_their_source_and_keep_state_money_apart(client):
    r = client.get("/money/2000099")
    assert r.status_code == 200
    assert "$1,000" in r.text and "FEC total" in r.text and "Federal Election Commission" in r.text
    assert "$55,321" not in r.text and "$55K" not in r.text     # 1,000 federal is never added to 54,321 state
    assert "Earlier state and local campaigns" in r.text
    state = client.get("/money/2000099?records=state")
    assert state.status_code == 200 and "Public Disclosure Commission" in state.text


def test_state_tab_links_stay_on_state_records(client):
    text = client.get("/money/2000099?records=state").text
    assert 'href="?records=state&view=contributions' in text.replace("&amp;", "&")


def test_all_federal_views_render(client):
    pages = {v: client.get(f"/money/2000099?view={v}") for v in ("overview", "contributions", "spending", "outside")}
    assert all(p.status_code == 200 for p in pages.values())
    assert "Print shop".upper() in pages["spending"].text and "DCCC" in pages["spending"].text
    assert "BOEING COMPANY PAC" in pages["overview"].text and "Corporate PACs" in pages["overview"].text
    assert "https://www.fec.gov/data/committee/C00238725/" in pages["overview"].text
    assert "Big Super PAC" in pages["outside"].text and "AD VENDOR" in pages["outside"].text
    assert "PRIVATE, DONOR" in pages["contributions"].text and "97204" in pages["contributions"].text
    assert "whole dollars" in pages["contributions"].text and "$400.00" not in pages["contributions"].text


def test_private_donors_stay_off_the_indexed_page(client):
    assert "PRIVATE, DONOR" not in client.get("/money/2000099").text
    assert "noindex" not in client.get("/money/2000099").text
    assert 'name="robots" content="noindex"' in client.get("/money/2000099?view=contributions").text


def test_memo_entries_are_hidden_unless_asked_for(client):
    assert "$999.00" not in client.get("/money/2000099?view=contributions").text
    assert "Memo, not counted" in client.get("/money/2000099?view=contributions&memo=1").text


def test_contribution_filters(client):
    assert "SOMEONE, ELSE" not in client.get("/money/2000099?view=contributions&min=100").text
    assert "BOEING COMPANY PAC" not in client.get("/money/2000099?view=contributions&source=Individuals+(itemized)").text
    assert "BOEING COMPANY PAC" in client.get("/money/2000099?view=contributions&q=boeing").text
    assert client.get("/money/2000099?view=contributions&cycle=abc&min=abc&sort=DROP&page=-1").status_code == 200


def test_index_lists_congress_with_the_source_labeled(client):
    r = client.get("/money?group=federal")
    assert "Pramila Jayapal" in r.text and "$1,000" in r.text and "FEC" in r.text and "$54,321 PDC" in r.text
    assert "Pramila Jayapal" not in client.get("/money?group=statewide").text


def test_sitemap_includes_federal_pages(client):
    assert "/money/2000099<" in client.get("/sitemap-people.xml").text


def test_no_federal_officials_is_not_an_error(client):
    from wapeople.money.fec import FecSync

    class Empty(FecSync):
        def _people(self):
            return {}

    assert "note" in Empty(client.conn, FakeBulk()).run()
