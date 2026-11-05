"""Pure helpers that turn raw PDC values into what we store and show."""
from __future__ import annotations

import html
import re
from datetime import date
from decimal import Decimal, InvalidOperation


def text(value) -> str | None:
    """Trimmed text with HTML entities decoded (the source has '&AMP;'), or None when empty."""
    if value is None:
        return None
    s = html.unescape(html.unescape(str(value)))
    s = re.sub(r"&amp;", "&", s, flags=re.I)
    s = " ".join(s.split())
    return s or None


# Filers sometimes type a street address into the wrong box: a city, an employer, a description.
# We promise not to publish street addresses, so these rules apply to every free-text field.
_STREET = re.compile(
    r"\b[NSEW]?\.?\s?\d{1,6}\s+(?:[NSEW]\.?\s+)?(?:[\w.'-]+\s+){0,4}?"
    r"(?:st|street|ave|avenue|blvd|boulevard|rd|road|dr|drive|way|ln|lane|ct|court|pl|place|hwy|highway|"
    r"cir|circle|ter|terrace|pkwy|parkway|loop)\b\.?"
    r"(?:\s+[NSEW]{1,2}\b\.?)?(?:[,\s]+(?:suite|ste|unit|apt|#)\s*[\w-]+)?",
    re.I,
)
_PO_BOX = re.compile(r"\bp\.?\s?o\.?\s*box\s*\w*", re.I)
ADDRESS_REMOVED = "[address removed]"


def has_street(value) -> bool:
    return bool(value and (_STREET.search(str(value)) or _PO_BOX.search(str(value))))


def free_text(value) -> str | None:
    """Text with any street address or PO box inside it replaced by a marker."""
    s = text(value)
    if s is None:
        return None
    return _PO_BOX.sub(ADDRESS_REMOVED, _STREET.sub(ADDRESS_REMOVED, s))


def place(value) -> str | None:
    """A city name. Anything with a digit in it isn't one, so it isn't shown."""
    s = text(value)
    return None if s is None or re.search(r"\d", s) or has_street(s) else s


def zip_code(value) -> str | None:
    s = text(value)
    return s if s and re.fullmatch(r"\d{5}(-?\d{4})?", s) else None


def employer(value) -> str | None:
    """An employer's name, or nothing if what's there is a street address."""
    s = text(value)
    return None if s is None or has_street(s) else s


def money(value) -> Decimal:
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal("0.00")


def money_or_none(value) -> Decimal | None:
    return None if value in (None, "") else money(value)


def day(value) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except (ValueError, TypeError):
        return None


def integer(value) -> int | None:
    try:
        return int(str(value).strip())
    except (ValueError, TypeError):
        return None


def link(value) -> str | None:
    """The source wraps some URLs in {'url': ...}. Only web addresses get through."""
    if isinstance(value, dict):
        value = value.get("url")
    return value if isinstance(value, str) and value.startswith(("https://", "http://")) else None


def name_key(name) -> str:
    """Normalized form used to group spellings that differ only in case and punctuation.
    Never fuzzy: 'AMAZON' and 'AMAZON.COM SERVICES INC' stay separate, as filed."""
    s = (text(name) or "").upper().replace("&", " AND ").replace("'", "").replace("’", "")
    return " ".join(re.sub(r"[^A-Z0-9]+", " ", s).split())


