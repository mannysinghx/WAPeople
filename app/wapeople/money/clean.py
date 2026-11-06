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
# We promise not to publish street addresses, phone numbers or email addresses, so these rules apply
# to every free-text field.
_SUFFIX = (r"st|street|ave|avenue|blvd|boulevard|rd|road|dr|drive|way|ln|lane|ct|court|pl|place|hwy|highway|"
           r"cir|circle|ter|terrace|pkwy|parkway|loop")
_STREET = re.compile(
    r"\b[NSEW]?\.?\s?\d{1,6}\s+(?:[NSEW]\.?\s+)?(?:[\w.'-]+\s+){0,4}?"
    rf"(?:{_SUFFIX})\b\.?"
    r"(?:\s+[NSEW]{1,2}\b\.?)?(?:[,\s]+(?:suite|ste|unit|apt|#)\s*[\w-]+)?",
    re.I,
)
_ORDINAL = r"\d{1,3}(?:st|nd|rd|th)\b"
# '1500 NW 119th', '711 20TH E.', and a bare '922 5th' unless it's a count or a year ('500 1st class').
_NUMBERED_STREET = re.compile(
    rf"\b\d{{1,6}}\s+[NSEW]{{1,2}}\.?\s+{_ORDINAL}\.?"
    rf"|\b\d{{1,6}}\s+{_ORDINAL}\s+[NSEW]{{1,2}}\b\.?"
    rf"|\b(?!(?:19|20)\d\d\b)\d{{3,6}}\s+{_ORDINAL}"
    r"(?!\s+(?:class|quarter|qtr|party|floor|place|annual|anniv|ld|leg|dist))",
    re.I,
)
# 'QFC 1401 Broadway Seattle, WA 98122': no suffix to recognize, but the state and ZIP give it away.
# Not a date's year or part of a larger number, and it starts at the last number before the state.
_CITY_STATE_ZIP = re.compile(r"(?<![\d/,-])\b\d{1,6}\s+(?:(?!\d)[\w.'#-]+,?\s+){1,6}?(?=[A-Z]{2}\s+\d{5}\b)")
# '[address removed] West University Pl,': what's left when an address has two street words.
_AFTER_MARKER = re.compile(rf"(\[address removed\])(?:\s+[\w.'-]+){{1,3}}?\s+(?:{_SUFFIX})\b\.?(?=\s*(?:,|$))", re.I)
_DOUBLE_MARKER = re.compile(r"(\[address removed\])[\s,#-]*(?:[\w.-]+\s*){0,2}\[address removed\]")
# Also when glued to the word before it ('COMPANYP.O. BOX 1393'), which needs a number to be sure.
_PO_BOX = re.compile(
    r"(?:\bp\.?\s?o\.?\s*bo?x(?:\s*#?\s*\w*\d\w*)?"
    r"|\bpost\s+office\s+box\s*#?\s*\d+"
    r"|(?<=[a-z])p\.?\s?o\.?\s*box\s*#?\s*\d+"
    r"|\bpmb\s*#?\s*\d+)",
    re.I,
)
_EMAIL = re.compile(r"[\w.+'-]+@[\w-]+(?:\.[\w-]+)*\.[A-Za-z]{2,}")
# Ten bare digits are usually an invoice or account number, so those need a local area code.
_PHONE = re.compile(
    r"(?<![\d#.-])(?:\+?1[-. ])?"
    r"(?:\([2-9]\d{2}\)\s?[2-9]\d{2}[-. ]?\d{4}"
    r"|[2-9]\d{2}[-. ][2-9]\d{2}[-. ]\d{4}"
    r"|(?:206|253|360|425|509|564|208|503|541|971)[2-9]\d{6})(?![\d-])"
)
_ZIP4 = re.compile(r"\b(\d{5})-\d{4}\b")
ADDRESS_REMOVED = "[address removed]"
PHONE_REMOVED = "[phone removed]"
EMAIL_REMOVED = "[email removed]"


def has_street(value) -> bool:
    s = str(value) if value else ""
    return bool(s and any(rx.search(s) for rx in (_STREET, _PO_BOX, _NUMBERED_STREET, _CITY_STATE_ZIP)))


def has_contact(value) -> bool:
    s = str(value) if value else ""
    return bool(s and (_EMAIL.search(s) or _PHONE.search(s)))


