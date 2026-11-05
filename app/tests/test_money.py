"""Unit tests for the money trail code: cleaning, linking, and privacy rules."""
import json
from decimal import Decimal

import pytest

from wapeople.money import clean as C
from wapeople.money.disclosure import from_json, sanitize
from wapeople.money.link import first_name_matches, group_by_person, link_member, name_tokens
from wapeople.money.sync import CAMPAIGN_SELECT, ITEM_SETS, load_roster


# ---- Cleaning ---------------------------------------------------------------------------

def test_text_decodes_entities_and_whitespace():
    assert C.text("LAW OFFICES OF SMALL, SNELL, WEISS &AMP; COMFORT, P.S.") == "LAW OFFICES OF SMALL, SNELL, WEISS & COMFORT, P.S."
    assert C.text("  A   B ") == "A B"
    assert C.text("   ") is None
    assert C.text(None) is None


def test_money_and_dates_never_raise():
    assert C.money("150.5") == Decimal("150.50")
    assert C.money("junk") == Decimal("0.00")
    assert C.money_or_none("") is None
    assert C.day("2026-08-18T00:00:00.000").isoformat() == "2026-08-18"
    assert C.day("not a date") is None
    assert C.integer("2024") == 2024
    assert C.integer(None) is None


def test_link_accepts_only_web_addresses():
    assert C.link({"url": "https://my.pdc.wa.gov/x"}) == "https://my.pdc.wa.gov/x"
    assert C.link("https://my.pdc.wa.gov/x") == "https://my.pdc.wa.gov/x"
    assert C.link("javascript:alert(1)") is None
    assert C.link({"url": None}) is None


def test_name_key_groups_spelling_not_different_names():
    assert C.name_key("Amazon.com, Inc.") == C.name_key("AMAZON COM INC")
    assert C.name_key("AT&T") == C.name_key("at & t")
    assert C.name_key("AMAZON") != C.name_key("AMAZON.COM SERVICES INC")
    assert C.name_key("O'Brien Law") == "OBRIEN LAW"


@pytest.mark.parametrize("a,b", [
    ("WA St Auto Dealers PAC", "Washington State Auto Dealers Political Action Committee"),
    ("WA. STATE DENTAL PAC", "Washington state Dental Pac"),
    ("HOUSE DEMOCRATIC CAMPAIGN COMMITTEE (HDCC)", "HDCC"),
    ("House Demo Camp Comm", "House Democratic Campaign Committee"),
    ("WA ST REPUB PARTY NON EXEMPT", "Washington State Republican Party"),
    ("Washington State Democratic Committee - NonExempt", "WA State Democrats"),
    ("The Boeing Company", "Boeing Company"),
    ("Avista Corp", "Avista Corporation"),
])
def test_org_key_merges_abbreviations_of_one_name(a, b):
    assert C.org_key(a) == C.org_key(b)


@pytest.mark.parametrize("a,b", [
    ("Amazon", "Amazon.com Services Inc"),
    ("Washington Realtors PAC", "Washington Dental PAC"),
    ("House Democratic Campaign Committee", "Senate Democratic Campaign"),
    ("WA Beverage Association", "WA Beer and Wine Distributors Association"),
])
def test_org_key_keeps_different_names_apart(a, b):
    assert C.org_key(a) != C.org_key(b)


def test_org_key_handles_empty_names():
    assert C.org_key(None) == ""
    assert C.org_key("(HDCC)") == ""


def test_org_slug_round_trip():
    key = C.name_key("Washington Realtors PAC")
    assert C.slug_to_key(C.key_to_slug(key)) == key


@pytest.mark.parametrize("code,category,name,expected", [
    ("Individual", "Individual", "Jane Doe", "Individuals"),
    ("Business", "Organization", "Acme", "Businesses"),
    ("Political Action Committee", "Organization", "X PAC", "Political action committees"),
    ("Self", "Individual", "Jane Doe", C.SELF_FUNDED),
    ("Other", "Other", "SMALL CONTRIBUTIONS", C.SMALL_CONTRIBUTIONS),
    ("Other", "Individual", "small contributions", C.SMALL_CONTRIBUTIONS),
    ("Other", "Organization", "MISCELLANEOUS RECEIPTS", C.MISCELLANEOUS),
    ("Other", "Individual", "PROCEEDS FROM LOW COST FUNDRAISER", C.LOW_COST),
    ("Other", "Organization", "A Tribe", "Other organizations"),
    ("Other", "Individual", "A Person", "Other sources"),
])
def test_source_type(code, category, name, expected):
    assert C.source_type(code, category, name) == expected