# Registered committees and the shorthand campaigns report them under. Every target was checked
# against the PDC's filer list; nothing here is a guess. Keys are already through org_key's rules.
ORG_ALIASES = {
    "HDCC": "HOUSE DEMOCRATIC CAMPAIGN COMMITTEE",
    "HOUSE DEMO CAMP COMM": "HOUSE DEMOCRATIC CAMPAIGN COMMITTEE",
    "HROC": "HOUSE REPUBLICAN ORGANIZATIONAL COMMITTEE",
    "HOUSE REPUB ORG COMM": "HOUSE REPUBLICAN ORGANIZATIONAL COMMITTEE",
    "SRCC": "SENATE REPUBLICAN CAMPAIGN COMMITTEE",
    "SENATE REPUB CAMP COMM": "SENATE REPUBLICAN CAMPAIGN COMMITTEE",
    "WSDC": "WASHINGTON SENATE DEMOCRATIC CAMPAIGN",
    "WASHINGTON SENATE DEMO CAMP": "WASHINGTON SENATE DEMOCRATIC CAMPAIGN",
    "WASHINGTON STATE DEMOCRATS": "WASHINGTON STATE DEMOCRATIC COMMITTEE",
    "WASHINGTON STATE DEMOCRATIC CENTRAL COMMITTEE": "WASHINGTON STATE DEMOCRATIC COMMITTEE",
    "WASHINGTON STATE DEMO CENT COMM": "WASHINGTON STATE DEMOCRATIC COMMITTEE",
    "WASHINGTON STATE REPUB PARTY": "WASHINGTON STATE REPUBLICAN PARTY",
}
# The full name to show for those committees, instead of whichever shorthand was filed most often.
ORG_DISPLAY_NAMES = set(ORG_ALIASES.values())
_ORG_WORDS = {"PAC": "POLITICAL ACTION COMMITTEE", "ASSN": "ASSOCIATION", "ASSOC": "ASSOCIATION",
              "CORP": "CORPORATION"}
_ORG_SUFFIXES = ("INC", "LLC", "EXEMPT", "NONEXEMPT", "NON")


def org_key(name) -> str:
    """Like name_key, plus a few fixed rules for how organizations get abbreviated, so that
    'WA St Auto Dealers PAC' and 'Washington State Auto Dealers Political Action Committee' are one
    entry. Deterministic, never approximate; different words stay different organizations."""
    s = re.sub(r"\s*\([^)]*\)\s*$", "", text(name) or "")     # a trailing '(HDCC)'
    words = name_key(s).split()
    if words[:1] == ["THE"] and len(words) > 1:
        words = words[1:]
    if words[:1] in (["WA"], ["WASH"]):
        words[0] = "WASHINGTON"
    if words[:2] == ["WASHINGTON", "ST"]:
        words[1] = "STATE"
    while len(words) > 1 and words[-1] in _ORG_SUFFIXES:
        words.pop()
    key = " ".join(_ORG_WORDS.get(w, w) for w in words)
    return ORG_ALIASES.get(key, key)


def key_to_slug(key: str) -> str:
    return key.replace(" ", "-")


def slug_to_key(slug: str) -> str:
    return slug.replace("-", " ").upper()


# ---- Who gave ----------------------------------------------------------------------------

SMALL_CONTRIBUTIONS = "Small contributions (not itemized)"
MISCELLANEOUS = "Miscellaneous receipts"
LOW_COST = "Low-cost fundraisers"
SELF_FUNDED = "The candidate's own money"
SOURCE_LABELS = {
    "Individual": "Individuals",
    "Business": "Businesses",
    "Political Action Committee": "Political action committees",
    "Union": "Unions",
    "Party": "Political parties",
    "Caucus": "Legislative caucuses",
    "Self": SELF_FUNDED,
    "Low-cost Fundraiser": LOW_COST,
}
# Not donors at all: lines where a report lumps money together. Miscellaneous receipts are often the
# candidate's own money moving between campaigns ("XFER FROM SURPLUS FOR 2018 CAMPAIGN"), plus
# interest and refunds, so they must not look like gifts from people or organizations.
_SMALL_NAMES = {"SMALL CONTRIBUTIONS", "SMALL CONTRIBUTION", "ANONYMOUS", "ANONYMOUS CONTRIBUTIONS"}
_MISC_NAMES = {"MISCELLANEOUS RECEIPTS", "MISCELLANEOUS RECEIPT", "MISC RECEIPTS", "MISC RECEIPT"}
_LOW_COST_NAMES = {"PROCEEDS FROM LOW COST FUNDRAISER", "LOW COST FUNDRAISER"}
_POOLED_NAMES = _SMALL_NAMES | _MISC_NAMES | _LOW_COST_NAMES


