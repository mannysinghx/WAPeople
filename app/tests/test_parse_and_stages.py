from datetime import date

from wapeople.legislature.parse import (
    base_bill_id,
    biennium_for_year,
    parse_date,
    roll_call_result,
    session_law,
    split_bill_id,
)
from wapeople.legislature.stages import biennium_status, compute_stage, is_activity_line, next_session_start
from wapeople.legislature.sync import compact_version, parse_roll_call, pick_current


def test_split_bill_id_handles_version_modifiers():
    assert split_bill_id("HB 1217") == ("HB", 1217)
    assert split_bill_id("EHB 1217") == ("HB", 1217)
    assert split_bill_id("E2SHB 1110") == ("HB", 1110)
    assert split_bill_id("2E2SSB 5000") == ("SB", 5000)
    assert split_bill_id("SSB 5001") == ("SB", 5001)
    assert split_bill_id("SR 8600") == ("SR", 8600)
    assert split_bill_id("ESSJM 8001") == ("SJM", 8001)
    assert split_bill_id("HI 2081") == ("HI", 2081)
    assert split_bill_id("garbage") is None
    assert base_bill_id("ESHB 1296") == "HB 1296"


def test_sga_is_parsed_as_appointment():
    assert split_bill_id("SGA 9001") == ("SGA", 9001)


def test_session_law_and_dates():
    assert session_law("C 209 L 25") == (2025, 209, None)
    assert session_law("C 4 L 21 E1") == (2021, 4, 1)
    assert session_law("C 1 L 92") == (1992, 1, None)
    assert session_law("H Approps") is None
    assert parse_date("2025-01-13T00:00:00") == date(2025, 1, 13)
    assert parse_date("0001-01-01T00:00:00") is None
    assert biennium_for_year(2026) == "2025-26"
    assert biennium_for_year(1999) == "1999-00"


def test_roll_call_result_matches_history_line():
    lines = [
        "Second reading, amended.",
        "Third reading, passed; yeas, 53; nays, 42; absent, 0; excused, 3.",
    ]
    assert roll_call_result(53, 42, lines) == "passed"
    assert roll_call_result(24, 22, ["Third reading, failed;  yeas, 24; nays, 22; absent, 0; excused, 3."]) == "failed"
    assert roll_call_result(49, 0, ["Confirmed; yeas, 49; nays, 0; absent, 0; excused, 0."]) == "confirmed"
    assert roll_call_result(10, 10, lines) is None


def _stage(**kw):
    base = dict(
        type_code="B", original_agency="House", status_code="H Approps", veto=False,
        partial_veto=False, passed_house=False, passed_senate=False, passed_legislature=False,
        introduced_date=date(2025, 1, 13),
        action_lines=["First reading, referred to Housing.", "Referred to Appropriations."],
        biennium_status="in_session",
    )
    base.update(kw)
    return compute_stage(**base)


def test_stage_committee_uses_full_committee_name():
    r = _stage()
    assert r.stage == "committee" and r.bucket == "proposed"
    assert r.label == "In House committee: Appropriations"


def test_stage_enacted_and_partial_veto():
    r = _stage(status_code="C 209 L 25", passed_house=True, passed_senate=True, passed_legislature=True)
    assert (r.stage, r.bucket) == ("law", "past")
    assert "Chapter 209, Laws of 2025" in r.label
    r = _stage(status_code="C 4 L 21 E1", partial_veto=True)
    assert r.stage == "law_partial_veto"
    assert "1st Special Session" in r.label


def test_stage_vetoed_and_passed_origin():
    assert _stage(status_code="Gov vetoed", veto=True).stage == "vetoed"
    r = _stage(status_code="S Ways & Means", passed_house=True,
               action_lines=["Third reading, passed; yeas, 60; nays, 38.", "First reading, referred to Ways & Means."])
    assert r.stage == "passed_origin" and r.bucket == "ongoing"
    assert r.label == "Passed House · now in Senate committee: Ways & Means"


def test_stage_rules_x_file_and_floor():
    r = _stage(status_code="S Rules X", original_agency="Senate")
    assert r.stage == "floor" and "X-file" in r.label
    assert _stage(status_code="H 2nd Reading").label == "On the House floor calendar (2nd Reading)"


def test_stage_overlay_when_session_over():
    r = _stage(biennium_status="adjourned_final")
    assert (r.stage, r.bucket) == ("did_not_pass", "past")
    assert r.label == "Did not pass · died in committee"
    r = _stage(status_code="S Rules 2", passed_house=True, biennium_status="closed")
    assert r.label == "Did not pass · passed House, stalled in Senate"
    r = _stage(biennium_status="interim")
    assert r.bucket == "proposed" and r.label.endswith("carries over to the next session")