def free_text(value) -> str | None:
    """Text with any street address, PO box, phone number or email address inside it replaced by
    a marker, and any nine-digit ZIP cut to five digits."""
    s = text(value)
    if s is None:
        return None
    s = _PHONE.sub(PHONE_REMOVED, _EMAIL.sub(EMAIL_REMOVED, s))
    s = _CITY_STATE_ZIP.sub(ADDRESS_REMOVED + ", ", s)
    s = _NUMBERED_STREET.sub(ADDRESS_REMOVED, _PO_BOX.sub(ADDRESS_REMOVED, _STREET.sub(ADDRESS_REMOVED, s)))
    s = _DOUBLE_MARKER.sub(r"\1", _AFTER_MARKER.sub(r"\1", s))
    return _ZIP4.sub(r"\1", s)


def place(value) -> str | None:
    """A city name. Anything with a digit in it isn't one, so it isn't shown."""
    s = text(value)
    return None if s is None or re.search(r"\d", s) or has_street(s) or has_contact(s) else s


def zip_code(value) -> str | None:
    """Five digits only: a nine-digit ZIP can point to a single building."""
    s = text(value)
    return s[:5] if s and re.fullmatch(r"\d{5}(-?\d{4})?", s) else None


def employer(value) -> str | None:
    """An employer's name, or nothing if what's there is a street address or contact detail."""
    s = text(value)
    return None if s is None or has_street(s) or has_contact(s) else s


# House numbers filers' exports leave after a donor's name ('KUPER GLENN 1542'), or a directional and
# numbered street ('LYON, DOROTHY NE 18TH'). A bare '3RD' stays: it's a generation ('WOLF RUSSELL R 3RD').
_TRAILING_NUMBER = re.compile(r"[\s,]*\b\d{1,6}(?:\.\d+)?$|(?<=[A-Za-z])\d{1,6}$")
_TRAILING_NUMBERED_STREET = re.compile(rf"[\s,]+[NSEW]{{1,2}}\.?\s+{_ORDINAL}\.?$", re.I)
_MARKERS = re.compile(rf"\s*(?:{re.escape(ADDRESS_REMOVED)}|{re.escape(PHONE_REMOVED)}|{re.escape(EMAIL_REMOVED)})\s*")


def person_name(value) -> str | None:
    """A person's name, with any address or contact detail that was typed into it taken out.
    Sync calls this for everything not coded as an organization, and that includes 'IBEW LOCAL 76',
    so a trailing number only goes when what's left reads as a name."""
    s = free_text(value)
    if s is None:
        return None
    s = _MARKERS.sub(" ", s).strip(" ,-")
    while True:
        cut = _TRAILING_NUMBERED_STREET.sub("", s)
        cut = _TRAILING_NUMBER.sub("", cut).rstrip(" ,-")
        words = name_key(cut).split()
        if cut == s or len(words) < 2 or _has_org_word(words):
            return s or None
        s = cut


def org_name(value) -> str | None:
    """An organization's name without an address or contact detail typed after it: 'Alliance for
    Automotive Innovation 1050 K St. NW, Suite 650'. A name that starts with its number is the name
    itself ('515 Park Place LLC'), and so is one with more words after the number."""
    s = text(value)
    if s is None:
        return None
    if has_contact(s):
        s = _EMAIL.sub("", _PHONE.sub("", s)).strip(" ,-")
    for rx in (_PO_BOX, _STREET, _NUMBERED_STREET):
        m = rx.search(s)
        if m and m.start() > 0 and not re.search(r"\w", s[m.end():]):
            return s[:m.start()].rstrip(" ,-") or None
    return s or None


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
    s = re.sub(r"\s*\([^)]*\)\s*$", "", org_name(name) or "")     # a trailing '(HDCC)'
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
OWN_ACCOUNTS = "Transfers from the candidate's own accounts"
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

