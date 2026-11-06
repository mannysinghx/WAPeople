"""Load Washington's members of Congress from the FEC's public bulk files.

For each two-year cycle: the FEC's own totals per candidate, then every itemized row for their
campaign committees. Each file is replaced as a unit and only when its version changes, so a
finished cycle is never read twice, and an interrupted run continues with what it hasn't done.

Reading this data correctly takes some care. These rules come from comparing the files with the
FEC's published totals:
  * A row with memo code X is detail about money already counted elsewhere; it doesn't add to totals.
    Neither do types ending in J (a share of a joint fundraising gift, which some filers leave
    unmarked) or a campaign's own report of a committee's gift (18K, 15Z): the giver's row counts.
  * One filed line can be in a file several times under new SUB_IDs, and amended reports repeat
    lines, so rows are matched on their transaction ID and only the newest filing's copy is kept.
  * Types 21Y and 22Y are refunds, and the file shows them as positive numbers.
  * A committee's gift to a candidate is found in the giver's report (the pas2 file), so the giver
    is the row's first column, not its name column.
  * Individuals' gifts of $200 or less aren't itemized, so unitemized = reported - itemized.
  * Contribution amounts in these files are whole dollars (expenses have cents), so itemized rows
    can't add up to reported totals to the cent. Reported totals are stored exactly, and were
    checked against the FEC's API; row sums are stored beside them so any gap stays visible.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb

from . import clean as C
from .fec_bulk import FecBulk
from .sync import EMPLOYER_GROUP, EMPLOYER_STOP, MIN_EMPLOYER_DONORS, PAYEE_LUMP_SUM, SuspiciousSourceData

log = logging.getLogger(__name__)

FIRST_CYCLE = 1992
FIRST_LINKAGE_CYCLE = 2000      # the candidate-committee linkage file starts here
INDIVIDUALS = "Individuals (itemized)"
SMALL_GIFTS = "Individuals (small gifts, not itemized)"
REFUND_TYPES = {"20Y", "21Y", "22Y", "23Y"}
ORG_TYPES = {"C": "Corporate PACs", "L": "Labor union PACs", "M": "Membership group PACs",
             "T": "Trade association PACs", "V": "Cooperative PACs", "W": "Corporation-without-stock PACs"}
GIFT_TYPES = {"24K", "24Z"}     # direct and in-kind gifts; other 24x types are outside spending
GIVER_COUNTED_TYPES = {"18K", "15Z"}    # the same gift is the counted row in the giver's pas2 report


def current_cycle(today: date | None = None) -> int:
    year = (today or date.today()).year
    return year + (year % 2)


def all_cycles(today: date | None = None) -> list[int]:
    return list(range(current_cycle(today), FIRST_CYCLE - 2, -2))


def fec_date(value) -> date | None:
    """MMDDYYYY in transaction files, MM/DD/YYYY in expenses, 01-JUN-24 in outside spending."""
    s = (value or "").strip()
    for fmt in ("%m%d%Y", "%m/%d/%Y", "%d-%b-%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def committee_source(cmte_type: str, designation: str, org_type: str) -> str:
    """How to describe the committee a gift came from, using the FEC's own classification."""
    if cmte_type in ("X", "Y", "Z"):
        return "Party committees"
    if cmte_type in ("H", "S", "P"):
        return "Other candidates' committees"
    if designation == "D":
        return "Leadership PACs"
    return ORG_TYPES.get(org_type, "Other PACs")


def individual_source(tx_type: str, entity: str, name: str) -> str:
    if tx_type.startswith("11"):    # the FEC's type for a tribe's gift, often filed with no entity type
        return "Tribes and other organizations"
    if tx_type == "15C" or entity == "CAN":
        return C.SELF_FUNDED
    if entity in ("ORG", "PAC", "COM", "CCM", "PTY"):
        return "Tribes and other organizations"
    return INDIVIDUALS


def receipt_source(tx_type: str, entity: str) -> str:
    if tx_type.startswith("16"):
        return "Loans"
    if tx_type.startswith("17"):
        return "Other receipts"
    if tx_type.startswith("18K"):
        return "Committee gifts reported by the campaign"
    if tx_type.startswith("18"):
        return "Transfers and joint fundraising"
    if entity == "CAN":
        return C.SELF_FUNDED
    return "Other receipts"


def outflow_category(tx_type: str) -> str:
    if tx_type.startswith("20") or tx_type.startswith("19"):
        return "Loan repayments"
    if tx_type.startswith("21") or tx_type.startswith("22"):
        return "Refunds of contributions"
    return "Contributions and transfers to other committees"