def source_type(code, category, name) -> str:
    key = name_key(name)
    if key in _SMALL_NAMES:
        return SMALL_CONTRIBUTIONS
    if key in _MISC_NAMES:
        return MISCELLANEOUS
    if key in _LOW_COST_NAMES:
        return LOW_COST
    label = SOURCE_LABELS.get((code or "").strip())
    if label:
        return label
    return "Other organizations" if (category or "").strip() == "Organization" else "Other sources"


def is_org(code, category, name=None) -> bool:
    if name_key(name) in _POOLED_NAMES:
        return False
    return (category or "").strip() == "Organization" or (code or "").strip() in (
        "Business", "Political Action Committee", "Union", "Party", "Caucus")


# ---- Where it went -----------------------------------------------------------------------

UNCATEGORIZED = "Not categorized by the filer"
# Older reports have no category code, only free text. These rules are deliberately narrow:
# anything they don't clearly recognize stays "not categorized" instead of being guessed at.
_SPEND_RULES = (
    (r"\b(tv|television|cable|broadcast)\b", "Broadcast/cable TV advertising"),
    (r"\bradio\b", "Radio advertising"),
    (r"\b(newspaper|periodical)\b", "Newspaper/periodical advertising"),
    (r"\b(digital|online|internet|facebook|google)\b.*\b(ad|ads|advertising)\b", "Digital advertising"),
    (r"\b(ad|ads|advertising|advertisement)\b", "Other advertising"),
    (r"\bpostage|stamps?\b|\bmail permit|\bpo box\b|bulk mail", "Postage costs, mail permits, purchase of stamps"),
    (r"\bprinting\b.*\bsigns?\b|\byard signs?\b|\bsigns?\b", "Printing campaign signs"),
    (r"\bprint|\bmailers?\b|\bmailing\b|\bliterature\b|\bbrochures?\b|\bflyers?\b|\bfliers?\b|\bdoorbelters?\b",
     "Printing literature, fliers, postcards, etc."),
    (r"\bconsult|\bmanagement\b|\bcampaign manager\b|\bstrategy\b", "Management and consulting services"),
    (r"\bsalar|\bwages?\b|\bpayroll\b|\bstaff\b", "Wages, salaries, benefits, payroll taxes"),
    (r"\bpoll|\bsurvey|\bresearch\b", "Surveys, polling, research costs"),
    (r"\bfundrais|\bevent\b|\bcatering\b", "Fundraising events and related costs"),
    (r"\bsurplus\b", "Transfer to surplus funds account"),
    (r"\bfiling fee\b", "Filing fees"),
    (r"\bbank\b|\bprocessing fee|\bmerchant\b|\bcredit card fee", "Bank and payment processing charges"),
    (r"\bmileage\b", "Mileage reimbursement"),
    (r"\btravel\b|\bmeals?\b|\bhotel\b|\blodging\b", "Travel, accommodations, meals"),
    (r"\baccounting\b|\blegal\b|\battorney\b|\bcompliance\b|\bbookkeep|\btreasury\b|\btreasurer\b",
     "Accounting, legal, regulatory compliance, etc."),
    (r"\bloan (re)?pay", "Loan payments"),
    (r"\bcredit card\b", "Bank and payment processing charges"),
    (r"\boffice supplies\b|\bsupplies\b", "Office supplies, furniture, staff food & beverages, etc."),
    (r"\bgas\b|\bfuel\b", "Travel, accommodations, meals"),
    (r"\bwebsite\b|\bweb hosting\b|\bsoftware\b|\bdata services\b", "Computers, printers, software, phones, etc."),
    (r"\bcell\b|\bphone\b|\btelephone\b|\binternet\b", "Utilities, phone, and other overhead costs"),
    (r"\brent\b|\blease\b", "Rent, lease, mortgage, PO box rental"),
    (r"\brefund", "Contribution Refunds"),
    (r"\bcontribution\b", "Monetary contributions to PAC or candidate"),
    (r"\bin[- ]?kind\b", "Inkind contribution"),
)
_SPEND_RES = [(re.compile(p, re.I), label) for p, label in _SPEND_RULES]


def spend_category(code, description) -> str:
    c = text(code)
    if c:
        return c
    d = text(description)
    if d:
        for rx, label in _SPEND_RES:
            if rx.search(d):
                return label
    return UNCATEGORIZED
