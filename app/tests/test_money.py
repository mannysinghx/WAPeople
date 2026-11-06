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


@pytest.mark.parametrize("raw,gone,kept", [
    ("CELL PHONE 425 736 9043", "736", "CELL PHONE"),
    ("MOBLIE 509-322-0507", "322", "MOBLIE"),
    ("MOBLE 5093220507", "5093220507", "MOBLE"),
    ("4258778127 Madelynn Sakay", "4258778127", "Madelynn Sakay"),
    ("ARROW LUMBER, ORTING 360.893.2200", "893", "ARROW LUMBER, ORTING"),
    ("Call (206) 555-0142 today", "555", "Call"),
    ("Todd Toddbowers66@gmail.com", "gmail", "Todd"),
    ("SARAH@BEEMER-MUMMA.COM", "BEEMER", ""),
])
def test_phone_numbers_and_emails_are_removed_from_free_text(raw, gone, kept):
    s = C.free_text(raw)
    assert gone not in s and kept in s


@pytest.mark.parametrize("value", [
    "Texting 7-23: 9,205 MMS @0.0450",          # an at sign alone isn't an email address
    "1 sign @38.99",
    "INV 8005551234",                           # ten bare digits without a local area code stay
    "ACCT 1234567890",
    "Printing 21,622 pieces",
])
def test_numbers_that_are_not_phones_or_emails_stay(value):
    assert C.free_text(value) == value


def test_zip_plus_four_is_cut_to_five_digits_in_free_text():
    s = C.free_text("Childcare - Bonni Lafferty, Tacoma WA 98465-2129")
    assert s.endswith("Tacoma WA 98465") and "2129" not in s


@pytest.mark.parametrize("raw,gone", [
    ("SUSAN PICATTI DESIGN, 711 20TH E., SEATTLE, WA 98122", "711"),
    ("Hype Strategies(1500 NW 119th Vancouver, WA 98685) Mailer printing", "1500"),
    ("Event Food - QFC 1401 Broadway Seattle, WA 98122", "1401"),
    ("Sale of property 922 5th capital gains", "922"),
    ("Alex Roderick, [address removed] West University Pl, University Place WA 98467", "West University"),
    ("CEGNAR COMPANYP.O. BOX 1393", "1393"),
    ("RENEWAL OF POST OFFICE BOX #470", "470"),
    ("TRICE TALENT, PO BX 2563 PRINCE FREDERICK, MD 20678", "2563"),
    ("Ward Media LLC, PMB 232, Poulsbo", "232"),
])
def test_more_street_forms_are_removed(raw, gone):
    s = C.free_text(raw)
    assert gone not in s and C.ADDRESS_REMOVED in s


@pytest.mark.parametrize("value", [
    "500 1st class stamps", "1000 3rd party mailers", "Postcards for 2024 1st quarter", "2,500 postcards to Tacoma",
    "Mailer printing (21,622)", "Paid 09/18/2025",
])
def test_counts_and_ordinary_text_are_not_mistaken_for_addresses(value):
    assert C.free_text(value) == value


def test_a_po_box_without_a_number_only_loses_the_box():
    assert C.free_text("PO BOX RENTAL") == "[address removed] RENTAL"


@pytest.mark.parametrize("value", ["jane@example.com", "425-736-9043"])
def test_contact_details_in_a_city_or_employer_field_are_dropped(value):
    assert C.place(value) is None
    assert C.employer(value) is None
    assert C.zip_code(value) is None


def test_employer_loses_a_glued_po_box_but_real_employers_stay():
    assert C.employer("CEGNAR COMPANYP.O. BOX 1393") is None
    assert C.employer("Microsoft") == "Microsoft"
    assert C.employer("JEAN LEONARD LOBBYIST/ATTORNEY@LAW") == "JEAN LEONARD LOBBYIST/ATTORNEY@LAW"