_FEC_SPEND_RULES = (
    (r"\bmedia buy|\btv\b|\btelevision\b|\bcable\b|\bbroadcast\b", "Broadcast/cable TV advertising"),
    (r"\bradio\b", "Radio advertising"),
    (r"\bdigital\b|\bonline\b|\bfacebook\b|\bgoogle ad", "Digital advertising"),
    (r"\bmedia\b|\bvideo\b", "Other advertising"),
    (r"\btelemarket|\btext messag|\bsms\b|\bphone bank", "Telemarketing and text messaging"),
    (r"\bpayroll\b|\bsalar|\bwages?\b|\bhealth insurance\b|\bemployee benefit|\bstaff\b|\btax impound|\bbonus\b",
     "Wages, salaries, benefits, payroll taxes"),
    (r"\bconsult|\bstrategy\b|\bmanagement\b|\bfundrais\w* (fee|consult)", "Management and consulting services"),
    (r"\bcredit card|\bprocessing fee|\bmerchant\b|\bbank (fee|charge)|\bactblue\b|\bwinred\b|\bonline fee",
     "Bank and payment processing charges"),
    (r"\bpostage\b|\bshipping\b", "Postage costs, mail permits, purchase of stamps"),
    (r"\bprint|\bmail\b|\bmailing\b|\bmailer|\bdirect mail\b|\bliterature\b", "Printing literature, fliers, postcards, etc."),
    (r"\bpoll|\bsurvey|\bresearch\b", "Surveys, polling, research costs"),
    (r"\blist rental|\blist acquisition|\bdata\b|\bvoter file\b|\bsoftware\b|\bwebsite\b|\bweb hosting\b|\bemail hosting\b|"
     r"\bcomputer\b|\bsubscription\b|\btech licens", "Computers, printers, software, phones, etc."),
    (r"\bparaphernalia\b|\bmerchandise\b", "Campaign merchandise/paraphernalia"),
    (r"\bevent\b|\bcatering\b|\bfundrais|\bsite rental\b", "Fundraising events and related costs"),
    (r"\btravel\b|\bairfare\b|\bhotel\b|\blodging\b|\bmileage\b|\bmeals?\b|\bgas\b|\bparking\b|\bcar rental\b",
     "Travel, accommodations, meals"),
    (r"\brent\b|\butilit|\bphone\b|\btelephone\b|\bcell\b|\binternet\b|\bfurniture\b|\boffice\b|\bstorage\b|\binsurance\b|"
     r"\bequipment rental\b", "Utilities, phone, and other overhead costs"),
    (r"\blegal\b|\baccounting\b|\bcompliance\b|\battorney\b|\bbookkeep", "Accounting, legal, regulatory compliance, etc."),
    (r"\bcontribution\b|\bdonation\b|\bunlimited transfer\b", "Contributions and transfers to other committees"),
    (r"\bad\b|\bads\b|\badvertis", "Other advertising"),
)
_FEC_SPEND_RES = [(re.compile(p, re.I), label) for p, label in _FEC_SPEND_RULES]
_FEC_CATEGORIES = {
    "administrative/salary/overhead expenses": "Utilities, phone, and other overhead costs",
    "advertising expenses": "Other advertising",
    "solicitation and fundraising expenses": "Fundraising events and related costs",
    "travel expenses": "Travel, accommodations, meals",
    "campaign materials": "Campaign merchandise/paraphernalia",
    "polling expenses": "Surveys, polling, research costs",
    "campaign event expenses": "Fundraising events and related costs",
    "loan repayments": "Loan repayments",
    "refunds of contributions": "Refunds of contributions",
}


def spend_category(purpose, category_desc) -> str:
    """Read the filer's stated purpose first, since most rows have no category code."""
    text = C.text(purpose)
    if text:
        for rx, label in _FEC_SPEND_RES:
            if rx.search(text):
                return label
    category = C.text(category_desc)
    return _FEC_CATEGORIES.get((category or "").lower(), category) or C.UNCATEGORIZED


def _zip5(value) -> str | None:
    match = re.match(r"\d{5}", (value or "").strip())
    return match[0] if match else None


# ---- Row conversion ---------------------------------------------------------------------------

CONTRIBUTION_COLUMNS = ("sub_id", "person_pk", "committee_id", "cycle", "dataset", "tx_type", "report_type", "election",
                        "image_num", "source_type", "is_memo", "is_refund", "name", "city", "state", "zip", "employer",
                        "occupation", "tx_date", "amount", "other_id", "connected_org", "memo_text")
DISBURSEMENT_COLUMNS = ("sub_id", "person_pk", "committee_id", "cycle", "dataset", "tx_type", "report_type", "image_num",
                        "category", "purpose", "is_memo", "name", "city", "state", "zip", "tx_date", "amount", "other_id")
OUTSIDE_COLUMNS = ("id", "person_pk", "candidate_id", "cycle", "spender_id", "spender_name", "support_oppose", "amount",
                   "tx_date", "purpose", "payee", "image_num")


def contribution_row(r: list[str], person: int, cycle: int, dataset: str = "indiv") -> tuple | None:
    if len(r) < 21 or not r[20].strip().isdigit():
        return None
    tx, entity = r[5].strip(), r[6].strip()
    refund = tx in REFUND_TYPES
    amount = C.money(r[14])
    if refund:
        amount = -abs(amount)
    source = individual_source(tx, entity, r[7]) if dataset == "indiv" else receipt_source(tx, entity)
    clean_name = C.person_name if source in (INDIVIDUALS, C.SELF_FUNDED) else C.free_text
    name = clean_name(r[7]) or "(name not reported)"
    memo = r[18].strip() == "X" or tx.endswith("J") or tx in GIVER_COUNTED_TYPES
    return (int(r[20]), person, r[0], cycle, dataset, tx, r[2].strip() or None, r[3].strip() or None, r[4].strip() or None,
            source, memo, refund, name, C.place(r[8]), C.text(r[9]), _zip5(r[10]), C.employer(r[11]),
            C.free_text(r[12]), fec_date(r[13]), amount, C.text(r[15]), None, C.free_text(r[19]))


