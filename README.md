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
