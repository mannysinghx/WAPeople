# WA People

A website that lists every Washington State bill since 1991 in one place: proposed, ongoing, and past.
Each bill shows who sponsored it, its full history with dates, and every roll-call vote, including how
each legislator voted. The site also lists Washington Supreme Court decisions about legislation and
public policy, linked to the bills they discuss, and shows the money trail for each elected official:
how much they raised, who gave it, where it came from, and where it went.

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
docker compose run --rm sync python -m wapeople.sync money                         # officials' campaign money
docker compose run --rm sync python -m wapeople.sync refresh                       # what the daily job does
```

Tests: `docker compose run --rm --no-deps sync python -m pytest -q -p no:cacheprovider tests`

CI runs on this machine, never on GitHub: `scripts/ci-local.sh [commit]` builds the production image from a
clean checkout of the commit and runs the whole suite (including the database integration tests against a
throwaway Postgres) and a dependency audit, in Linux containers. Run it on the exact commit before every push.

## Deploying on Railway

Railway doesn't run `docker-compose.yml`; the stack is three services in one project:

| Service | Source | Settings |
| --- | --- | --- |
| `Postgres` | Railway PostgreSQL template | — |
| `WAPeople` (website) | this repo, branch `main` | Root directory `/app` (uses `app/Dockerfile`; its default command serves the site on `$PORT`), healthcheck `/healthz`, watch paths `/app/**`, variable `DATABASE_URL=${{Postgres.DATABASE_URL}}` |
| `sync` (daily data refresh) | this repo, branch `main` | Root directory `/app`, start command `python -m wapeople.sync daily`, cron `0 12 * * *` (UTC), restart policy never, watch paths `/app/**`, variables `DATABASE_URL=${{Postgres.DATABASE_URL}}`, `TZ=America/Los_Angeles`, `COURTLISTENER_API_TOKEN` |

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

## Money trails

`/money` lists every sitting legislator, statewide official and Supreme Court justice; `/money/{id}` shows
one person's money, and `/money/organizations` shows who gave across officials. All of it comes from the
Washington Public Disclosure Commission's open data on data.wa.gov (no key needed).

| Tab | PDC dataset |
| --- | --- |
| Campaigns, totals, election results | Campaign Finance Summary `3h9x-7bvm` |
| Contributions | Contributions to Candidates and Political Committees `kv7h-kjye` |
| Spending | Expenditures by Candidates and Political Committees `tijg-9zyp` |
| Loans & debts | Loans `d2ig-r3q4`, Debt `3r6b-hsaa`, Pledges `8bva-rkeb` |
| Outside money | Independent Campaign Expenditures and Electioneering `67cp-h962` |
| Surplus funds | Candidate Surplus Funds Latest `3cbn-54c3`, Surplus Expenditures `ti55-mvy5` |
| Personal finances | Financial Affairs Disclosures `ehbc-shxw` |

- **Who:** legislators come from the Legislature's roster and are linked by `money/link.py`, which needs last
  name, first name, chamber, district and a matching time all to agree, and refuses to choose between two
  matches. Statewide officials and justices, and the few hand-made links, are in
  `data/money_officials.json`. **Update that file when officeholders change** (next: January 2027).
- **Sync:** `python -m wapeople.sync money` runs in the daily job. It compares each campaign's row count and
  total with the PDC's own and reloads only campaigns that differ (`--full` reloads everything). A first load
  takes 8 to 16 minutes and adds about 420 MB. Results are in `sync_runs`; `mismatched` should be 0.
- **Privacy:** the sync's `select` lists never name address, phone or email columns, so those never leave the
  source. `money/disclosure.py` is a whitelist that drops home addresses, family names and anything only about
  a dependent from financial statements. Itemized donor pages are `noindex` and blocked in `robots.txt`.
- **Condition of release:** the PDC's data may not be used for commercial purposes (RCW 42.56.070(9)). The site
  states this on every money page and in the terms. Don't add ads or sell access without reading that first.
- Federal officials report to the FEC and aren't included, nor are county and city officials.

## Contact form

`/contact` stores every message in `contact_messages`, then emails it over SMTP to `CONTACT_TO`
(Reply-To is the sender, if they gave an address). Configure `CONTACT_TO`, `SMTP_HOST`,
`SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD` (a Gmail App Password for Gmail) and `SITE_URL` in the
server environment — never in the code; the address is not shown anywhere on the site. Without
`SMTP_PASSWORD` nothing is attempted and messages wait in the database. Undelivered messages are retried by
the next submission and by the daily sync. Spam protection: hidden honeypot field, signed form timestamp,
per-connection rate limits (IPs are stored only as keyed hashes), and link-markup and SEO-pitch rules: matching
messages are kept with `email_status = 'spam'` but not emailed.

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
| Campaign money, financial statements | [Washington Public Disclosure Commission](https://www.pdc.wa.gov/political-disclosure-reporting-data/open-data) on data.wa.gov (no key) |

CourtListener's search API has required a token since late October 2026; without one it answers 401, existing
decisions stay on the site, new ones aren't collected, and the daily job exits with an error naming the fix.
Set `COURTLISTENER_API_TOKEN` (free account, 5,000 requests/hour), which also gets full opinion text for older
cases that have no PDF.

## How it works

- `app/wapeople/legislature/`: the client for the Legislature services, the status-code-to-stage
  rules (`stages.py`), and the loader. One bulk request returns every bill version since 1991. History
  is pulled in bulk per session; sponsors and roll calls are fetched per bill and only re-fetched when
  a bill has new activity.
- The roster service lists members who have left with a blank name and no party or district. Those rows are
  ignored, because they used to overwrite real names with "Senator"; `repair_members` restores any that remain.
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