def committee_gift_row(r: list[str], person: int, recipient: str, cycle: int, giver: list[str] | None) -> tuple | None:
    """A pas2 row: the first column is the committee that gave; the name column is the recipient."""
    if len(r) < 22 or not r[21].strip().isdigit():
        return None
    giver = giver or []
    field = lambda i: giver[i] if len(giver) > i else ""    # noqa: E731
    return (int(r[21]), person, recipient, cycle, "pas2", r[5].strip(), r[2].strip() or None, r[3].strip() or None,
            r[4].strip() or None, committee_source(field(9), field(8), field(12)), r[19].strip() == "X", False,
            C.text(field(1)) or f"Committee {r[0]}", C.place(field(5)), C.text(field(6)), _zip5(field(7)), None, None,
            fec_date(r[13]), C.money(r[14]), r[0], C.text(field(13)), C.free_text(r[20]))


def expense_row(r: list[str], person: int, cycle: int) -> tuple | None:
    if len(r) < 22 or not r[21].strip().isdigit():
        return None
    return (int(r[21]), person, r[0], cycle, "oppexp", None, r[3].strip() or None, r[4].strip() or None,
            spend_category(r[15], r[17]), C.free_text(r[15]), r[18].strip() == "X", C.free_text(r[8]), C.place(r[9]),
            C.text(r[10]), _zip5(r[11]), fec_date(r[12]), C.money(r[13]), None)


def outflow_row(r: list[str], person: int, cycle: int) -> tuple | None:
    if len(r) < 21 or not r[20].strip().isdigit():
        return None
    tx = r[5].strip()
    return (int(r[20]), person, r[0], cycle, "oth", tx, r[2].strip() or None, r[4].strip() or None, outflow_category(tx),
            C.free_text(r[19]), r[18].strip() == "X", C.free_text(r[7]), C.place(r[8]), C.text(r[9]), _zip5(r[10]),
            fec_date(r[13]), C.money(r[14]), C.text(r[15]))


# Where each file keeps a row's transaction ID and filing number.
FILING_COLUMNS = {"indiv": (16, 17), "oth": (16, 17), "pas2": (17, 18), "oppexp": (23, 22)}


def newest_filings(rows, dataset: str) -> list[list[str]]:
    """Each filed line once, from its newest filing. A transaction ID can come up again in a committee's
    other reports, so the report and year are part of the match. Rows with no ID can't be matched and all stay."""
    tran_col, file_col = FILING_COLUMNS[dataset]
    newest: dict[tuple, tuple] = {}
    unmatched = []
    for r in rows:
        if len(r) <= max(tran_col, file_col) or not r[tran_col].strip():
            unmatched.append(r)
            continue
        report, year = (r[3], r[2]) if dataset == "oppexp" else (r[2], r[13].strip()[-4:])
        key = (r[0], report.strip(), year.strip(), r[tran_col].strip())
        filing = int(r[file_col]) if r[file_col].strip().isdigit() else 0
        if key not in newest or filing > newest[key][0]:
            newest[key] = (filing, r)
    return _one_report_per_page(unmatched + [r for _, r in newest.values()], dataset)


# A filing can list the same pages under two report types (Murray 2004: Q1 and Q2, with no matching
# transaction IDs), which doubled $361,563 of gifts. An image number is one physical page, so the same line
# on the same page under another report is that line again. Two equal lines under one report both stay.
def _one_report_per_page(rows: list[list[str]], dataset: str) -> list[list[str]]:
    if dataset not in ("indiv", "oth", "pas2"):
        return rows
    reports: dict[tuple, set] = {}

    def line(r: list[str]) -> tuple:
        return (r[0], r[4].strip(), r[5].strip(), r[7].strip().upper(), r[13].strip(), r[14].strip())

    def on_a_page(r: list[str]) -> bool:
        return len(r) > 14 and bool(r[4].strip())

    for r in rows:
        if on_a_page(r):
            reports.setdefault(line(r), set()).add(r[2].strip())
    return [r for r in rows if not on_a_page(r) or r[2].strip() == min(reports[line(r)])]