# A filer's code doesn't settle whether a name is a person: people get coded 'Business', and thousands
# of real businesses have two or three plain words. So a name reads as a person only when it has no
# organization word and it also has a positive sign of one: a given name at either end, 'LAST, FIRST'
# punctuation, or a middle initial. Checked against all 9,855 two-to-four word names in money_orgs.
_ORGANIZATION_WORDS = frozenset("""
    ACADEMY ACCOUNT ACCOUNTING ACQUISITION ACTION ADVERTISING ADVISORS ADVOCATES AFFAIRS AFFORDABLE AGENCY AGENT AIR
    AIRFIELD AIRLINE AIRLINES AIRWAYS ALLIANCE ALUMINUM AMERICA AMERICAN ANONYMOUS APARTMENTS ARCADE ARCHITECT ARCHITECTS
    ARCHITECTURE ARTS ASPHALT ASSN ASSOC ASSOCIATES ASSOCIATION ATTORNEY ATTORNEYS AUCTION AUCTIONEER AUTHORITY AUTO
    AUTOMOTIVE AVIATION BAIL BAKERY BAND BANK BAR BEVERAGE BEVERAGES BOARD BODY BONDS BOOKS BREWERY BREWING BROKERAGE BROS
    BROTHERS BUICK BUILDER BUILDERS BUILDING BUSINESS CAB CAFE CAMPAIGN CAPITAL CARE CARPET CASINO CATERING CAUCUS CELLARS
    CENTER CENTERS CHAMBER CHEVROLET CHIROPRACTIC CHRYSLER CINEMAS CITIZENS CITY CLINIC CLUB CMTE CO COALITION COFFEE
    COLLECTIONS COLLECTORS COLLEGE COM COMM COMMERCE COMMERCIAL COMMITTEE COMMUNICATION COMMUNICATIONS COMMUNITIES COMMUNITY
    COMPANIES COMPANY CONCEPTS CONCRETE CONGRESS CONNECTION CONNECTIONS CONSERVATION CONSTRUCTION CONSULTANTS CONSULTING
    CONTRACTOR CONTRACTORS CONTROL COOP COOPERATIVE COPE CORP CORPORATION COUNCIL COUNSEL COUNTY CREATIVE CREDIT CROSSING
    DAIRY DBA DEALERSHIPS DEMOCRAT DEMOCRATIC DEMOCRATS DENTAL DENTIST DENTISTRY DENTISTS DEPOT DESIGN DESIGNS DEVELOPERS
    DEVELOPMENT DIGITAL DINNER DISPOSAL DISTRIBUTING DISTRIBUTORS DISTRICT DIVISION DOCTORS DRILLING DRUG EDUCATION ELECT
    ELECTRIC ELECTRICAL EMPLOYEES EMPLOYERS ENERGY ENGINEERING ENGINEERS ENTERPRISE ENTERPRISES ENTERTAINMENT ENVIRONMENTAL
    EQUIPMENT ESTATE ESTATES EVENT EXCAVATING EXCAVATION EXCAVATORS EXECUTIVE EXPRESS FACTORY FAMILY FARM FARMERS FARMS
    FEDERATION FIGHTERS FINANCIAL FIRE FIREFIGHTERS FIREWORKS FIRM FISHERIES FITNESS FLOORING FOODS FOR FORUM FOUNDATION
    FOUNDATIONS FREEDOM FREIGHT FRIENDS FRUIT FUND FUNDRAISING FUNDS FURNITURE GALLERY GAMING GARAGE GARDEN GARDENS GAS
    GENERAL GMC GOLF GOP GOVERNMENT GOVT GRAIN GRILL GROCERY GROUP GROWERS GRP GUILD HARDWARE HARVESTERS HEALTH HEALTHCARE
    HEART HOLDING HOLDINGS HOME HOMES HONDA HOSE HOSPITAL HOSPITALITY HOTEL HOUSING IBEW IN INC INCORPORATED INDIANS
    INDUSTRIAL INDUSTRIES INITIATIVE INN INSTITUTE INSURANCE INTEREST INTERIORS INTERNATIONAL INTL INVESTMENT INVESTMENTS
    IRON JEEP JEWELERS KROGER LABOR LABORATORIES LABS LAND LANDSCAPE LANDSCAPING LAWYER LAWYERS LEADERSHIP LEAGUE LEASING
    LEGAL LIBERTY LICENSE LICENSING LINES LIQUOR LIVING LLC LLLP LLP LOBBY LOCAL LOGGING LP LTD LUMBER MAINTENANCE
    MANAGEMENT MANOR MANUFACTURING MARINA MARINE MARKET MARKETING MART MATERIALS MAZDA MECHANICAL MEDIA MEDICAL MEDICINE
    MEMORIAL MEN MERCHANTS METALS MGMT MHC MHP MILL MINI MISC MISCELLANEOUS MOBILE MORTGAGE MOTOR MOTORS MOTORSPORTS MOVERS
    MUTUAL NATION NATIONAL NETWORK NISSAN NORTHWEST NURSERY NURSES OF OFFICE OFFICES OIL OPERATIONS OPTICAL OPTOMETRIC
    ORCHARD ORCHARDS ORTHODONTICS PAC PACIFIC PACKARD PACKING PAINTING PALLET PARTNER PARTNERS PARTNERSHIP PARTY PBC PC
    PEOPLE PETROLEUM PETS PHARMA PHARMACEUTICALS PHARMACY PHOTOGRAPHER PHOTOGRAPHY PHYSICAL PHYSICIANS PILOTS PIZZA PLAN
    PLLC PLUMBING POLICY POLITICAL PORT PRINT PRINTING PROCEEDS PRODUCTION PRODUCTIONS PRODUCTS PROFESSIONAL PROGRESS
    PROJECT PROPERTIES PROPERTY PS PUB PUBLIC PUBLISHING QUARRY RAFFLE RAILROAD RANCH RANCHES REAL REALTORS REALTY RECYCLING
    REFUND REGIONAL RELATIONS RENEWABLES RENTAL RENTALS REPUBLICAN REPUBLICANS RESOURCE RESOURCES RESTAURANT RESTAURANTS
    RETAIL RIGHTS ROOFING SALE SALES SALON SALOON SCHOOL SCHOOLS SCIENCE SCIENCES SCION SEAFOOD SEATTLE SECURITY SEIU
    SEMINARS SERVICE SERVICES SHELL SHELLFISH SHERIFFS SHIPYARD SHIPYARDS SHOP SOCIETIES SOCIETY SOFTWARE SOLAR SOLUTIONS
    SPA SPC SPECIALISTS SPOKANE SPORTS STATE STATION STORAGE STORE STORES STRATEGIES STUDIO STUDIOS SUBARU SUPPLY SURGEONS
    SURPLUS SVCES SYSTEMS TACOMA TAVERN TAXI TEACHERS TEAM TECHNOLOGIES TECHNOLOGY TERMINAL TERMINALS THE THEATER THEATRE
    THERAPY TIMBER TIRE TITLE TOWING TOWN TOYOTA TRADES TRANSFER TRANSIT TRANSPORT TRANSPORTATION TRIBAL TRIBE TRIBES
    TRUCKING TRUCKS TRUST UNION UNITED UNIVERSITY UNKNOWN USA VALLEY VENTURES VETERANS VETERINARY VIDEOGRAPHY VIEW VILLAGE
    VINEYARD VINEYARDS VISION VOLKSWAGEN VOTERS VOTES WASTE WATER WELLNESS WINE WINERY WINES WOMEN WOMENS WORKERS WORKS YOGA
""".split())
# Also family names, so they only count as organization words in a longer name: 'Bickford Ford Lincoln'
# yes, 'John Ford' no.
_ALSO_FAMILY_NAMES = frozenset("""
    ART BLUE CHURCH FLOWERS FORD FOREST HOUSE JUSTICE LAW LINE LODGE MILLS PARK POWER PRESS RIDGE STEEL STREET WASHINGTON
""".split())
_GIVEN_NAMES = frozenset("""
    AARON ABBY ABIGAIL ABRAHAM ADAM ADELE ADRIAN ADRIENNE AGNES AHMED AIMEE AL ALAN ALBERT ALEC ALEJANDRO ALEX ALEXA
    ALEXANDER ALEXANDRA ALEXIS ALFRED ALI ALICE ALICIA ALISON ALISSA ALLAN ALLEN ALLISON ALMA ALTHEA ALVIN ALYSSA AMANDA
    AMBER AMELIA AMIR AMY ANA ANDREA ANDREW ANDY ANGELA ANGIE ANIL ANITA ANN ANNA ANNE ANNETTE ANNIE ANTHONY ANTOINETTE
    ANTONIO APRIL ARLENE ARNOLD ART ARTHUR ASHLEY AUDREY AUSTIN AVA BARB BARBARA BARRY BART BEATRICE BECKY BEN BENJAMIN
    BERNARD BERNICE BERT BETH BETSY BETTE BETTY BEVERLY BILL BILLIE BILLY BLAIR BLAKE BOB BOBBI BOBBIE BOBBY BONNIE BRAD
    BRADLEY BRANDI BRANDON BRANDY BRENDA BRENDAN BRENT BRET BRETT BRIAN BRIANA BRIDGET BRITTANY BROOKE BRUCE BRYAN BRYCE
    BYRON CAITLIN CALVIN CAMERON CAMILLE CANDACE CANDICE CANDY CARA CAREN CAREY CARL CARLA CARLOS CARMEN CAROL CAROLE
    CAROLINE CAROLYN CARRIE CARSON CARY CASEY CASSANDRA CATHERINE CATHY CECILIA CELESTE CELIA CHAD CHARLENE CHARLES CHARLIE
    CHARLOTTE CHAS CHELSEA CHERI CHERYL CHESTER CHRIS CHRISTA CHRISTIE CHRISTINA CHRISTINE CHRISTOPHER CHRISTY CHUCK CINDY
    CLAIRE CLARA CLARE CLARENCE CLARK CLAUDE CLAUDIA CLAYTON CLIFF CLIFFORD CLINT CLINTON CLYDE CODY COLEEN COLIN COLLEEN
    CONNIE CONRAD CONSTANCE COREY CORI CORY COURTNEY CRAIG CRISTINA CRYSTAL CURT CURTIS CYNTHIA DALE DAMON DAN DANA DANIEL
    DANIELLE DANNY DARCY DARIN DARLENE DARREL DARRELL DARREN DARRYL DARYL DAVE DAVID DAWN DEAN DEANNA DEB DEBBIE DEBORAH
    DEBRA DEE DELORES DENISE DENNIS DENNY DEREK DEVIN DIANA DIANE DIANNE DICK DOLORES DOMINIC DON DONALD DONNA DOREEN DORIS
    DOROTHY DOUG DOUGLAS DREW DUANE DUSTIN DWAYNE DWIGHT DYLAN EARL ED EDDIE EDITH EDMUND EDNA EDWARD EDWIN EILEEN ELAINE
    ELEANOR ELENA ELI ELISA ELISE ELIZABETH ELLA ELLEN ELLIOT ELLIOTT ELSIE EMILY EMMA ERIC ERICA ERICK ERIK ERIKA ERIN
    ERNEST ESTHER ETHAN EUGENE EVA EVAN EVE EVELYN FAITH FAY FELICIA FELIX FLORENCE FLOYD FRAN FRANCES FRANCIS FRANCISCO
    FRANK FRANKLIN FRED FREDERICK FREDRICK GABRIEL GAIL GARRETT GARY GAYLE GENE GEOFF GEOFFREY GEORGE GERALD GERALDINE
    GERARD GERRY GINA GINGER GLEN GLENDA GLENN GLORIA GORDON GRACE GRAHAM GREG GREGG GREGORY GRETCHEN GUY GWEN HAL HANNAH
    HANS HAROLD HARRIET HARRY HARVEY HAZEL HEATHER HEIDI HELEN HENRY HERBERT HERMAN HILARY HILLARY HOLLY HOPE HOWARD HUGH
    IAN INGRID IRENE IRIS IRVING ISAAC IVAN JACK JACKIE JACOB JACQUELINE JACQUELYN JAKE JAMES JAMIE JAN JANA JANE JANELLE
    JANET JANICE JANIE JANINE JANIS JARED JASMINE JASON JAVIER JAY JAYNE JEAN JEANETTE JEANNE JEANNIE JEANNINE JED JEFF
    JEFFERY JEFFREY JEN JENIFER JENNA JENNIFER JENNY JEREMY JERI JEROME JERRY JESSE JESSICA JESUS JILL JIM JIMMY JO JOAN
    JOANN JOANNA JOANNE JODI JODIE JODY JOE JOEL JOEY JOHN JOHNNY JOLENE JON JONATHAN JONI JORDAN JORGE JOSE JOSEPH JOSH
    JOSHUA JOY JOYCE JUAN JUDITH JUDY JULIA JULIAN JULIE JULIET JUNE JUSTIN KAREN KARI KARIN KARL KARLA KATE KATHARINE
    KATHERINE KATHLEEN KATHRYN KATHY KATIE KATRINA KATY KAY KAYLA KEITH KELLI KELLIE KELLY KELSEY KEN KENDRA KENNETH KENNY
    KENT KERI KERRI KERRY KEVIN KIM KIMBERLY KIRK KIRSTEN KRIS KRISTA KRISTEN KRISTI KRISTIE KRISTIN KRISTINA KRISTINE
    KRISTY KURT KYLE LANCE LARRY LAURA LAUREL LAUREN LAURIE LAWRENCE LEAH LEANN LEE LEIGH LEN LENA LEO LEON LEONARD LEROY
    LESLEY LESLIE LESTER LEWIS LIBBY LILLIAN LINDA LINDSAY LINDSEY LISA LIZ LLOYD LOIS LONNIE LOREN LORI LORNA LORRAINE LOU
    LOUIS LOUISE LUCAS LUCILLE LUCY LUIS LUKE LUTHER LYDIA LYLE LYNDA LYNN LYNNE MABEL MADELINE MAGGIE MANUEL MARC MARCIA
    MARCUS MARCY MARGARET MARGE MARGIE MARGO MARIA MARIAN MARIANNE MARIE MARILYN MARIO MARION MARJORIE MARK MARLENE MARSHA
    MARSHALL MARTHA MARTIN MARTY MARVIN MARY MATT MATTHEW MAUREEN MAX MAXINE MEGAN MEGHAN MEL MELANIE MELINDA MELISSA MELODY
    MELVIN MEREDITH MIA MICHAEL MICHEAL MICHEL MICHELE MICHELLE MIGUEL MIKE MILDRED MILES MILTON MINDY MIRANDA MIRIAM MITCH
    MITCHELL MOHAMED MOHAMMAD MOHAMMED MOLLY MONICA MORGAN MURIEL MYRA MYRON NADINE NANCY NAOMI NATALIE NATASHA NATHAN
    NATHANIEL NEAL NEIL NELSON NICHOLAS NICK NICOLAS NICOLE NIKKI NINA NOAH NOEL NORA NORM NORMA NORMAN OLGA OLIVER OLIVIA
    OSCAR OWEN PAIGE PAM PAMELA PAT PATRICE PATRICIA PATRICK PATSY PATTI PATTY PAUL PAULA PAULINE PEARL PEDRO PEGGY PENNY
    PERRY PETE PETER PHIL PHILIP PHILLIP PHYLLIS PRISCILLA RACHAEL RACHEL RALPH RAMON RANDAL RANDALL RANDI RANDY RAUL RAY
    RAYMOND REBECCA REED REGINA REGINALD RENE RENEE RHONDA RICARDO RICH RICHARD RICK RICKY RITA ROB ROBBIE ROBERT ROBERTA
    ROBERTO ROBIN ROBYN ROCHELLE ROD RODNEY ROGER ROLAND RON RONALD RONNIE RORY ROSA ROSALIE ROSE ROSEMARY ROSS ROXANNE ROY
    RUBEN RUBY RUDY RUSS RUSSELL RUTH RYAN SALLY SAM SAMANTHA SAMUEL SANDRA SANDY SARA SARAH SAUL SCOTT SEAN SERGIO SETH
    SHANE SHANNON SHARI SHARON SHAUN SHAWN SHEILA SHELBY SHELDON SHELLEY SHELLY SHERI SHERMAN SHERRI SHERRY SHERYL SHIRLEY
    SIDNEY SIMON SONIA SONJA SONYA SOPHIA SPENCER STACEY STACI STACY STAN STANLEY STEFAN STEPHAN STEPHANIE STEPHEN STEVE
    STEVEN STEWART STU STUART SUE SUSAN SUSANNE SUZANNE SUZIE SYLVIA TAMARA TAMI TAMMY TANYA TARA TED TERENCE TERESA TERI
    TERRANCE TERRENCE TERRI TERRY THEODORE THERESA THOMAS TIFFANY TIM TIMOTHY TINA TOBY TODD TOM TOMMY TONI TONY TRACEY
    TRACI TRACY TRAVIS TRENT TREVOR TRICIA TROY TYLER VALERIE VANESSA VERA VERN VERNON VERONICA VICKI VICKIE VICKY VICTOR
    VICTORIA VINCE VINCENT VIOLET VIRGINIA VIVIAN WADE WALLACE WALLY WALT WALTER WANDA WARREN WAYNE WENDELL WENDY WES WESLEY
    WHITNEY WILL WILLARD WILLIAM WILLIE WILMA WINSTON YOLANDA YVETTE YVONNE ZACH ZACHARY ZOE
""".split())
_NAME_EXTRAS = frozenset("MR MRS MS DR SEN REP JR SR II III IV MD DDS DMD DC DPM DVM OD PHD CPA ESQ".split())
# Businesses that read exactly like a person, and that really do give in this data.
_NAMED_LIKE_PEOPLE = frozenset({
    "BRIAN CAVE", "BRYAN CAVE", "CHARLES SCHWAB", "EDWARD JONES", "ELI LILLY", "FRED MEYER", "FRED MEYERS", "GENE JUAREZ",
    "GORDON DERR", "GORDON THOMAS HONEYWELL", "JOHN DEERE", "JP MORGAN", "MARY KAY", "MORGAN STANLEY", "PHILIP MORRIS",
    "REED SMITH", "TITUS WILL",
})