@pytest.mark.parametrize("raw,expected", [
    ("KUPER GLENN 1542", "KUPER GLENN"),
    ("OLSON JULIE 16708", "OLSON JULIE"),
    ("LYON, DOROTHY NE 18TH", "LYON, DOROTHY"),
    ("EDWARD, D NE 18TH", "EDWARD, D"),
    ("KNIGHT CATHERINE6", "KNIGHT CATHERINE"),
    ("DUTELLE AUTUMN 27.0", "DUTELLE AUTUMN"),
    ("Todd Toddbowers66@gmail.com", "Todd"),
    ("Jane Doe 123 Main St", "Jane Doe"),
    ("jerry@sauerdevgroup.com", None),
    (None, None),
    # A generation isn't a street, and a person with no number loses nothing.
    ("WOLF RUSSELL R 3RD", "WOLF RUSSELL R 3RD"),
    ("Mary Smith", "Mary Smith"),
    # Filers code unions and shops as people too. Their numbers are part of the name.
    ("IBEW LOCAL 76", "IBEW LOCAL 76"),
    ("PSE SEIU 1948", "PSE SEIU 1948"),
    ("INT'L UNION OF OP ENGINEERS 302", "INT'L UNION OF OP ENGINEERS 302"),
    ("BOATHOUSE 19", "BOATHOUSE 19"),
])
def test_person_name(raw, expected):
    assert C.person_name(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("Alliance for Automotive Innovation 1050 K St. NW, Suite 650", "Alliance for Automotive Innovation"),
    ("FairVote Washington PACPO Box 9100", "FairVote Washington PAC"),
    ("Acme Co 206-555-0142", "Acme Co"),
    ("515 Park Place LLC", "515 Park Place LLC"),         # the number is the name
    ("615 SOUTH 9TH ST LLC", "615 SOUTH 9TH ST LLC"),
    ("UA LOCAL 290 ST. OF WA SEGREGATED FUND", "UA LOCAL 290 ST. OF WA SEGREGATED FUND"),
    ("IBEW LOCAL 77 PAC", "IBEW LOCAL 77 PAC"),
])
def test_org_name_cuts_only_a_trailing_address(raw, expected):
    assert C.org_name(raw) == expected


def test_org_key_has_no_address_in_it():
    assert "PO BOX" not in C.org_key("FairVote Washington PACPO Box 9100")
    assert C.org_key("Alliance for Automotive Innovation 1050 K St. NW, Suite 650") == "ALLIANCE FOR AUTOMOTIVE INNOVATION"
    assert C.org_key("515 Park Place LLC") == "515 PARK PLACE"


@pytest.mark.parametrize("name", [
    "CANDACE G. INAGI", "CANDACE G INAGI", "INAGI, CANDACE", "INAGI CANDACE", "NICOLAS HANAUER", "JOAN L. MCBRIDE",
    "MARK ANDERSON", "BETH DOGLIO", "GARRETT HOLBROOK", "STEPHEN L PIPER", "KIM M. KRON", "DEBORAH HARPER",
    "BOEING WILLIAM E JR", "HOWARD KENNEY, M.D.", "LESTA L. ROGERS", "John Ford", "Mary Washington", "CHURCH JAMES R",
    "Mark and Sue Smith", "SMITH MARK & SUE",
])
def test_names_that_read_as_people(name):
    assert C.person_shaped(name)


@pytest.mark.parametrize("name", [
    "Microsoft", "Puget Sound Energy", "The Boeing Company", "HOUSE DEMOCRATIC CAMPAIGN COMMITTEE", "WINPOWER STRATEGIES",
    "ACME AND SONS", "515 Park Place LLC", "SURPLUS FUNDS BACK LLC", "BRIAN BLAKE SURPLUS ACCOUNT", "PHIL WATKINS PUBLIC AFFAIRS",
    "Bickford Ford Lincoln", "ELI LILLY", "Philip Morris", "FRED MEYER", "MORGAN STANLEY", "HAVE A HEART", "PARK N JET",
    "JOHNSON AND JOHNSON", "T W O", "WEYERHAEUSER", "BIOPAC", "PITNEY BOWES", "Michael Lacorte Dentistry", "", None,
])
def test_names_that_do_not_read_as_people(name):
    assert not C.person_shaped(name)


HOBBS = frozenset({"HOBBS"})


@pytest.mark.parametrize("name,own", [
    ("COMMITTEE TO ELECT STEVE HOBBS SURPLUS ACCOUNT", HOBBS),
    ("DEREK STANFORD SURPLUS FUND ACCOUNT", frozenset({"STANFORD"})),
    ("TRANSFER FROM SURPLUS FUNDS WITH WRITTEN PERMISSION", frozenset({"LOW"})),
    ("SURPLUS FUND", frozenset()),
    ("HOBBS FOR LT GOVERNOR", HOBBS),
    ("FRIENDS OF KAREN KEISER", frozenset({"KEISER"})),
    ("Hargrove for House", frozenset({"HARGROVE"})),
    ("VOTERS FOR KASTAMA 2008", frozenset({"KASTAMA"})),
    ("THE COMMITTEE TO RE-ELECT KATHY HAIGH", frozenset({"HAIGH"})),
    ("08 CITIZENS FOR CHRISTOPHER HURST CAMPAIGN", frozenset({"HURST"})),
])
def test_own_accounts(name, own):
    assert C.own_account(name, own)