def outside_rows(rows, people: dict[str, int], cycle: int) -> list[tuple]:
    """Many filers restart their transaction IDs with each filing, so an ID only identifies a row together
    with its filing. An amendment replaces the whole filing it names, or the rows with its IDs when it names
    none. A line repeated word for word with its ID in a later filing counts once. Rows must all be read first,
    since an amendment that drops a candidate has no row naming them."""
    def field(r: dict, column: str) -> str:
        return (r.get(column) or "").strip()

    def filing(r: dict) -> int:
        return int(field(r, "file_num")) if field(r, "file_num").isdigit() else 0

    replaced: set[tuple] = set()
    amended_ids: dict[tuple, int] = {}
    ours = []
    for r in rows:
        if field(r, "amndt_ind") not in ("", "N"):
            if field(r, "prev_file_num"):
                replaced.add((field(r, "spe_id"), field(r, "prev_file_num")))
            elif field(r, "tran_id"):
                key = (field(r, "spe_id"), field(r, "tran_id"))
                amended_ids[key] = max(amended_ids.get(key, 0), filing(r))
        if field(r, "cand_id") in people:
            ours.append(r)

    newest: dict[tuple, tuple] = {}
    for r in ours:
        candidate, spender, tran = field(r, "cand_id"), field(r, "spe_id"), field(r, "tran_id")
        if (spender, field(r, "file_num")) in replaced or amended_ids.get((spender, tran), 0) > filing(r):
            continue
        spent_on = field(r, "exp_date") or field(r, "dissem_dt")
        line = tran or field(r, "image_num")
        key = (spender, line, candidate, spent_on, field(r, "exp_amo"), field(r, "pur").upper())
        if key in newest and newest[key][0] >= filing(r):
            continue
        newest[key] = (filing(r), (
            f"{cycle}-{spender}-{field(r, 'file_num')}-{line}", people[candidate], candidate, cycle, spender or None,
            C.text(r.get("spe_nam")), field(r, "sup_opp") or None, C.money(r.get("exp_amo")), fec_date(spent_on),
            C.free_text(r.get("pur")), C.free_text(r.get("pay")), field(r, "image_num") or None))
    return [v[1] for v in newest.values()]


def money(field: list[str], i: int) -> Decimal:
    return C.money(field[i]) if len(field) > i else Decimal("0.00")


def totals_row(r: list[str], person: int, cycle: int) -> tuple:
    return (r[0], cycle, person, C.text(r[1]), r[0][:1], C.text(r[18]) if len(r) > 18 else None,
            (r[19].lstrip("0") or None) if len(r) > 19 else None, C.text(r[4]), money(r, 5), money(r, 7), money(r, 17),
            money(r, 25), money(r, 26), money(r, 11), money(r, 12), money(r, 13), money(r, 6), money(r, 8), money(r, 28),
            money(r, 29), money(r, 9), money(r, 10), money(r, 16), fec_date(r[27]) if len(r) > 27 else None)


def _roster(people: dict[str, int]) -> list[str]:
    return sorted(f"{candidate}:{person}" for candidate, person in people.items())


