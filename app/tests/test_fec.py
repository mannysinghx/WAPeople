"""Unit tests for reading the FEC's bulk files correctly and privately."""
import io
import zipfile
from datetime import date
from decimal import Decimal

import httpx
import pytest

from wapeople.money import clean as C
from wapeople.money import fec as F
from wapeople.money.fec_bulk import FecBulk, FileInfo, SeekableHTTP
from wapeople.money.sync import load_roster


def indiv(tx="15", entity="IND", memo="", amount="3300", name="VAIL, DAVID B", city="TACOMA", zip_code="984150707",
          employer="ACME", occupation="ATTORNEY", sub="4111620241069704958", date_text="08052024", memo_text=""):
    return ["C00345546", "N", "Q3", "P2024", "202410159687666104", tx, entity, name, city, "WA", zip_code, employer,
            occupation, date_text, amount, "", "5584161", "1827863", memo, memo_text, sub]


def contribution(row):
    return dict(zip(F.CONTRIBUTION_COLUMNS, row))


def test_a_normal_gift_is_read_correctly():
    r = contribution(F.contribution_row(indiv(), 100, 2024))
    assert r["amount"] == Decimal("3300.00") and r["tx_date"] == date(2024, 8, 5)
    assert r["source_type"] == F.INDIVIDUALS and not r["is_memo"] and not r["is_refund"]
    assert r["name"] == "VAIL, DAVID B" and r["city"] == "TACOMA" and r["sub_id"] == 4111620241069704958


def test_nine_digit_zip_is_cut_to_five():
    assert contribution(F.contribution_row(indiv(), 100, 2024))["zip"] == "98415"


def test_refunds_are_negative_even_though_the_file_shows_them_positive():
    r = contribution(F.contribution_row(indiv(tx="22Y", amount="500"), 100, 2024))
    assert r["is_refund"] and r["amount"] == Decimal("-500.00")


def test_memo_entries_are_flagged_so_totals_can_skip_them():
    assert contribution(F.contribution_row(indiv(memo="X"), 100, 2024))["is_memo"]


@pytest.mark.parametrize("tx,entity,expected", [
    ("15", "IND", F.INDIVIDUALS), ("15E", "IND", F.INDIVIDUALS), ("15C", "CAN", C.SELF_FUNDED),
    ("11", "ORG", "Tribes and other organizations"),
])
def test_who_gave(tx, entity, expected):
    assert contribution(F.contribution_row(indiv(tx=tx, entity=entity), 100, 2024))["source_type"] == expected


def test_street_addresses_typed_into_other_fields_are_removed():
    r = contribution(F.contribution_row(indiv(city="14715 182ND PL SE", employer="13420 339TH AVE SE",
                                              memo_text="MAIL TO 77 SECRET LANE"), 100, 2024))
    assert r["city"] is None and r["employer"] is None
    assert "SECRET" not in r["memo_text"]


def test_short_or_corrupt_rows_are_skipped():
    assert F.contribution_row(["C00345546", "N"], 100, 2024) is None
    assert F.contribution_row(indiv(sub="not-a-number"), 100, 2024) is None


def pas2(tx="24K", giver="C00238725", amount="1000"):
    return [giver, "N", "M2", "P2024", "202302179578660679", tx, "CCM", "DAN NEWHOUSE FOR CONGRESS", "SUNNYSIDE", "WA",
            "989449767", "", "", "01242023", amount, "C00559393", "H4WA04104", "48394886", "1689568", "", "CONTRIBUTION",
            "4021720231732323105"]


def cm(name="BOEING COMPANY PAC", cmte_type="Q", designation="B", org="C", connected="THE BOEING COMPANY"):
    return ["C00238725", name, "A TREASURER", "1 SECRET ST", "", "ARLINGTON", "VA", "222021234", designation, cmte_type,
            "", "M", org, connected, ""]


def test_committee_gift_names_the_giver_not_the_recipient():
    r = contribution(F.committee_gift_row(pas2(), 100, "C00559393", 2024, cm()))
    assert r["name"] == "BOEING COMPANY PAC" and r["other_id"] == "C00238725"
    assert r["committee_id"] == "C00559393" and r["connected_org"] == "THE BOEING COMPANY"
    assert r["source_type"] == "Corporate PACs" and r["zip"] == "22202"


def test_treasurer_and_street_from_the_committee_file_are_not_kept():
    row = F.committee_gift_row(pas2(), 100, "C00559393", 2024, cm())
    assert "A TREASURER" not in row and "1 SECRET ST" not in row


def test_unknown_giver_still_has_a_name():
    assert contribution(F.committee_gift_row(pas2(), 100, "C00559393", 2024, None))["name"] == "Committee C00238725"


@pytest.mark.parametrize("cmte_type,designation,org,expected", [
    ("X", "U", "", "Party committees"), ("Y", "U", "", "Party committees"),
    ("H", "A", "", "Other candidates' committees"), ("Q", "D", "", "Leadership PACs"),
    ("Q", "B", "C", "Corporate PACs"), ("Q", "U", "L", "Labor union PACs"), ("Q", "U", "T", "Trade association PACs"),
    ("Q", "U", "M", "Membership group PACs"), ("N", "U", "", "Other PACs"),
])
def test_committee_types_use_the_fec_classification(cmte_type, designation, org, expected):
    assert F.committee_source(cmte_type, designation, org) == expected