def _has_org_word(words: list[str]) -> bool:
    return any(w in _ORGANIZATION_WORDS or w.endswith("PAC") or (len(words) > 2 and w in _ALSO_FAMILY_NAMES)
               for w in words)


def person_shaped(name) -> bool:
    """True when a name reads like a human, as 'First Last' or 'LAST, FIRST'. Either the name as filed
    or its key will do, so the punctuation can be gone by the time this is called."""
    s = text(name)
    if not s or re.search(r"[\d@#/]", s):
        return False
    words = name_key(s).split()
    if not 2 <= len(words) <= 4 or " ".join(words) in _NAMED_LIKE_PEOPLE:
        return False
    if words[-2:] in (["P", "S"], ["P", "C"], ["L", "P"]):      # 'Causey Wright, P.S.'
        return False
    if "AND" in words:      # A couple: 'Mark and Sue Smith', 'SMITH MARK & SUE'. 'Acme and Sons' isn't.
        at = words.index("AND")
        return (len(words) == 4 and 0 < at < 3 and {words[at - 1], words[at + 1]} <= _GIVEN_NAMES
                and not any(w in _ORGANIZATION_WORDS for w in words))
    names = [w for w in words if len(w) > 1 and w not in _NAME_EXTRAS]
    if len(names) < 2 or _has_org_word(names):
        return False
    if s.count(",") == 1 or names[0] in _GIVEN_NAMES or names[-1] in _GIVEN_NAMES:
        return True
    # 'LESTA L. ROGERS', but not 'HAVE A HEART' or 'PARK N JET'.
    return len(words) == 3 and len(words[1]) == 1 and words[1] not in "AN"