class FecSync:
    # How long a changed file can wait: the FEC regenerates recent cycles' files every week.
    CURRENT_MIN_AGE = timedelta(days=6)
    RECENT_MIN_AGE = timedelta(days=30)

    def __init__(self, conn: psycopg.Connection, bulk: FecBulk | None = None):
        self.conn = conn
        self.bulk = bulk or FecBulk()
        self.stats: dict = {"files_loaded": 0, "files_unchanged": 0, "rows_loaded": 0}

    def close(self) -> None:
        self.bulk.close()

    def _people(self) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT id, fec_candidate_ids FROM money_people WHERE cardinality(fec_candidate_ids) > 0").fetchall()
        self.conn.commit()
        return {cid: r["id"] for r in rows for cid in r["fec_candidate_ids"]}

    def _due(self, dataset: str, cycle: int, name: str, full: bool, today: date, people: dict[str, int]):
        """The file's version info if it should be loaded now, otherwise None."""
        info = self.bulk.info(name)
        state = self.conn.execute(
            "SELECT etag, rows, roster, loaded_at FROM fec_sync_state WHERE dataset = %s AND cycle = %s",
            (dataset, cycle)).fetchone()
        self.conn.commit()
        if info is None:
            if state and state["rows"]:
                raise SuspiciousSourceData(f"{name} had {state['rows']} rows and is no longer published. Nothing changed.")
            return None
        # Rows are filtered to the roster as they're read, so a new name has nothing in a file loaded before it.
        if full or state is None or state["roster"] != _roster(people):
            return info
        if state["etag"] == info.etag:
            self.stats["files_unchanged"] += 1
            return None
        wait = self.CURRENT_MIN_AGE if cycle == current_cycle(today) else self.RECENT_MIN_AGE
        if datetime.now(timezone.utc) - state["loaded_at"] < wait:
            return None
        return info

    def _record(self, dataset: str, cycle: int, etag: str | None, rows: int, people: dict[str, int]) -> None:
        self.conn.execute(
            "INSERT INTO fec_sync_state (dataset, cycle, etag, rows, roster, loaded_at) VALUES (%s, %s, %s, %s, %s, now())"
            " ON CONFLICT (dataset, cycle) DO UPDATE SET etag = EXCLUDED.etag, rows = EXCLUDED.rows,"
            " roster = EXCLUDED.roster, loaded_at = now()",
            (dataset, cycle, etag, rows, _roster(people)))
        self.stats["files_loaded"] += 1
        self.stats["rows_loaded"] += rows

    def _check_shrink(self, table: str, where: str, params: tuple, new_count: int, floor: int = 10) -> None:
        """A result far smaller than what's stored stops the run: that looks like a bad download, not real
        deletions. Rows of someone taken off the roster don't count, since they're meant to go."""
        have = self.conn.execute(
            f"SELECT count(*) AS n FROM {table} WHERE {where} AND person_pk IN"
            " (SELECT id FROM money_people WHERE cardinality(fec_candidate_ids) > 0)", params).fetchone()["n"]
        if have >= floor and new_count < have / 2:
            self.conn.rollback()
            raise SuspiciousSourceData(f"{table} {params}: file has {new_count} rows, we have {have}. Nothing changed.")

    def _replace(self, table: str, columns: tuple, where: str, params: tuple, rows: list[tuple], key: int = 0) -> None:
        """Swap a file's rows in one transaction, unless the shrink check stops it."""
        self._check_shrink(table, where, params, len(rows))
        seen, unique = set(), []
        for row in rows:
            if row[key] not in seen:
                seen.add(row[key])
                unique.append(row)
        with self.conn.cursor() as cur:
            cur.execute(f"DELETE FROM {table} WHERE {where}", params)
            cur.execute(f"DELETE FROM {table} WHERE {columns[key]} = ANY(%s)", (list(seen),))
            with cur.copy(f"COPY {table} ({', '.join(columns)}) FROM STDIN") as copy:
                for row in unique:
                    copy.write_row(row)

    def _support_rows(self, prefix: str, cycle: int):
        """A file another load depends on. A missing one would quietly strip names and committees from what's
        stored, so from the first cycle that has them all, it stops the run."""
        name = FecBulk.zip_name(prefix, cycle)
        info = self.bulk.info(name)
        if info is not None:
            return self.bulk.rows(name, info=info)
        if cycle >= FIRST_LINKAGE_CYCLE:
            raise SuspiciousSourceData(f"{name} is missing. Nothing changed.")
        return ()

    # ---- One cycle ----------------------------------------------------------------------------

    def sync_cycle(self, cycle: int, people: dict[str, int], full: bool, today: date) -> None:
        committees = self._sync_reference(cycle, people, full, today)
        if not committees:
            return
        wanted = frozenset(c.encode() for c in committees)

        name = FecBulk.zip_name("indiv", cycle)
        info = self._due("indiv", cycle, name, full, today, people)
        if info:
            rows = [row for r in newest_filings(self.bulk.rows(name, wanted, info), "indiv")
                    if (row := contribution_row(r, committees[r[0]], cycle)) is not None]
            self._replace("fec_contributions", CONTRIBUTION_COLUMNS, "cycle = %s AND dataset = 'indiv'", (cycle,), rows)
            self._record("indiv", cycle, info.etag, len(rows), people)
            self.conn.commit()
            log.info("FEC %s: %d individual rows", cycle, len(rows))

        name = FecBulk.zip_name("oth", cycle)
        info = self._due("oth", cycle, name, full, today, people)
        if info:
            receipts, outflows = [], []
            for r in newest_filings(self.bulk.rows(name, wanted, info), "oth"):
                tx = r[5].strip() if len(r) > 5 else ""
                if tx.startswith("2"):
                    if (row := outflow_row(r, committees[r[0]], cycle)) is not None:
                        outflows.append(row)
                elif (row := contribution_row(r, committees[r[0]], cycle, "oth")) is not None:
                    receipts.append(row)
            self._replace("fec_contributions", CONTRIBUTION_COLUMNS, "cycle = %s AND dataset = 'oth'", (cycle,), receipts)
            self._replace("fec_disbursements", DISBURSEMENT_COLUMNS, "cycle = %s AND dataset = 'oth'", (cycle,), outflows)
            self._record("oth", cycle, info.etag, len(receipts) + len(outflows), people)
            self.conn.commit()

        name = FecBulk.zip_name("oppexp", cycle)
        info = self._due("oppexp", cycle, name, full, today, people)
        if info:
            rows = [row for r in newest_filings(self.bulk.rows(name, wanted, info), "oppexp")
                    if (row := expense_row(r, committees[r[0]], cycle)) is not None]
            self._replace("fec_disbursements", DISBURSEMENT_COLUMNS, "cycle = %s AND dataset = 'oppexp'", (cycle,), rows)
            self._record("oppexp", cycle, info.etag, len(rows), people)
            self.conn.commit()

        name = FecBulk.zip_name("pas2", cycle)
        info = self._due("pas2", cycle, name, full, today, people)
        if info:
            givers = {r[0]: r for r in self._support_rows("cm", cycle) if r}
            principal = {cid: c for c, cid in self._campaign_committees(cycle).items()}
            gifts = (r for r in self.bulk.rows(name, info=info)
                     if len(r) > 16 and r[16] in people and r[5].strip() in GIFT_TYPES)
            rows = []
            for r in newest_filings(gifts, "pas2"):
                recipient = r[15].strip() if r[15].strip() in committees else principal.get(r[16], r[15].strip())
                if (row := committee_gift_row(r, people[r[16]], recipient, cycle, givers.get(r[0]))) is not None:
                    rows.append(row)
            self._replace("fec_contributions", CONTRIBUTION_COLUMNS, "cycle = %s AND dataset = 'pas2'", (cycle,), rows)
            self._record("pas2", cycle, info.etag, len(rows), people)
            self.conn.commit()

        name = FecBulk.ie_name(cycle)
        info = self._due("ie", cycle, name, full, today, people)
        if info:
            rows = outside_rows(self.bulk.csv_rows(name, info), people, cycle)
            self._replace("fec_outside", OUTSIDE_COLUMNS, "cycle = %s", (cycle,), rows)
            self._record("ie", cycle, info.etag, len(rows), people)
            self.conn.commit()

    def _campaign_committees(self, cycle: int) -> dict[str, str]:
        rows = self.conn.execute(
            "SELECT committee_id, candidate_id, designation FROM fec_committees WHERE cycle = %s AND is_campaign"
            " ORDER BY designation DESC", (cycle,)).fetchall()
        return {r["committee_id"]: r["candidate_id"] for r in rows}

    def _sync_reference(self, cycle: int, people: dict[str, int], full: bool, today: date) -> dict[str, int]:
        """The FEC's totals and committee list. Returns campaign committee -> person for the cycle."""
        name = FecBulk.zip_name("weball", cycle)
        info = self._due("weball", cycle, name, full, today, people)
        if info:
            rows = [totals_row(r, people[r[0]], cycle) for r in self.bulk.rows(name, info=info) if r and r[0] in people]
            self._check_shrink("fec_cycles", "cycle = %s", (cycle,), len(rows), floor=1)
            with self.conn.cursor() as cur:
                cur.execute("DELETE FROM fec_cycles WHERE cycle = %s", (cycle,))
                cur.executemany(
                    "INSERT INTO fec_cycles (candidate_id, cycle, person_pk, candidate_name, office, state, district, party,"
                    " receipts, disbursements, from_individuals, from_pacs, from_parties, candidate_contributions,"
                    " candidate_loans, other_loans, transfers_in, transfers_out, individual_refunds, committee_refunds,"
                    " cash_start, cash_end, debts, coverage_end)"
                    " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", rows)
            self._record("weball", cycle, info.etag, len(rows), people)
            self.conn.commit()

        # The candidate file exists for every cycle. The linkage file only starts in 2000, so for
        # older cycles the candidate file's principal committee is all there is.
        name = FecBulk.zip_name("cn", cycle)
        info = self._due("cn", cycle, name, full, today, people)
        if info:
            names = {r[0]: r for r in self._support_rows("cm", cycle) if r}
            found: dict[str, tuple] = {}
            for r in self._support_rows("ccl", cycle):
                if len(r) > 5 and r[0] in people:
                    found[r[3]] = (r[3], cycle, people[r[0]], r[0], C.text(names.get(r[3], ["", None])[1]), r[5],
                                   r[5] in ("P", "A"))
            for r in self.bulk.rows(name, info=info):
                if len(r) > 9 and r[0] in people and r[9] and r[9] not in found:
                    found[r[9]] = (r[9], cycle, people[r[0]], r[0], C.text(names.get(r[9], ["", None])[1]), "P", True)
            self._check_shrink("fec_committees", "cycle = %s", (cycle,), len(found), floor=1)
            with self.conn.cursor() as cur:
                cur.execute("DELETE FROM fec_committees WHERE cycle = %s", (cycle,))
                cur.executemany(
                    "INSERT INTO fec_committees (committee_id, cycle, person_pk, candidate_id, name, designation, is_campaign)"
                    " VALUES (%s,%s,%s,%s,%s,%s,%s)", list(found.values()))
            self._record("cn", cycle, info.etag, len(found), people)
            self.conn.commit()

        rows = self.conn.execute(
            "SELECT committee_id, person_pk FROM fec_committees WHERE cycle = %s AND is_campaign", (cycle,)).fetchall()
        self.conn.commit()
        return {r["committee_id"]: r["person_pk"] for r in rows}

    # ---- Totals the pages read -------------------------------------------------------------

    def rebuild_rollups(self) -> dict:
        c = self.conn
        c.execute(
            "UPDATE fec_cycles k SET"
            " itemized_individuals = coalesce((SELECT sum(x.amount) FROM fec_contributions x JOIN fec_committees m"
            "   ON m.committee_id = x.committee_id AND m.cycle = x.cycle WHERE m.candidate_id = k.candidate_id"
            "   AND x.cycle = k.cycle AND x.dataset = 'indiv' AND NOT x.is_memo AND NOT x.is_refund"
            "   AND x.source_type <> %(own)s), 0),"   # a candidate's own money is reported separately
            " itemized_committees = coalesce((SELECT sum(x.amount) FROM fec_contributions x JOIN fec_committees m"
            "   ON m.committee_id = x.committee_id AND m.cycle = x.cycle WHERE m.candidate_id = k.candidate_id"
            "   AND x.cycle = k.cycle AND x.dataset = 'pas2' AND NOT x.is_memo), 0),"
            " itemized_operating = coalesce((SELECT sum(x.amount) FROM fec_disbursements x JOIN fec_committees m"
            "   ON m.committee_id = x.committee_id AND m.cycle = x.cycle WHERE m.candidate_id = k.candidate_id"
            "   AND x.cycle = k.cycle AND x.dataset = 'oppexp' AND NOT x.is_memo), 0)",
            {"own": C.SELF_FUNDED},
        )
        roll: dict[int, dict] = {}

        def add(key: str, rows) -> None:
            for r in rows:
                roll.setdefault(r["person_pk"], {}).setdefault(key, []).append(
                    {k: (float(v) if k == "total" else v) for k, v in r.items() if k != "person_pk"})

        add("by_giver_type", c.execute(
            "SELECT person_pk, source_type AS type, sum(amount) AS total, count(*) AS n FROM fec_contributions"
            " WHERE dataset = 'pas2' AND NOT is_memo GROUP BY 1, 2 ORDER BY 1, 3 DESC"))
        add("top_committees", c.execute(
            "SELECT person_pk, name, other_id, type, connected_org, total, n FROM ("
            " SELECT person_pk, other_id, mode() WITHIN GROUP (ORDER BY name) AS name,"
            " mode() WITHIN GROUP (ORDER BY source_type) AS type, max(connected_org) AS connected_org,"
            " sum(amount) AS total, count(*) AS n,"
            " row_number() OVER (PARTITION BY person_pk ORDER BY sum(amount) DESC) AS rn"
            " FROM fec_contributions WHERE dataset = 'pas2' AND NOT is_memo GROUP BY person_pk, other_id) t"
            " WHERE rn <= 25 ORDER BY 1, 6 DESC"))
        people_filter = f"source_type = '{INDIVIDUALS}' AND NOT is_memo AND NOT is_refund"
        add("by_state", c.execute(
            "SELECT person_pk, state, total, n FROM ("
            " SELECT person_pk, upper(state) AS state, sum(amount) AS total, count(*) AS n,"
            " row_number() OVER (PARTITION BY person_pk ORDER BY sum(amount) DESC) AS rn"
            f" FROM fec_contributions WHERE {people_filter} AND state IS NOT NULL AND upper(state) <> 'WA'"
            " GROUP BY 1, 2) t WHERE rn <= 12 ORDER BY 1, 3 DESC"))
        add("by_city", c.execute(
            "SELECT person_pk, city, total, n FROM ("
            " SELECT person_pk, initcap(city) || coalesce(', ' || upper(state), '') AS city, sum(amount) AS total,"
            " count(*) AS n, row_number() OVER (PARTITION BY person_pk ORDER BY sum(amount) DESC) AS rn"
            f" FROM fec_contributions WHERE {people_filter} AND city IS NOT NULL"
            " GROUP BY person_pk, initcap(city), upper(state)) t WHERE rn <= 15 ORDER BY 1, 3 DESC"))
        # Same rules as the state rollup: stop-list answers, donors who typed their own name as the
        # employer ('Michael Murr LLC' for MURR, MICHAEL), and firms with too few donors don't rank.
        add("top_employers", c.execute(
            "SELECT person_pk, employer, total, n FROM ("
            " SELECT person_pk, max(employer) AS employer, sum(amount) AS total, count(*) AS n,"
            " row_number() OVER (PARTITION BY person_pk ORDER BY sum(amount) DESC) AS rn"
            " FROM (SELECT *, array_remove(regexp_split_to_array(replace(upper(name), chr(39), ''), '[^A-Z0-9]+'), '') AS donor,"
            "   array_remove(regexp_split_to_array(replace(upper(employer), chr(39), ''), '[^A-Z0-9]+'), '') AS works"
            f"   FROM fec_contributions WHERE {people_filter} AND employer IS NOT NULL AND upper(employer) !~ %s) m"
            " WHERE NOT (donor <@ works OR works <@ donor)"
            " GROUP BY person_pk, " + EMPLOYER_GROUP + " HAVING count(DISTINCT donor) >= %s) t"
            " WHERE rn <= 15 ORDER BY 1, 3 DESC", (EMPLOYER_STOP, MIN_EMPLOYER_DONORS)))
        add("by_spend", c.execute(
            "SELECT person_pk, category, sum(amount) AS total, count(*) AS n FROM fec_disbursements"
            " WHERE NOT is_memo GROUP BY 1, 2 ORDER BY 1, 3 DESC"))
        add("top_payees", c.execute(
            "SELECT person_pk, name, total, n FROM ("
            " SELECT person_pk, max(name) AS name, sum(amount) AS total, count(*) AS n,"
            " row_number() OVER (PARTITION BY person_pk ORDER BY sum(amount) DESC) AS rn"
            " FROM fec_disbursements WHERE name IS NOT NULL AND NOT is_memo AND category <> 'Refunds of contributions'"
            " AND coalesce(purpose, '') !~* '\\mrefund' AND upper(name) !~ %s"
            " GROUP BY person_pk, upper(name)) t WHERE rn <= 15 ORDER BY 1, 3 DESC", (PAYEE_LUMP_SUM,)))
        rows = c.execute(
            "WITH item AS (SELECT person_pk, count(*) FILTER (WHERE NOT is_memo) AS n,"
            f"  sum(amount) FILTER (WHERE {people_filter} AND upper(state) = 'WA') AS wa,"
            f"  sum(amount) FILTER (WHERE {people_filter} AND state IS NOT NULL AND upper(state) <> 'WA') AS out"
            "  FROM fec_contributions GROUP BY 1),"
            " totals AS (SELECT person_pk, sum(receipts) AS raised, sum(disbursements) AS spent, count(*) AS n,"
            "  min(cycle) AS first_cycle, max(cycle) AS last_cycle, sum(from_individuals) AS individuals,"
            "  sum(itemized_individuals) AS itemized, sum(from_pacs) AS pacs, sum(from_parties) AS parties,"
            "  sum(candidate_contributions) AS own_gifts, sum(candidate_loans) AS own_loans, sum(other_loans) AS loans,"
            "  sum(transfers_in) AS transfers FROM fec_cycles GROUP BY 1),"
            " latest AS (SELECT DISTINCT ON (person_pk) person_pk, receipts, cash_end, debts FROM fec_cycles"
            "  ORDER BY person_pk, cycle DESC, receipts DESC)"
            " SELECT p.id, t.*, i.n AS items, i.wa, i.out, l.receipts AS latest_receipts, l.cash_end, l.debts"
            " FROM money_people p JOIN totals t ON t.person_pk = p.id LEFT JOIN item i ON i.person_pk = p.id"
            " LEFT JOIN latest l ON l.person_pk = p.id"
        ).fetchall()
        updates = []
        for r in rows:
            parts = reported_sources(r)
            rollup = roll.get(r["id"], {})
            rollup["by_source"] = parts
            updates.append({
                "id": r["id"], "raised": r["raised"], "spent": r["spent"], "n": r["n"], "first": r["first_cycle"],
                "last": r["last_cycle"], "latest": r["latest_receipts"], "cash": r["cash_end"], "debts": r["debts"],
                "items": r["items"] or 0, "wa": r["wa"] or 0, "out": r["out"] or 0,
                "top": top_source(parts), "rollup": Jsonb(rollup),
            })
        with c.cursor() as cur:
            cur.executemany(
                "UPDATE money_people SET fed_raised = %(raised)s, fed_spent = %(spent)s, fed_cycles = %(n)s,"
                " fed_first_cycle = %(first)s, fed_last_cycle = %(last)s, fed_latest_raised = %(latest)s,"
                " fed_cash_on_hand = %(cash)s, fed_debts = %(debts)s, fed_contributions = %(items)s,"
                " fed_in_state = %(wa)s, fed_out_of_state = %(out)s, fed_top_source = %(top)s,"
                " fed_rollup = %(rollup)s, updated_at = now() WHERE id = %(id)s",
                updates,
            )
        c.commit()
        return {"people_with_federal_records": len(updates)}

    def reconciliation(self) -> dict:
        """How the itemized rows compare with what the FEC reports, overall."""
        r = self.conn.execute(
            "SELECT sum(from_individuals) AS individuals, sum(itemized_individuals) AS itemized,"
            " sum(from_pacs + from_parties) AS committees, sum(itemized_committees) AS committee_rows,"
            " sum(receipts) AS receipts, sum(disbursements) AS disbursements, sum(itemized_operating) AS operating,"
            " count(*) AS cycles, count(*) FILTER (WHERE itemized_operating > disbursements) AS cycles_operating_above_reported"
            " FROM fec_cycles").fetchone()
        self.conn.commit()
        return {k: float(v or 0) for k, v in r.items()}

    def run(self, full: bool = False, today: date | None = None) -> dict:
        today = today or date.today()
        people = self._people()
        if not people:
            return {"note": "no federal officials in money_people yet"}
        for cycle in all_cycles(today):
            self.sync_cycle(cycle, people, full, today)
        self.stats.update(self.rebuild_rollups())
        self.stats["reconciliation"] = self.reconciliation()
        return self.stats