@pytest.mark.parametrize("name,own", [
    # Real rows: this one reached Rep. Ortiz-Self, and it's a tribal gaming group.
    ("CAMPAIGN FOR TRIBAL SELF-RELIANCE BY WASHINGTON INDIAN GAMING ASSOCIATION", frozenset({"ORTIZ", "SELF"})),
    ("KING COUNTY DEMOCRATS COMMITTEE", frozenset({"KING"})),
    ("YOUNG REPUBLICANS COMMITTEE", frozenset({"YOUNG"})),
    ("Friends of Jane Roe", frozenset({"DOE"})),
    ("Jane Doe", frozenset({"DOE"})),                   # her own name, but no campaign
    ("The Doe Bakery", frozenset({"DOE"})),
    ("Bank of Doe", frozenset({"DOE"})),
    ("Committee to Elect Steve Hobbs", frozenset()),    # no name to compare it to
    ("SURPLUS LINE ASSOCIATION OF WASHINGTON", frozenset()),
    ("SURPLUS FUNDS BACK LLC", frozenset()),
])
def test_not_own_accounts(name, own):
    assert not C.own_account(name, own)


RULE = frozenset({"RULE"})


@pytest.mark.parametrize("code,category,name,own,expected", [
    ("Self", "Individual", "Alicia Rule", RULE, C.SELF_FUNDED),
    ("Self", "Individual", "RULE ALICIA J", RULE, C.SELF_FUNDED),
    ("Self", "Individual", "CAND PERSONAL FUNDS", RULE, C.SELF_FUNDED),
    ("Self", "Individual", "VANWERVEN LUANNE", frozenset({"VAN", "WERVEN"}), C.SELF_FUNDED),
    # The filer's code says 'Self'. The name says otherwise.
    ("Self", "Individual", "House Democratic Campaign Committee", RULE, "Other organizations"),
    ("Self", "Individual", "HDCC", RULE, "Other organizations"),
    ("Self", "Individual", "Chevron PAC", RULE, "Other organizations"),
    ("Self", "Individual", "Steve Ballmer", RULE, "Other sources"),
    ("Self", "Organization", "WEYERHAEUSER", RULE, "Other organizations"),
    ("Self", "Individual", "Rule Trucking LLC", RULE, "Other organizations"),
    # With no name to compare, the code stands.
    ("Self", "Individual", "House Democratic Campaign Committee", frozenset(), C.SELF_FUNDED),
    # Their own accounts, however they're coded.
    ("Self", "Individual", "Lauren Davis Surplus Funds Acct", frozenset({"DAVIS"}), C.OWN_ACCOUNTS),
    ("Political Action Committee", "Organization", "FRIENDS OF KAREN KEISER", frozenset({"KEISER"}), C.OWN_ACCOUNTS),
    ("Other", "Organization", "SURPLUS FUND", frozenset(), C.OWN_ACCOUNTS),
    # Everything else as before.
    ("Business", "Organization", "Rule Hardware", RULE, "Businesses"),
    ("Other", "Organization", "MISCELLANEOUS RECEIPTS", RULE, C.MISCELLANEOUS),
])
def test_source_type_with_the_officials_name(code, category, name, own, expected):
    assert C.source_type(code, category, name, own) == expected


