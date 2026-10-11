from wapeople.courts.refs import extract_mentions
from wapeople.courts.sync import case_from_result, clean_snippet, merge_case, normalize_docket


def kinds(ms):
    return [(m["kind"], m.get("number") or m.get("chapter"), m.get("year")) for m in ms]


def test_long_form_bill_with_abbreviation_and_year():
    t = "The legislature passed Engrossed Substitute <mark>House Bill</mark> (ESHB) 1296 in 2025, and it was enacted"
    ms = extract_mentions(t)
    assert kinds(ms) == [("bill", 1296, 2025)]
    assert ms[0]["chamber"] == "H" and ms[0]["type_code"] == "B"


def test_session_law_and_short_form():
    t = ("Engrossed Substitute Senate Bill 5096 (ESSB 5096), Laws of 2021, ch. 196, imposes a tax. "
         "See also LAWS OF  2019, ch. 331, § 3 and Laws of 2023, 1st Spec. Sess., ch. 1.")
    ms = extract_mentions(t)
    laws = [(m["year"], m["chapter"], m["special"]) for m in ms if m["kind"] == "session_law"]
    assert laws == [(2021, 196, None), (2019, 331, None), (2023, 1, 1)]
    bills = [m for m in ms if m["kind"] == "bill"]
    assert len(bills) == 1 and bills[0]["number"] == 5096 and bills[0]["year"] == 2021


def test_undated_bill_kept_without_year_and_implausible_numbers_ignored():
    ms = extract_mentions("House Bill 1234 was discussed. Senate Bill 1234 is not a real number.")
    assert kinds(ms) == [("bill", 1234, None)]


def test_initiatives():
    ms = extract_mentions("Initiative Measure No. 695 and I-976 and Initiative 1631.")
    assert [m["number"] for m in ms if m["kind"] == "initiative"] == [695, 976, 1631]


def test_snippet_is_escaped_except_mark():
    s = clean_snippet('a <script>x</script> <mark>House Bill</mark> &amp; b')
    assert "<script>" not in s and "<mark>House Bill</mark>" in s and "&amp;" in s


def test_case_dedupe_key_and_merge():
    assert normalize_docket("No. 64654-6") == normalize_docket("64654-6") == "646546"
    r1 = {"caseName": "State v. Broadaway", "docketNumber": "No. 64654-6", "dateFiled": "1997-08-21",
          "cluster_id": 1, "citation": ["133 Wash. 2d 118"], "citeCount": 5,
          "opinions": [{"id": 10, "snippet": "x <mark>initiative measure</mark>"}],
          "absolute_url": "/opinion/1/x/"}
    r2 = dict(r1, docketNumber="64654-6", cluster_id=2, citation=["942 P.2d 363"], citeCount=9,
              opinions=[{"id": 11, "snippet": "y"}])
    a, b = case_from_result(r1, "ballot_measure"), case_from_result(r2, "constitutionality")
    assert a["case_key"] == b["case_key"]
    m = merge_case(a, b)
    assert m["cluster_ids"] == [1, 2] and m["cite_count"] == 9
    assert set(m["categories"]) == {"ballot_measure", "constitutionality"}
    assert m["citations"] == ["133 Wash. 2d 118", "942 P.2d 363"]


def test_discipline_cases_excluded():
    r = {"caseName": "In re Disciplinary Proceeding Against Smith", "dateFiled": "2001-01-01", "cluster_id": 3}
    assert case_from_result(r, "constitutionality") is None