def expense(purpose="CREDIT CARD FEES", category="", memo="", amount="294.67", sub="4042620231744760560"):
    return ["C00652628", "N", "2023", "Q1", "202304149580536988", "17", "F3", "SB", "ACTBLUE TECHNICAL SERVICES", "CAMBRIDGE",
            "MA", "021385106", "02/05/2023", amount, "P2024", purpose, "", category, memo, "", "ORG", sub, "1699203", "500457159", ""]


def test_expenses_are_read_and_categorized_from_the_stated_purpose():
    r = dict(zip(F.DISBURSEMENT_COLUMNS, F.expense_row(expense(), 100, 2024)))
    assert r["amount"] == Decimal("294.67") and r["tx_date"] == date(2023, 2, 5) and r["zip"] == "02138"
    assert r["category"] == "Bank and payment processing charges" and r["purpose"] == "CREDIT CARD FEES"


@pytest.mark.parametrize("purpose,category,expected", [
    ("MEDIA BUY", "", "Broadcast/cable TV advertising"), ("PAYROLL", "", "Wages, salaries, benefits, payroll taxes"),
    ("POLLING", "", "Surveys, polling, research costs"), ("POSTAGE", "", "Postage costs, mail permits, purchase of stamps"),
    ("XYZ", "Travel Expenses", "Travel, accommodations, meals"), ("XYZ", "Something New", "Something New"),
    ("MEDIA", "", "Other advertising"), ("TELEMARKETING FEE", "", "Telemarketing and text messaging"),
    ("TELEPHONE", "", "Utilities, phone, and other overhead costs"), ("REIMBURSEMENT - SEE BELOW", "", C.UNCATEGORIZED),
    ("XYZ", "", C.UNCATEGORIZED), (None, None, C.UNCATEGORIZED),
])
def test_spending_categories(purpose, category, expected):
    assert F.spend_category(purpose, category) == expected


@pytest.mark.parametrize("tx,expected", [
    ("24K", "Contributions and transfers to other committees"), ("24G", "Contributions and transfers to other committees"),
    ("22Z", "Refunds of contributions"), ("20C", "Loan repayments"),
])
def test_money_sent_to_other_committees(tx, expected):
    assert F.outflow_category(tx) == expected


def test_amended_outside_spending_counts_once_at_its_newest_amount():
    base = {"cand_id": "H6WA07458", "spe_id": "C1", "spe_nam": "Big PAC", "tran_id": "T1", "sup_opp": "S", "exp_date": "01-JUN-24"}
    rows = F.outside_rows([{**base, "exp_amo": "100", "file_num": "1"}, {**base, "exp_amo": "250", "file_num": "2"},
                           {**base, "cand_id": "H9XX00000", "exp_amo": "9", "file_num": "3"}], {"H6WA07458": 7}, 2024)
    assert len(rows) == 1
    r = dict(zip(F.OUTSIDE_COLUMNS, rows[0]))
    assert r["amount"] == Decimal("250.00") and r["tx_date"] == date(2024, 6, 1) and r["person_pk"] == 7


WEBALL = ["H0WA08046", "DELBENE, SUZAN K", "I", "1", "DEM", "3850571.4", "405898.79", "3466395.53", "0", "679544.74",
          "1063720.61", "0", "0", "0", "0", "0", "495000", "1635590.56", "WA", "01", "", "", "", "", "", "1805850", "1338.8",
          "12/31/2024", "28621", "0"]


def test_reported_totals_go_in_the_right_columns():
    r = dict(zip(("candidate_id", "cycle", "person_pk", "candidate_name", "office", "state", "district", "party", "receipts",
                  "disbursements", "from_individuals", "from_pacs", "from_parties", "candidate_contributions", "candidate_loans",
                  "other_loans", "transfers_in", "transfers_out", "individual_refunds", "committee_refunds", "cash_start",
                  "cash_end", "debts", "coverage_end"), F.totals_row(WEBALL, 9, 2024)))
    assert r["receipts"] == Decimal("3850571.40") and r["disbursements"] == Decimal("3466395.53")
    assert r["from_individuals"] == Decimal("1635590.56") and r["from_pacs"] == Decimal("1805850.00")
    assert r["transfers_in"] == Decimal("405898.79") and r["cash_end"] == Decimal("1063720.61")
    assert r["debts"] == Decimal("495000.00") and r["district"] == "1" and r["office"] == "H"
    assert r["coverage_end"] == date(2024, 12, 31)