_CAMPAIGN_WORDS = frozenset(
    "CAMPAIGN CITIZENS CMTE COMM COMMITTE COMMITTEE CTE ELECT FOR FRIEND FRIENDS PEOPLE REELECT VOTERS".split())
# Organization words that are also how campaigns name themselves: 'Friends of Doe for State Senate'.
_CAMPAIGN_FILLER = frozenset("CONGRESS HOUSE OF STATE THE".split())
# 'SURPLUS LINE ASSOCIATION' is a trade group, and 'Surplus Funds Back LLC' is a business.
_SURPLUS_FIRMS = frozenset({"ASSOCIATION", "ASSN", "INC", "LINE", "LINES", "LLC"})


def own_account(name, own_names: frozenset[str]) -> bool:
    """True for money from the official's own surplus account or campaign. Their name and a campaign
    word aren't enough alone: 'Campaign for Tribal Self-Reliance' reached Rep. Ortiz-Self, and King County
    and Young Democrats committees exist. Nothing else that reads as an organization can be in the name."""
    words = frozenset(name_key(name).split())
    if any(w.startswith("SURPLUS") for w in words):
        return not words & _SURPLUS_FIRMS
    return bool(words & own_names and words & _CAMPAIGN_WORDS
                and not _has_org_word(list(words - own_names - _CAMPAIGN_WORDS - _CAMPAIGN_FILLER)))