@pytest.mark.parametrize("args,expected", [
    (("Self", "Individual", "House Democratic Campaign Committee", RULE), True),
    (("Self", "Individual", "HDCC", RULE), True),
    (("Self", "Organization", "WEYERHAEUSER", RULE), True),
    (("Self", "Individual", "Alicia Rule", RULE), False),
    (("Self", "Organization", "Alicia Rule", RULE), False),
    (("Self", "Individual", "Steve Ballmer", RULE), False),
    (("Political Action Committee", "Organization", "FRIENDS OF KAREN KEISER", frozenset({"KEISER"})), False),
    (("Other", "Organization", "DEREK STANFORD SURPLUS FUND ACCOUNT", frozenset()), False),
    (("Business", "Organization", "CANDACE G. INAGI", frozenset(), "ORGANIZER"), False),
    (("Business", "Organization", "GARRETT HOLBROOK", frozenset(), "CONSULTANT"), False),
    # An occupation on an organization's row is the job of whoever delivered the check.
    (("Business", "Organization", "MICROSOFT", frozenset(), "STATE GOVERNMENT AFFAIRS"), True),
    (("Business", "Organization", "AT&T SERVICES", frozenset(), "KING COUNTY LOBBYIST"), True),
    (("Unions", "Organization", "SEIU LOCAL 6 PAC", frozenset(), "PRESIDENT"), True),
    (("Business", "Organization", "CANDACE G. INAGI"), True),   # no occupation: the code stands, and rollups catch it
    (("Business", "Organization", "Acme"), True),
])
def test_is_org_with_name_and_occupation(args, expected):
    assert C.is_org(*args) is expected


@pytest.mark.parametrize("description,recipient,expected", [
    ("SURPLUS CONTRIBUTION TO HOUSE CAUCUS", "HOUSE DEMOCRATIC CAMPAIGN COMMITTEE", C.PARTY_TRANSFER),
    ("TRANSFER OF PRIMARY SURPLUS", "WASHINGTON SENATE DEMOCRATIC CAMPAIGN COMMITTEE", C.PARTY_TRANSFER),
    ("SURPLUS FUNDS", "HDCC", C.PARTY_TRANSFER),
    ("SURPLUS FUNDS TRANSFER", "KING COUNTY REPUBLICAN PARTY", C.PARTY_TRANSFER),
    ("TRANSFER TO SURPLUS", "ROBERT FERGUSON SURPLUS FUNDS", C.SURPLUS_TRANSFER),
    ("SURPLUS", "CENTRAL VALLEY BANK", C.SURPLUS_TRANSFER),
    ("SURPLUS", None, C.SURPLUS_TRANSFER),
])
def test_money_sent_to_a_party_is_not_a_surplus_transfer(description, recipient, expected):
    assert C.spend_category(None, description, recipient) == expected


def test_filers_own_surplus_code_stands_whoever_received_it():
    assert C.spend_category("Transfer to surplus funds account", "x", "HDCC") == C.SURPLUS_TRANSFER


@pytest.mark.parametrize("description,expected", [
    ("PHONE BANK SUPPORT", C.UNCATEGORIZED),
    ("PHONE BANKS", C.UNCATEGORIZED),
    ("NORTH COUNTY FOOD BANK SPONSORSHIP", "Charity"),
    ("MAIL PROCESSING FEES", C.UNCATEGORIZED),
    ("PAYCHECK PROCESSING FEE", C.UNCATEGORIZED),
    ("CHARITABLE CONTRIBUTION", "Charity"),
    ("OFFICE CABLE/INTERNET/PHONE", "Utilities, phone, and other overhead costs"),
    ("PRINTER CABLE", C.UNCATEGORIZED),
    ("CABLE TIES FOR SIGNS", "Printing campaign signs"),
    ("INTERNET BUY", "Digital advertising"),
    # The rules still do their jobs.
    ("BANK FEE", "Bank and payment processing charges"),
    ("CC PROCESSING FEE", "Bank and payment processing charges"),
    ("PROCESSING FEES", "Bank and payment processing charges"),
    ("ONLINE CONTRIBUTION PROCESSING FEE", "Bank and payment processing charges"),
    ("CABLE", "Broadcast/cable TV advertising"),
    ("SUBVENDOR: AL MEDIA-SEATTLE CABLE 10/13 - 10/19", "Broadcast/cable TV advertising"),
    ("CONTRIBUTION TO CAMPAIGN", "Monetary contributions to PAC or candidate"),
    ("OFFICE INTERNET", "Utilities, phone, and other overhead costs"),
    ("CELL PHONE", "Utilities, phone, and other overhead costs"),
])
def test_spending_rules_on_real_descriptions(description, expected):
    assert C.spend_category(None, description) == expected


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


def statement(person=None, **fields) -> dict:
    return {"statement": {"judge_or_law_enforcement": False, **fields}, "person": person or {"name": "Jane Roe"}}