def test_is_org_keeps_people_out_of_organization_pages():
    assert C.is_org("Business", "Organization")
    assert C.is_org("Union", None)
    assert not C.is_org("Individual", "Individual")
    assert not C.is_org("Self", "Individual")
    # Report lump-sum lines aren't organizations, even when filed under that category.
    assert not C.is_org("Other", "Organization", "MISCELLANEOUS RECEIPTS")
    assert not C.is_org("Other", "Organization", "SMALL CONTRIBUTIONS")


def test_spend_category_prefers_the_filers_code():
    assert C.spend_category(" Radio advertising ", "printing") == "Radio advertising"
    assert C.spend_category(None, "POSTAGE FOR MAILER") == "Postage costs, mail permits, purchase of stamps"
    assert C.spend_category("", "Yard signs") == "Printing campaign signs"
    assert C.spend_category(None, "Consulting") == "Management and consulting services"


def test_spend_category_does_not_guess():
    assert C.spend_category(None, "misc") == C.UNCATEGORIZED
    assert C.spend_category(None, None) == C.UNCATEGORIZED


@pytest.mark.parametrize("value", ["14715 182ND PL SE", "15369 Broom Street NE", "1701 JFK BOULEVARD", "210 4TH AVE. SW",
                                   "000000", "108", "P.O. Box 125", "PO BOX 9100"])
def test_street_addresses_in_a_city_field_are_dropped(value):
    assert C.place(value) is None


@pytest.mark.parametrize("value", ["Seattle", "Walla Walla", "Coeur d'Alene", "St. John", "Point Roberts"])
def test_real_cities_are_kept(value):
    assert C.place(value) == value


@pytest.mark.parametrize("value", ["13420 339TH AVE SE", "631 Strander Blvd #F", "3302 FUHRMAN AVE. E STE 111"])
def test_street_addresses_in_an_employer_field_are_dropped(value):
    assert C.employer(value) is None


@pytest.mark.parametrize("value", ["Boeing", "3M Company", "7-Eleven", "King County", "Self Employed", "Fifth Third Bank"])
def test_real_employers_are_kept(value):
    assert C.employer(value) == value


def test_zip_codes_must_look_like_zip_codes():
    assert C.zip_code("98122") == "98122"
    assert C.zip_code("98122-5204") == "98122"      # nine digits can point to one building
    assert C.zip_code("981225204") == "98122"
    for junk in ("'0709", "+90", ", 983", "09/24", "", None):
        assert C.zip_code(junk) is None


def test_addresses_inside_descriptions_are_removed_and_the_rest_kept():
    s = C.free_text("OFFICE SPACE RENT LOCATED AT 715 YELM AVE E SUITE 7, YELM 98597")
    assert "715" not in s and "YELM AVE" not in s and C.ADDRESS_REMOVED in s and s.startswith("OFFICE SPACE RENT")
    s = C.free_text("Polling: GQR (1101 15th St NW Washington, DC)")
    assert "1101" not in s and s.startswith("Polling: GQR")
    assert "Box" not in C.free_text("Mail to PO Box 12066 please")


@pytest.mark.parametrize("value", ["Printing 12,500 postcards", "Consulting for 2024 race", "10 yard signs",
                                   "Ad in 3 papers", "100 stamps"])
def test_ordinary_descriptions_are_untouched(value):
    assert C.free_text(value) == value


# ---- Linking people ----------------------------------------------------------------------

BILLIG = {"id": 1, "first_name": "Andy", "last_name": "Billig"}
TERMS = [{"biennium": "2021-22", "agency": "Senate", "district": "3"}]


def row(pid, name, office="STATE SENATOR", district="03", year="2020"):
    return {"person_id": str(pid), "filer_name": name, "office": office, "legislative_district": district,
            "election_year": year}