def reported_sources(r: dict) -> list[dict]:
    """Where the money came from, using the FEC's reported totals, so the parts add up to its total.
    Small donors are the part of individuals' money that isn't itemized."""
    itemized = float(r["itemized"] or 0)
    individuals = float(r["individuals"] or 0)
    named = [("Political action committees", r["pacs"]), ("Party committees", r["parties"]),
             (C.SELF_FUNDED, r["own_gifts"]), ("Loans from the candidate", r["own_loans"]), ("Other loans", r["loans"]),
             ("Transfers from other committees", r["transfers"])]
    parts = [{"type": INDIVIDUALS, "total": min(itemized, individuals)},
             {"type": SMALL_GIFTS, "total": max(individuals - itemized, 0.0)}]
    parts += [{"type": label, "total": float(v or 0)} for label, v in named]
    accounted = individuals + sum(float(v or 0) for _, v in named)
    parts.append({"type": "Other receipts (interest, refunds, offsets)", "total": round(float(r["raised"] or 0) - accounted, 2)})
    return sorted([p for p in parts if abs(p["total"]) >= 0.005], key=lambda p: -p["total"])


def top_source(parts: list[dict]) -> str | None:
    """The largest source. Itemized and small gifts are both money from individuals, so they compete as one."""
    individuals = sum(p["total"] for p in parts if p["type"] in (INDIVIDUALS, SMALL_GIFTS))
    others = [p for p in parts if p["type"] not in (INDIVIDUALS, SMALL_GIFTS)]
    largest = max(others, key=lambda p: p["total"], default=None)
    if individuals > 0 and (largest is None or individuals >= largest["total"]):
        return "Individuals"
    return largest["type"] if largest else None


def load_federal_roster() -> list[dict]:
    path = Path(__file__).parent.parent / "data" / "money_officials.json"
    return json.loads(path.read_text()).get("federal", [])