def _is_own_name(name, own_names: frozenset[str]) -> bool:
    """The official's name, and not a business or group that happens to share a word with it."""
    key = name_key(name)
    if key.endswith("PERSONAL FUNDS"):      # 'CAND PERSONAL FUNDS'
        return True
    words = frozenset(key.split())
    if own_names and any(all(n in w for n in own_names) for w in words):    # 'VANWERVEN LUANNE' for Van Werven
        words |= own_names
    return bool(words & own_names) and not _has_org_word(list(words - own_names))


def _self_coded_wrongly(code, name, own_names: frozenset[str]) -> bool:
    """The PDC says codes are determined by the filer, and they code PACs and caucuses 'Self'. Without
    the official's name to compare (an empty own_names), the code stands."""
    return (code or "").strip() == "Self" and bool(own_names) and not _is_own_name(name, own_names)


def reads_as_org(name) -> bool:
    return _has_org_word(name_key(name).split()) or org_key(name) in ORG_DISPLAY_NAMES


def source_type(code, category, name, own_names: frozenset[str] = frozenset()) -> str:
    key = name_key(name)
    if key in _SMALL_NAMES:
        return SMALL_CONTRIBUTIONS
    if key in _MISC_NAMES:
        return MISCELLANEOUS
    if key in _LOW_COST_NAMES:
        return LOW_COST
    if own_account(name, own_names):
        return OWN_ACCOUNTS
    miscoded = _self_coded_wrongly(code, name, own_names)
    label = SOURCE_LABELS.get((code or "").strip())
    if label and not miscoded:
        return label
    org = (category or "").strip() == "Organization" or (miscoded and reads_as_org(name))
    return "Other organizations" if org else "Other sources"