def test_links_on_name_chamber_district_and_time():
    people = group_by_person([row(1076, "Andrew Billig (Andy Billig)")])
    link = link_member(BILLIG, TERMS, people)
    assert link.person_ids == [1076]
    assert link.method


def test_nickname_links_to_formal_name():
    people = group_by_person([row(1076, "BILLIG ANDREW S")])
    assert link_member(BILLIG, TERMS, people).person_ids == [1076]


def test_same_last_name_different_first_name_never_links():
    # The real case: Marlo Braun briefly held John Braun's Senate seat.
    people = group_by_person([row(1080, "John E Braun (JOHN BRAUN)", district="20")])
    member = {"id": 2, "first_name": "Marlo", "last_name": "Braun"}
    terms = [{"biennium": "2017-18", "agency": "Senate", "district": "20"}]
    assert link_member(member, terms, people).person_ids == []


@pytest.mark.parametrize("changed", [
    {"district": "04"},                          # another district
    {"office": "STATE REPRESENTATIVE"},          # the other chamber
    {"year": "2010"},                            # long before the term
    {"year": "2030"},                            # long after it
])
def test_wrong_seat_or_time_never_links(changed):
    people = group_by_person([{**row(1076, "Andrew Billig"), **{
        ("legislative_district" if k == "district" else "election_year" if k == "year" else k): v
        for k, v in changed.items()}}])
    assert link_member(BILLIG, TERMS, people).person_ids == []


def test_two_matching_people_means_no_link():
    people = group_by_person([row(1, "Andrew Billig"), row(2, "Andrew S. Billig")])
    link = link_member(BILLIG, TERMS, people)
    assert link.person_ids == []
    assert "Ambiguous" in link.note


def test_manual_link_carries_its_note():
    link = link_member({"id": 3, "first_name": "JD", "last_name": "Rossetti"}, [], {},
                       {"person_ids": [4015], "note": "J.D. files as Justin."})
    assert link.person_ids == [4015]
    assert link.method == "manual" and link.note == "J.D. files as Justin."


def test_hyphen_and_accent_names():
    assert name_tokens("Montoya-Lewis, Raquél") == ["MONTOYA", "LEWIS", "RAQUEL"]
    assert first_name_matches(["BOB"], {"ROBERT"})
    assert first_name_matches(["KATHY"], {"KATHRYN"})
    assert not first_name_matches(["JOHN"], {"MARLO"})


def test_roster_is_complete_and_points_at_official_sources():
    roster = load_roster()
    kinds = [o["kind"] for o in roster["officials"]]
    assert kinds.count("statewide") == 9
    assert kinds.count("justice") == 9
    assert len({o["person_id"] for o in roster["officials"]}) == 18
    assert all(o["source_url"].startswith("https://") for o in roster["officials"])
    assert all(v["note"] for v in roster["manual_links"].values())


# ---- Privacy -----------------------------------------------------------------------------

@pytest.mark.parametrize("field_list", [s.select for s in ITEM_SETS] + [CAMPAIGN_SELECT])
def test_private_columns_are_never_requested(field_list):
    for word in ("address", "phone", "email", "location", "treasurer"):
        assert word not in field_list