def test_stage_resolutions_and_appointments():
    assert _stage(type_code="R", status_code="H Adopted").stage == "adopted"
    assert _stage(type_code="GA", original_agency="Senate", status_code="S Confirmed").stage == "confirmed"
    assert _stage(type_code="GA", status_code="S Term expired").stage == "not_confirmed"
    r = _stage(type_code="JR", status_code="S Filed Sec/St")
    assert r.stage == "filed" and "voters" in r.label
    assert _stage(status_code="", introduced_date=None).stage == "prefiled"


def test_biennium_status():
    assert biennium_status(2025, date(2026, 3, 12), today=date(2026, 10, 11)) == "adjourned_final"
    assert biennium_status(2025, date(2025, 4, 27), today=date(2025, 10, 11)) == "interim"
    assert biennium_status(2025, date(2026, 2, 1), today=date(2026, 2, 5)) == "in_session"
    assert biennium_status(2023, date(2024, 3, 7), today=date(2026, 10, 11)) == "closed"
    assert biennium_status(2027, None, today=date(2026, 12, 5)) == "upcoming"
    assert is_activity_line("Third reading, passed; yeas, 53; nays, 42.")
    assert not is_activity_line("By resolution, reintroduced and retained in present status.")
    assert not is_activity_line("Effective date 7/27/2025.")


def test_next_session_start_is_second_monday_of_january():
    assert next_session_start(date(2026, 10, 11)) == date(2027, 1, 11)
    assert next_session_start(date(2026, 1, 5)) == date(2026, 1, 12)


def test_compact_version_and_pick_current():
    recs = [
        {"Biennium": "2025-26", "BillId": "HB 1217", "BillNumber": "1217", "SubstituteVersion": "0",
         "EngrossedVersion": "0", "ShortLegislationType": {"ShortLegislationType": "B"},
         "OriginalAgency": "House", "Active": "false",
         "CurrentStatus": {"Status": "H not subst", "ActionDate": "0001-01-01T00:00:00"}},
        {"Biennium": "2025-26", "BillId": "EHB 1217", "BillNumber": "1217", "SubstituteVersion": "0",
         "EngrossedVersion": "1", "ShortLegislationType": {"ShortLegislationType": "B"},
         "OriginalAgency": "House", "Active": "true", "ShortDescription": "Rent stabilization",
         "PrimeSponsorID": "34024", "IntroducedDate": "2025-01-13T00:00:00",
         "CurrentStatus": {"Status": "C 209 L 25     ", "ActionDate": "2025-05-07T00:00:00"},
         "Companions": {"Companion": {"Biennium": "2025-26", "BillId": "SB 5222"}}},
    ]
    vs = [compact_version(r) for r in recs]
    cur = pick_current(vs)
    assert cur["version_id"] == "EHB 1217"
    assert cur["status"] == "C 209 L 25"
    assert cur["prime"] == 34024
    assert cur["companions"] == ["SB 5222"]


def test_parse_roll_call():
    rc = {
        "Agency": "House", "BillId": "EHB 1217", "Motion": "3rd Reading & Final Passage",
        "SequenceNumber": "15", "VoteDate": "2025-03-10T00:00:00",
        "YeaVotes": {"Count": "53"}, "NayVotes": {"Count": "42"},
        "AbsentVotes": {"Count": "0", "MembersVoting": ""}, "ExcusedVotes": {"Count": "3"},
        "Votes": {"Vote": [
            {"MemberId": "31526", "Name": "Abbarno", "VOte": "Nay"},
            {"MemberId": "31534", "Name": "Berg", "VOte": "Yea"},
        ]},
    }
    p = parse_roll_call(rc)
    assert (p["yeas"], p["nays"], p["excused"]) == (53, 42, 3)
    assert p["votes"] == [(31526, "Abbarno", "Nay"), (31534, "Berg", "Yea")]
    assert p["vote_date"] == date(2025, 3, 10)


def test_group_versions_keeps_live_copy_of_duplicated_version():
    from wapeople.legislature.sync import group_versions

    def rec(bill_id, active, status, when):
        return compact_version({
            "Biennium": "2025-26", "BillId": bill_id, "BillNumber": "2487",
            "SubstituteVersion": "0", "EngrossedVersion": "1" if bill_id.startswith("E") else "0",
            "ShortLegislationType": {"ShortLegislationType": "B"}, "OriginalAgency": "House",
            "Active": active, "CurrentStatus": {"Status": status, "ActionDate": when},
        })

    # The service returned a stale copy of EHB 2487 before the live one.
    vs = [rec("EHB 2487", "false", "H Amended", "0001-01-01T00:00:00"),
          rec("HB 2487", "false", "H 2nd Reading", "0001-01-01T00:00:00"),
          rec("EHB 2487", "true", "C 263 L 26", "2026-04-01T00:00:00")]
    groups = group_versions(vs)
    assert len(groups[(2487, "B")]) == 2
    assert pick_current(groups[(2487, "B")])["status"] == "C 263 L 26"