def test_where_the_money_came_from_adds_up_to_the_reported_total():
    parts = F.reported_sources({"raised": Decimal("1000.00"), "individuals": Decimal("600.00"), "itemized": Decimal("450.00"),
                                "pacs": Decimal("200.00"), "parties": Decimal("50.00"), "own": Decimal("0"),
                                "loans": Decimal("0"), "transfers": Decimal("100.00")})
    by = {p["type"]: p["total"] for p in parts}
    assert by[F.INDIVIDUALS] == 450 and by["Individuals (small gifts, not itemized)"] == 150
    assert "Other loans" not in by                     # zero amounts aren't listed
    assert "Other receipts (interest, refunds, offsets)" in by and by["Other receipts (interest, refunds, offsets)"] == 50
    assert round(sum(by.values()), 2) == 1000


def test_itemized_above_the_reported_figure_never_shows_negative_small_gifts():
    parts = F.reported_sources({"raised": Decimal("100"), "individuals": Decimal("100"), "itemized": Decimal("101"),
                                "pacs": 0, "parties": 0, "own": 0, "loans": 0, "transfers": 0})
    assert {p["type"]: p["total"] for p in parts} == {F.INDIVIDUALS: 100}


def test_cycles_run_newest_first_back_to_1992():
    cycles = F.all_cycles(date(2026, 11, 5))
    assert cycles[0] == 2026 and cycles[-1] == 1992 and F.current_cycle(date(2027, 1, 5)) == 2028


def test_roster_has_the_whole_delegation():
    federal = load_roster()["federal"]
    assert len(federal) == 12
    assert sum(1 for o in federal if o["title"] == "U.S. Senator") == 2
    assert {f"WA-{i}" for i in range(1, 11)} == {o["title"].split(", ")[1] for o in federal if "," in o["title"]}
    assert all(o["fec_candidate_ids"] and o["source_url"].startswith("https://") for o in federal)
    assert all(o["member_id"] or o["id"] >= 2_000_000 for o in federal)
    ids = [c for o in federal for c in o["fec_candidate_ids"]]
    assert len(ids) == len(set(ids))


# ---- The streaming reader ------------------------------------------------------------------

def make_zip(members: dict[str, bytes]) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in members.items():
            z.writestr(name, data)
    return out.getvalue()


def range_server(data: bytes, fail_once_at: int | None = None):
    """Serves byte ranges, and can drop the connection once partway through a transfer."""
    state = {"failed": False, "requests": []}

    class Stream(httpx.SyncByteStream):
        def __init__(self, chunk, start):
            self.chunk, self.start = chunk, start

        def __iter__(self):
            for i in range(0, len(self.chunk), 1024):
                if fail_once_at is not None and not state["failed"] and self.start + i >= fail_once_at:
                    state["failed"] = True
                    raise httpx.ReadError("connection reset")
                yield self.chunk[i:i + 1024]

    def handler(request):
        if request.method == "HEAD":
            return httpx.Response(200, headers={"content-length": str(len(data)), "etag": '"v1"'})
        start = int(request.headers["range"].split("=")[1].split("-")[0])
        state["requests"].append(start)
        return httpx.Response(206, stream=Stream(data[start:], start))

    return httpx.Client(transport=httpx.MockTransport(handler), base_url="https://fec.test"), state


def test_only_the_full_file_is_read_so_rows_are_not_counted_twice():
    data = make_zip({"itcont.txt": b"C00000001|a\nC00000002|b\n", "by_date/itcont_1.txt": b"C00000001|a\n"})
    client, _ = range_server(data)
    bulk = FecBulk(client)
    rows = list(bulk.rows("2024/indiv24.zip"))
    assert rows == [["C00000001", "a"], ["C00000002", "b"]]


def test_rows_are_filtered_by_their_first_field_and_bad_bytes_survive():
    data = make_zip({"itoth.txt": b"C00000001|caf\xe9\nC00000002|b\n"})
    client, _ = range_server(data)
    rows = list(FecBulk(client).rows("x.zip", frozenset({b"C00000001"})))
    assert len(rows) == 1 and rows[0][0] == "C00000001" and rows[0][1].startswith("caf")


def test_an_interrupted_transfer_resumes_without_losing_or_repeating_data(monkeypatch):
    monkeypatch.setattr("wapeople.money.fec_bulk.time.sleep", lambda s: None)
    payload = bytes(range(256)) * 400
    client, state = range_server(payload, fail_once_at=40_000)
    source = SeekableHTTP(client, FileInfo("https://fec.test/x", len(payload), None, None))
    out = b""
    while chunk := source.read(4096):
        out += chunk
    assert out == payload and state["failed"] and len(state["requests"]) == 2


def test_seeking_works_and_a_read_past_the_end_is_empty():
    payload = b"0123456789" * 100
    client, _ = range_server(payload)
    source = SeekableHTTP(client, FileInfo("https://fec.test/x", len(payload), None, None))
    source.seek(-5, 2)
    assert source.read(100) == b"56789" and source.read(10) == b""
    source.seek(10)
    assert source.read(3) == b"012" and source.tell() == 13


def test_a_missing_file_is_not_an_error():
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404)), base_url="https://fec.test")
    bulk = FecBulk(client)
    assert bulk.info("1992/oppexp92.zip") is None
    assert list(bulk.rows("1992/oppexp92.zip")) == []
    assert list(bulk.csv_rows("1992/independent_expenditure_1992.csv")) == []