STATEMENT = {"statement": {
    "judge_or_law_enforcement": False,
    "spouse": "Pat Secretname",
    "dependents": [{"name": "Kid Secretname"}],
    "income": {"data": [
        {"who": ["filer"], "name": "Acme Co", "why": "Salary", "income": "30000-59999",
         "address": "1 SECRET ST", "postcode": "98765", "city": "Spokane", "state": "WA", "category": "employment"},
        {"who": ["filer"], "name": "Rentals", "why": "Rent from 77 SECRET LANE", "income": "0-29999",
         "city": "78 SECRET ROAD", "state": "WA"},
        {"who": ["spouse"], "name": "Beta LLC", "why": "Wages", "income": "0-29999", "address": "2 SECRET ST",
         "city": "Olympia", "state": "WA"},
        {"who": ["dependent"], "name": "Kid Job Secret", "why": "Wages", "income": "0-29999"},
    ]},
    "accounts": {"data": [{"who": ["filer"], "name": "BECU", "address": "3 SECRET ST", "postcode": "98765",
                           "city": "Tukwila", "state": "WA", "assets": [{"nature": "Savings", "value": "0-29999"}]}]},
    "business_associations": {"data": [{"who": ["filer"], "legal_name": "Legal LLC", "operating_name": "Ops",
                                        "address": "4 SECRET ST", "wa_property": [{"address": "5 SECRET ST"}],
                                        "agency_payments": [{"agency": "DOT", "purpose": "Consulting"}]}]},
    "real_estate": [{"who": ["filer"], "assessed_value": "100000-199999", "purchased": False, "sold": True,
                     "location": {"address": "6 SECRET ST", "city": "Spokane", "zip": "98765", "parcel": "SECRET-PARCEL"},
                     "purchaser": {"name": "Secret Buyer", "address": "7 SECRET ST"},
                     "creditors": [{"name": "BECU", "address": "8 SECRET ST", "current_amount": "0-29999"}]}],
    "creditors": [{"who": ["filer"], "name": "Big Bank", "address": "9 SECRET ST", "ending_amount": "0-29999"}],
    "gifts": [{"who": ["filer"], "donor_name": "NCSL", "value": "710.82", "description": "Travel",
               "date_received": "2019-06-25", "city": "Denver", "state": "CO"}],
    "lobbies": [{"who": ["filer"], "for": "A Client", "purpose": "Lobbying", "compensation": "1000"}],
    "reporting_modifications": {"residential_address": True},
    "brand_new_field_in_the_source": "SECRET",
}}


def test_statement_keeps_the_money_trail():
    d = sanitize(STATEMENT)
    assert d["income"][0]["source"] == "Acme Co" and d["income"][0]["amount"] == "30000-59999"
    assert d["income"][2]["who"] == ["Spouse or partner"]
    assert d["businesses"][0]["paid_by_state_agencies"] == [{"agency": "DOT", "purpose": "Consulting"}]
    assert d["gifts"][0]["donor"] == "NCSL"
    assert d["lobbying"][0]["for"] == "A Client"
    assert d["real_estate"][0]["city"] == "Spokane" and d["real_estate"][0]["sold"] is True


def test_statement_drops_addresses_and_family_names():
    text = json.dumps(sanitize(STATEMENT))
    for secret in ("SECRET", "Secretname", "98765", "Secret Buyer"):
        assert secret not in text


def test_statement_drops_items_reported_only_for_a_dependent():
    assert [i["source"] for i in sanitize(STATEMENT)["income"]] == ["Acme Co", "Rentals", "Beta LLC"]


def test_judges_property_locations_are_withheld():
    raw = json.loads(json.dumps(STATEMENT))
    raw["statement"]["judge_or_law_enforcement"] = True
    raw["statement"]["real_estate"][0]["location"]["county"] = "Spokane"
    d = sanitize(raw)
    assert d["location_withheld"] is True
    assert d["real_estate"][0]["city"] is None and d["real_estate"][0]["county"] is None


def test_from_json_handles_older_filings():
    assert from_json(None) is None
    assert from_json("") is None
    assert from_json("{not json") is None
    assert from_json(json.dumps(STATEMENT))["income"]


# ---- Display helpers ---------------------------------------------------------------------

def test_money_labels():
    from wapeople.web.main import office_label, position_label, range_label, usd, usd_cents, usd_short
    assert usd(1234.4) == "$1,234"
    assert usd(-25) == "-$25"
    assert usd(None) == "—"
    assert usd_cents(-25.5) == "-$25.50"
    assert usd_short(1_234_567) == "$1.2M"
    assert usd_short(45_300) == "$45K"
    assert usd_short(812) == "$812"
    assert usd_short(None) == "$0"
    assert office_label("SUPERINTENDENT OF PUBLIC INSTRUCTION") == "Superintendent of Public Instruction"
    assert position_label("State Representative Pos. 1") == "Position 1"
    assert position_label("7") == "Position 7"
    assert range_label("30000-59999") == "$30,000–$59,999"
    assert range_label("500000+") == "$500,000 or more"
    assert range_label("10000") == "$10,000"
    assert range_label("Unknown") == "Unknown"
    assert range_label(None) == "—"


def test_every_alias_points_at_a_name_that_is_its_own_key():
    # Otherwise a committee's donors and its registered filing would land on different pages.
    for target in C.ORG_ALIASES.values():
        assert C.org_key(target) == target