def test_family_names_are_redacted_from_every_kept_string():
    d = sanitize(statement(
        spouse="Pat Secretname",
        dependents=["Kid Roe"],
        income={"data": [{"who": ["filer"], "name": "Pat Secretname Consulting", "why": "Fees to PAT"}]},
        accounts={"data": [{"who": ["spouse"], "name": "IRA - Secretname", "assets": [{"nature": "IRA (Pat)"}]}]},
        business_associations={"data": [{"who": ["spouse"], "legal_name": "SECRETNAME LLC", "description": "Kid helps"}]},
        creditors=[{"who": ["filer"], "name": "Pat Secretname", "payment_terms": "None"}],
        gifts=[{"who": ["filer"], "donor_name": "Kid Roe"}],
    ))
    text = json.dumps(d)
    assert "secretname" not in text.lower() and "Pat" not in text and "PAT" not in text and "Kid" not in text
    assert d["income"][0]["source"] == "[family member] Consulting"
    assert d["creditors"][0]["name"] == "[family member]"
    assert d["gifts"][0]["donor"] == "[family member]"


def test_the_officials_own_surname_and_other_words_stay():
    d = sanitize(statement(
        person={"name": "Janice Zahn"}, spouse="Bob Zahn", dependents=["Leah Zahn"],
        income={"data": [{"who": ["filer"], "name": "Zahn Consulting"}, {"who": ["filer"], "name": "Leah Zahn Tutoring"},
                         {"who": ["filer"], "name": "Bobcat Rentals"}, {"who": ["filer"], "name": "Boeing"}]},
    ))
    assert [i["source"] for i in d["income"]] == ["Zahn Consulting", "[family member] Tutoring", "Bobcat Rentals", "Boeing"]


def test_a_nickname_covers_the_formal_name():
    d = sanitize(statement(person={"name": "Sharon Wylie"}, spouse="Ted Gathe",
                           income={"data": [{"who": ["filer"], "name": "SSI for Theodore Gathe, husband"}]}))
    assert d["income"][0]["source"] == "SSI for [family member] [family member], husband"


def test_a_filled_in_placeholder_is_not_a_name():
    for spouse in ("None", "N/A", "Not applicable", "", None):
        d = sanitize(statement(spouse=spouse, dependents=[], income={"data": [{"who": ["filer"], "name": "None of these"}]}))
        assert d["income"][0]["source"] == "None of these"


def test_a_brokerage_is_not_a_family_member_because_of_a_shared_name():
    d = sanitize(statement(
        spouse="James Doe", dependents=["Jones Doe"],
        accounts={"data": [{"who": ["filer"], "name": "Raymond James", "assets": [{"nature": "IRA"}]},
                           {"who": ["filer"], "name": "Edward Jones", "assets": [{"nature": "IRA"}]}]},
    ))
    assert [a["name"] for a in d["accounts"]] == ["Raymond James", "Edward Jones"]


def test_home_address_pair_is_removed_from_any_kept_string():
    raw = statement(
        person={"name": "Javier Valdez", "address": "1234 NE 56th St"},
        real_estate=[{"who": ["filer"], "location": {"address": "9800 Roosevelt Way NE, # 23", "city": "Seattle"}}],
        creditors=[{"who": ["filer"], "name": "9800 Roosevelt Condominiums", "payment_terms": "15 years - Special Assessment"},
                   {"who": ["filer"], "name": "1234 NE 56TH HOA"}, {"who": ["filer"], "name": "1234 56th Group"},
                   {"who": ["filer"], "name": "9800 Lake City Partners"}, {"who": ["filer"], "name": "Roosevelt Savings"}],
    )
    d = sanitize(raw)
    assert "Roosevelt Condominiums" not in json.dumps(d)
    assert [c["name"] for c in d["creditors"]] == [
        "[address removed]", "[address removed] HOA", "[address removed] Group", "9800 Lake City Partners", "Roosevelt Savings"]
    assert d["creditors"][0]["terms"] == "15 years - Special Assessment"


def test_a_statement_without_any_address_is_fine():
    assert sanitize(statement(real_estate=[{"who": ["filer"], "location": "not a dict"}]))["real_estate"]


