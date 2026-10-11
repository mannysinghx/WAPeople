import os

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://wapeople:wapeople@localhost:55440/wapeople"
)

# Washington State Legislature web services (public, no key required).
LEG_BASE = "https://wslwebservices.leg.wa.gov"
# The services' bill data starts with the 1991-92 biennium.
FIRST_BIENNIUM_START = 1991

# CourtListener (Free Law Project) — open legal data. Token is optional.
COURTLISTENER_BASE = "https://www.courtlistener.com/api/rest/v4"
COURTLISTENER_TOKEN = os.environ.get("COURTLISTENER_API_TOKEN") or None
COURT_CASES_SINCE = os.environ.get("COURT_CASES_SINCE") or "1991-01-01"

USER_AGENT = "WAPeople/0.1 (open-source civic legislation tracker)"

SYNC_HOUR_LOCAL = int(os.environ.get("SYNC_HOUR_LOCAL", "5"))
