"""Summary extraction against real staff documents from different eras (tests/fixtures)."""
from pathlib import Path

from wapeople.legislature.parse import version_modifiers
from wapeople.legislature.summaries import (
    extract_brief_summary,
    first_sentences,
    html_to_text,
    rank_candidates,
    summarize,
)

FIX = Path(__file__).parent / "fixtures"


def text(name):
    return html_to_text((FIX / name).read_bytes())


def test_modern_house_analysis_bullets():
    s = summarize(text("2025-26_1217_HBA_HOUS_25.htm"), {"Name": "1217 HBA HOUS 25"})
    bullets = s.split("\n")
    assert bullets[0].startswith("Limits rent and fee increases to 7 percent")
    assert 2 <= len(bullets) <= 5
    assert not any("Hearing Date" in b or "Staff" in b for b in bullets)


def test_senate_brief_summary_of_amended_bill():
    s = summarize(text("2025-26_1069_SBA_WM_25.htm"), {"Name": "1069 SBA WM 25"})
    assert s.startswith("Requires bargaining under the Personnel System Reform Act")
    assert "COMMITTEE" not in s and "Majority Report" not in s


def test_2003_bullets_wrapped_across_lines_are_joined():
    bullets = extract_brief_summary(text("2003-04_1019.HBA.htm"))
    assert bullets and bullets[0].startswith("Exempts from public disclosure personal identifying information")
    assert "law enforcement agencies with a subpoena" in bullets[0]


def test_1991_report_falls_back_to_summary_section():
    s = summarize(text("1991-92_5001.SBR.htm"), {"Name": "5001.SBR"})
    assert s and s.startswith("Where another state attempts to collect an income tax")
    assert len(s) <= 420


def test_final_bill_report_summary_section():
    s = summarize(text("2025-26_1003_HBR_FBR_25.htm"), {"Name": "1003 HBR FBR 25"})
    assert s and "notice" in s.lower() and len(s) <= 420
    assert not s.startswith("Background")


def test_digest():
    s = summarize(text("1997-98_1001.DIG.htm"), {"Name": "1001.DIG", "Class": "Digests"})
    assert s and "DIGEST" not in s and len(s) > 20


def test_rank_prefers_modern_report_of_latest_version():
    docs = [
        {"Name": "1217.E HBR FBR 25", "BillId": "EHB 1217", "HtmLastModifiedDate": "2025-05-20T00:00:00"},
        {"Name": "1217 HBA HOUS 25", "BillId": "HB 1217", "HtmLastModifiedDate": "2025-01-10T00:00:00"},
        {"Name": "1217.E SBR WM TA 25", "BillId": "EHB 1217", "HtmLastModifiedDate": "2025-04-01T00:00:00"},
        {"Name": "1217-S2 HBR APP 25", "BillId": "2SHB 1217", "HtmLastModifiedDate": "2025-02-20T00:00:00"},
        {"Name": "1217.DIG", "BillId": "HB 1217", "Class": "Digests"},
    ]
    ranked = [d["Name"] for d in rank_candidates(docs)]
    assert ranked[0] == "1217-S2 HBR APP 25"   # second substitute outranks the original
    assert ranked[-2:] == ["1217.E HBR FBR 25", "1217.DIG"]


def test_version_modifiers():
    assert version_modifiers("E2SHB 1110") == (2, 1)
    assert version_modifiers("SSB 5001") == (1, 0)
    assert version_modifiers("SB 5001") == (0, 0)
    assert version_modifiers("2ESSB 5000") == (1, 2)
    assert version_modifiers("SGA 9001") == (0, 0)


def test_first_sentences_keeps_abbreviations_together():
    s = first_sentences("Revises RCW 4.56.110 on interest. Applies to judgments after 2020. Third sentence.")
    assert s == "Revises RCW 4.56.110 on interest. Applies to judgments after 2020."


def test_version_label_lines_are_dropped():
    from wapeople.legislature.summaries import _bullets

    assert _bullets(["(As Amended by House)", "Makes changes to bargaining unit certification processes."]) == [
        "Makes changes to bargaining unit certification processes."]


def test_rank_prefers_report_on_the_version_that_became_law():
    # Reality: the service tags every document with the bill's current id (EHB 1217).
    docs = [
        {"Name": "1217-S2 HBR APP 25", "BillId": "EHB 1217", "HtmLastModifiedDate": "2025-02-20T00:00:00"},
        {"Name": "1217.E HBR PL 25", "BillId": "EHB 1217", "HtmLastModifiedDate": "2025-04-20T00:00:00"},
        {"Name": "1217 HBA HOUS 25", "BillId": "EHB 1217", "HtmLastModifiedDate": "2025-01-10T00:00:00"},
    ]
    assert rank_candidates(docs, "EHB 1217")[0]["Name"] == "1217.E HBR PL 25"
    assert rank_candidates(docs)[0]["Name"] == "1217-S2 HBR APP 25"  # no live version known


def test_doc_version_and_label():
    from wapeople.legislature.summaries import doc_version, version_id

    assert doc_version("1217-S2 HBR APP 25") == (2, 0)
    assert doc_version("1296-S.E HBR PL 25") == (1, 1)
    assert doc_version("1217.E SBA HSG 25") == (0, 1)
    assert doc_version("1019-S.HBR") == (1, 0)
    assert doc_version("1019.HBR") == (0, 0)
    assert doc_version("2034-S2.E HBR PL 26") == (2, 1)
    assert version_id("HB", 2034, (2, 1)) == "E2SHB 2034"
    assert version_id("SB", 5167, (1, 1)) == "ESSB 5167"
    assert version_id("HB", 1217, (0, 0)) == "HB 1217"
