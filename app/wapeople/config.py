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

# Contact form delivery. Set these in the server environment only — never in the code:
# the repository is public and the recipient address must not appear on the site.
CONTACT_TO = os.environ.get("CONTACT_TO") or None
SMTP_HOST = os.environ.get("SMTP_HOST") or None
SMTP_PORT = int(os.environ.get("SMTP_PORT") or 587)
SMTP_USER = os.environ.get("SMTP_USER") or None
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD") or None
SMTP_FROM = os.environ.get("SMTP_FROM") or SMTP_USER
# STARTTLS is required for real providers (Gmail: smtp.gmail.com:587); "false" only for local test servers.
SMTP_STARTTLS = (os.environ.get("SMTP_STARTTLS") or "true").lower() != "false"
SITE_URL = (os.environ.get("SITE_URL") or "").rstrip("/")
# Key for form tokens and rate-limit hashes; falls back to one derived from the database URL.
SECRET_KEY = os.environ.get("SECRET_KEY") or None