@pytest.mark.parametrize("nature", [
    "Regular Savings - Son", "Personal Savings - Daughter", "529 Plan - Son", "Checking Account (Son)",
    "529 College Savings Plan (For Benefit of Daughter)", "Vanguard UTMA (Held in Trust for Son Samuel Frockt)",
    "UTMA / Preston Hoff (grandson) / Mutual Funds", "UGMA account", "529 ACCOUNT FBO M. HECK", "Savings for my granddaughter",
    "529 Plan - James Schwarmann", "Aspen - 529", "Guaranteed Education Tuition - Washington state - for Leah Zahn",
    "Guaranteed Education Tuition Account for Henry Pollet",
])
def test_holdings_marked_as_a_childs_are_dropped(nature):
    raw = statement(accounts={"data": [{"who": ["filer"], "name": "USAA",
                                        "assets": [{"nature": nature, "value": "30000-59999"}, {"nature": "IRA", "value": "1"}]}]})
    assert sanitize(raw)["accounts"][0]["assets"] == [{"nature": "IRA", "value": "1", "income": None}]


@pytest.mark.parametrize("nature", [
    "529 Plan", "529 Plan - Cash", "529 Plan - Stocks and bonds", "College Savings Plan", "Fidelity 529", "College savings/529",
    "Seattle Children's Hospital Pension and Retirement Plan", "Johnson & Sons Stock", "Wilson and Sons",
    "( Joint Filer/Spouse and Joint Accounts of Filer w/Son and w/Daugter; Vanguard 529 Account for Son & Daughter)",
])
def test_holdings_that_only_look_like_a_childs_stay(nature):
    raw = statement(accounts={"data": [{"who": ["filer"], "name": "USAA", "assets": [{"nature": nature}]}]})
    assert sanitize(raw)["accounts"][0]["assets"][0]["nature"] == nature


def test_account_is_dropped_when_it_or_everything_in_it_is_a_childs():
    raw = statement(accounts={"data": [
        {"who": ["filer"], "name": "529 ACCOUNT FBO R. HECK", "assets": [{"nature": "Mutual Funds"}]},
        {"who": ["filer"], "name": "USAA", "assets": [{"nature": "Savings - Son"}]},
        {"who": ["filer"], "name": "BECU", "assets": []},
    ]})
    assert [a["name"] for a in sanitize(raw)["accounts"]] == ["BECU"]


def test_holding_with_a_dependents_name_is_dropped_even_with_no_relation_word():
    raw = statement(dependents=["Gaelia Schwarmann"], accounts={"data": [
        {"who": ["filer"], "name": "USAA", "assets": [{"nature": "Savings - Gaelia"}, {"nature": "Savings"}]}]})
    assert [a["nature"] for a in sanitize(raw)["accounts"][0]["assets"]] == ["Savings"]


def test_a_former_spouse_named_as_a_source_of_support_is_removed():
    raw = statement(income={"data": [{"who": ["filer"], "name": "Edward Rule", "why": "Spousal support"},
                                     {"who": ["filer"], "name": "Tim Farrell", "why": "Child Support"},
                                     {"who": ["filer"], "name": "DSHS", "why": "Child support"}]})
    assert [i["source"] for i in sanitize(raw)["income"]] == ["[family member]", "[family member]", "DSHS"]


@pytest.mark.parametrize("value", [
    "My abuser was ordered to reimburse a portion of my attorney's fees",
    "Securing a domestic violence protection order",
    "Legal costs for a restraining order",
    "Paid by my stalker after a judgment",
    "Settlement after a sexual assault",
])
def test_personal_details_are_removed(value):
    d = sanitize(statement(income={"data": [{"who": ["filer"], "name": "Legal judgment", "why": value, "income": "0-29999"}]}))
    assert d["income"] == [{"who": ["Official"], "source": "Legal judgment", "for": "[personal detail removed]",
                            "amount": "0-29999", "category": None, "city": None, "state": None}]


@pytest.mark.parametrize("value", [
    "Domestic Violence Prevention Services", "Domestic Violence support", "Rebuilding Hope the Sexual Assault Center for Pierce County",
    "Nonprofit organization restraining government overreach", "Substance Abuse Treatment Center", "Abuse Prevention Manager",
])
def test_groups_doing_this_work_keep_their_names(value):
    d = sanitize(statement(business_associations={"data": [{"who": ["filer"], "legal_name": "X", "description": value}]}))
    assert d["businesses"][0]["what"] == value


def test_sanitize_tolerates_junk():
    assert sanitize({})["income"] == []
    assert sanitize({"person": "not a dict", "statement": {"spouse": 7, "dependents": {"x": 1}}})["income"] == []


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