def is_org(code, category, name=None, own_names: frozenset[str] = frozenset(), occupation=None) -> bool:
    if name_key(name) in _POOLED_NAMES or own_account(name, own_names):
        return False
    # Having an occupation only counts for a name that reads as a person: filers also fill the box in
    # for Microsoft, AT&T and SEIU PACs, usually with the job of whoever handed over the check.
    if text(occupation) and person_shaped(name):
        return False
    if (code or "").strip() == "Self" and own_names:
        return not _is_own_name(name, own_names) and (reads_as_org(name) or (category or "").strip() == "Organization")
    return (category or "").strip() == "Organization" or (code or "").strip() in (
        "Business", "Political Action Committee", "Union", "Party", "Caucus")


# ---- Where it went -----------------------------------------------------------------------

UNCATEGORIZED = "Not categorized by the filer"
# Older reports have no category code, only free text. These rules are deliberately narrow:
# anything they don't clearly recognize stays "not categorized" instead of being guessed at.
_SPEND_RULES = (
    (r"\bcable\b.*\binternet\b|\binternet\b.*\bcable\b", "Utilities, phone, and other overhead costs"),
    (r"\b(tv|television|cable|broadcast)\b", "Broadcast/cable TV advertising"),
    (r"\bradio\b", "Radio advertising"),
    (r"\b(newspaper|periodical)\b", "Newspaper/periodical advertising"),
    (r"\b(digital|online|internet|facebook|google)\b.*\b(ad|ads|advertising|buy|buys)\b", "Digital advertising"),
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
    (r"\bcharit|\b(food|blood) bank\b", "Charity"),
    (r"(?<!phone )\bbank\b|(?<!mail )(?<!mailing )(?<!paycheck )(?<!payroll )\bprocessing fee|\bmerchant\b|\bcredit card fee",
     "Bank and payment processing charges"),
    (r"\bmileage\b", "Mileage reimbursement"),
    (r"\btravel\b|\bmeals?\b|\bhotel\b|\blodging\b", "Travel, accommodations, meals"),
    (r"\baccounting\b|\blegal\b|\battorney\b|\bcompliance\b|\bbookkeep|\btreasury\b|\btreasurer\b",
     "Accounting, legal, regulatory compliance, etc."),
    (r"\bloan (re)?pay", "Loan payments"),
    (r"\bcredit card\b", "Bank and payment processing charges"),
    (r"\boffice supplies\b|\bsupplies\b", "Office supplies, furniture, staff food & beverages, etc."),
    (r"\bgas\b|\bfuel\b", "Travel, accommodations, meals"),
    (r"\bwebsite\b|\bweb hosting\b|\bsoftware\b|\bdata services\b", "Computers, printers, software, phones, etc."),
    (r"\bcell\b|\bphone\b(?! ?bank)|\btelephone\b|\binternet\b", "Utilities, phone, and other overhead costs"),
    (r"\brent\b|\blease\b", "Rent, lease, mortgage, PO box rental"),
    (r"\brefund", "Contribution Refunds"),
    (r"\bcontribution\b", "Monetary contributions to PAC or candidate"),
    (r"\bin[- ]?kind\b", "Inkind contribution"),
)
_SPEND_RES = [(re.compile(p, re.I), label) for p, label in _SPEND_RULES]


_PARTY_OR_CAUCUS = re.compile(r"\b(democrat\w*|republican\w*|repub|gop|caucus|hdcc|hroc|srcc|wsdc|legislative district)\b", re.I)
SURPLUS_TRANSFER = "Transfer to surplus funds account"
PARTY_TRANSFER = "Transfer to political party or legislative caucus committee"


# Cable that's a wire, not a TV buy.
_NOT_TV_CABLE = re.compile(r"\bties?,? cable\b|\bcable ties?\b|\b(printer|ethernet|usb|hdmi)\s+cable\b|\badapter/cable\b", re.I)


def spend_category(code, description, recipient=None) -> str:
    c = text(code)
    if c:
        return c
    d = text(description)
    if d:
        d = _NOT_TV_CABLE.sub(" ", d)
        for rx, label in _SPEND_RES:
            if rx.search(d):
                # 'SURPLUS CONTRIBUTION TO HOUSE CAUCUS' funded other races. It didn't stay in the account.
                if label == SURPLUS_TRANSFER and _PARTY_OR_CAUCUS.search(text(recipient) or ""):
                    return PARTY_TRANSFER
                return label
    return UNCATEGORIZED
