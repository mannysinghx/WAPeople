# WA People

A website that lists every Washington State bill since 1991 in one place: proposed, ongoing, and past.
Each bill shows who sponsored it, its full history with dates, and every roll-call vote, including how
each legislator voted. The site also lists Washington Supreme Court decisions about legislation and
public policy, linked to the bills they discuss.

Everything is open source: **PostgreSQL** (storage + full-text search), **Python / FastAPI / Jinja2**
(website), **httpx + pypdf** (data collection), run with **Docker Compose**.

## Run it

```bash
cp .env.example .env              # then set POSTGRES_PASSWORD
docker compose up -d --build      # database, website (http://127.0.0.1:8095), daily sync
```

On first start the `sync` service loads data by itself. It starts with the current and previous
sessions so the site is usable within minutes, then collects court decisions and backfills older
sessions. It picks up where it left off if restarted. To run steps by hand:

```bash
docker compose run --rm sync python -m wapeople.sync initial                       # everything, resumable
docker compose run --rm sync python -m wapeople.sync legislature --biennium 2025-26
docker compose run --rm sync python -m wapeople.sync courts --since 2024-01-01
docker compose run --rm sync python -m wapeople.sync refresh                       # what the daily job does
```

Tests: `docker compose run --rm --no-deps sync python -m pytest -q -p no:cacheprovider tests`

## Deploying on Railway

Railway doesn't run `docker-compose.yml`; the stack is three services in one project:

| Service | Source | Settings |
| --- | --- | --- |
| `Postgres` | Railway PostgreSQL template | — |
| `WAPeople` (website) | this repo, branch `main` | Root directory `/app` (uses `app/Dockerfile`; its default command serves the site on `$PORT`), healthcheck `/healthz`, watch paths `/app/**`, variable `DATABASE_URL=${{Postgres.DATABASE_URL}}` |
| `sync` (daily data refresh) | this repo, branch `main` | Root directory `/app`, start command `python -m wapeople.sync daily`, cron `0 12 * * *` (UTC), restart policy never, watch paths `/app/**`, variables `DATABASE_URL=${{Postgres.DATABASE_URL}}`, `TZ=America/Los_Angeles`, optional `COURTLISTENER_API_TOKEN` |

`sync daily` loads any missing history on an empty database (about 1.5 hours for 1991 onward;
court decisions fill in over a few days without a CourtListener token), then refreshes and exits.
To start from an existing database instead, restore a `scripts/backup.sh` dump into the Railway
Postgres before the first run.

## District map

`/map` shows Washington's 49 legislative districts with each district's senator and two
representatives. Boundaries are the official **Washington State Legislative Districts 2024**
(court-ordered Remedial Map 3B) from the Office of Financial Management, simplified with
mapshaper to within 5 m of the official lines (`scripts/build_districts.sh` rebuilds the file;
provenance is in `app/wapeople/data/*.meta.json`). They're stored in the `legislative_districts`
table at startup. Map rendering uses MapLibre GL JS (bundled in `static/vendor`) and OpenFreeMap tiles.

## Contact form

`/contact` stores every message in `contact_messages`, then emails it over SMTP to `CONTACT_TO`
(Reply-To is the sender, if they gave an address). Configure `CONTACT_TO`, `SMTP_HOST`,
`SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD` (a Gmail App Password for Gmail) and `SITE_URL` in the
server environment — never in the code; the address is not shown anywhere on the site. Undelivered
messages are retried by the next submission and by the daily sync. Spam protection: hidden
honeypot field, signed form timestamp, and per-connection rate limits (IPs are stored only as keyed hashes).

## Security and privacy

- Security headers on every response (`app/wapeople/web/security.py`): strict Content-Security-Policy
  (scripts only from this site; OpenFreeMap is the only third-party host, for map tiles), HSTS on HTTPS,
  `X-Frame-Options: DENY`, `nosniff`, Referrer-Policy, Permissions-Policy (only geolocation, for "Find my
  district"), and a 64 KB request-body cap.
- No cookies, analytics, or access logs of visitor IPs (`uvicorn --no-access-log`). Contact messages are
  deleted after `CONTACT_RETENTION_DAYS` (default 730) and their keyed IP hashes cleared after 7 days by the
  daily sync, as promised on `/privacy`.
- Pages: `/privacy`, `/terms`, `/disclaimer`; `/.well-known/security.txt` points to the contact form.
- Dependencies are pinned; check them with
  `docker compose run --rm --no-deps -u root sync sh -c "pip install -q pip-audit && pip-audit"`.

## Data sources

| Data | Source |
| --- | --- |
| Bills, versions, sponsors, history, roll calls, legislators | [WA Legislature web services](https://wslwebservices.leg.wa.gov/) (public, no key) |
| Supreme Court decisions | [CourtListener](https://www.courtlistener.com/) search API (Free Law Project) + official PDFs from [courts.wa.gov](https://www.courts.wa.gov/opinions/) |

Anonymous CourtListener access is limited to 50 requests per hour. The first court load needs about
120 requests, so it takes roughly 2.5 hours (it paces itself automatically). Daily updates need only a
handful of requests. Set `COURTLISTENER_API_TOKEN` in `.env` (free account, 5,000 requests/hour) to make
the first load take minutes and to get full opinion text for older cases that have no PDF.

## How it works

- `app/wapeople/legislature/`: the client for the Legislature services, the status-code-to-stage
  rules (`stages.py`), and the loader. One bulk request returns every bill version since 1991. History
  is pulled in bulk per session; sponsors and roll calls are fetched per bill and only re-fetched when
  a bill has new activity.
- Whether a bill passed the House or Senate comes from its recorded final-passage roll call
  (the service's "passed House/Senate" lists turned out to equal the "passed Legislature" list).
- `app/wapeople/legislature/summaries.py`: plain-language summaries from the Legislature's
  nonpartisan staff Bill Analyses/Reports ("Brief Summary of Bill"), choosing the report on the
  bill's live version (version read from the document name; the service's BillId tag is unreliable).
- `app/wapeople/courts/`: CourtListener search queries (one per policy category, see `CATEGORIES`),
  de-duplication of the archive's duplicate records, PDF text extraction, and bill linking.
  Bill links are made only when they're unambiguous (session-law chapter, or a bill number with a year).
- `app/wapeople/web/`: server-rendered pages. Works without JavaScript, on phones, and in dark mode.
- `app/wapeople/migrations/`: SQL schema, applied automatically at startup.

## Operations

- Data lives in the Docker volume `wapeople_pgdata` and persists across restarts and rebuilds.
- Back up: `scripts/backup.sh` writes to `backups/`. Restore instructions are in the script header.
- Database shell: `scripts/psql.sh`
- Every sync run is recorded in the `sync_runs` table (shown on the About page).
- Side-effecting operations are logged in `docs/OPS_LOG.md`.
