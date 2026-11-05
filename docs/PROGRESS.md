# WA People progress

## Where it stopped (2026-11-05)

- Branch `claude/wa-people-money-trails-28ed0d`, based on `main` at `15ce984`. See `git log` for the last commit.
- Nothing is pushed or deployed from this work yet. Waiting for the owner's OK on one push (see Next steps).
- Uncommitted: only `.claude/launch.json`, a local preview config that is not part of the project.

## Done and verified

Each item was checked against the live site, production logs, or the PDC's own totals.

| Change | Proof |
| --- | --- |
| Money trails: `/money`, `/money/{id}` (7 tabs), `/money/organizations` | 147 of 147 sitting legislators and 18 of 18 statewide officials and justices linked; pages reviewed in a browser, desktop and phone |
| Data matches the source | Dev load: contributions, spending, loans, debts and pledges all equal the PDC's own count and total to the cent; 1,750 of 1,756 campaign summaries match itemized rows exactly (the other 6 are 2026 and 2028 campaigns still filing) |
| 15 real legislators were displayed as "Senator" or "Representative" | Live: `/legislators/15814` shows "Senator" for Ann Rivers (240 bills, 9,359 votes). Fix restored 15 of 15 on the dev database from the Legislature's older rosters |
| Daily `sync` job crashed every day since 2026-10-28 | Log: CourtListener 401. Fixed to run every other step and name the fix; still needs a token (below) |
| Contact email never delivered | `SMTP_PASSWORD` is unset on both Railway services; retries were being used up. No attempts now without a password; still needs the password (below) |
| Contact spam | All 9 stored messages are spam (checked by reading them); rules hold back this kind without blocking other languages |
| HEAD answered 405 on every page | `curl -I https://wapeople.org/`; now 200 |
| No sitemap, canonical links, share tags or favicon | `/sitemap.xml` and `/favicon.ico` were 404 |
| Duplicate site at the Railway address, `SITE_URL` pointing there | Live values checked; redirect added, env change is part of the deploy |
| `/legislators` showed 151 members for 147 seats with no explanation | Members who left during the term are now labeled |
| Home page said "so far" after final adjournment | Wording follows session status |

Tests: `TEST_DATABASE_URL=... python -m pytest tests` and `scripts/ci-local.sh` (results are in `docs/OPS_LOG.md`).

## In flight

Nothing. The dev database and dev server below can be deleted at any time.

## Open decisions

| Item | Where | Who |
| --- | --- | --- |
| Approve the one push to `main` | below | Owner |
| Set `SMTP_PASSWORD` (Gmail App Password) on `WAPeople` and `sync` | Railway variables | Owner: only they can create it |
| Set `COURTLISTENER_API_TOKEN` on `sync` (free account) | Railway variables | Owner: creating accounts isn't something Claude does |
| `www.wapeople.org` has no DNS record | CNAME at Namecheap, then add the domain in Railway | Owner |
| Show spouse or partner income on `/money/{id}?view=disclosures`? Currently labeled without a name | `app/wapeople/money/disclosure.py` `WHO` | Owner |
| Add federal officeholders (FEC data, needs a free API key) and county and city officials | New work, not started | Owner |

Known limits, stated on the site: PDC candidate records begin with the 2009–10 cycle; "mini reporting" campaigns have no itemized rows; the committee funding trail matches names by fixed rules, so it stops at names that don't match.

## Next steps, in order

1. Owner approves the push.
2. Set `SITE_URL=https://wapeople.org` on `WAPeople` and `sync` (Railway, no deploy of its own).
3. Run `scripts/ci-local.sh` on the exact commit, then push to `main`. Railway redeploys `WAPeople` and `sync` (both watch `/app/**`). The web service applies migration 006 at startup (new tables only).
4. Run the first money load: run the `sync` service now or wait for the 12:00 UTC cron. About 8 minutes; `/money` says "loading" until then.
5. Verify live: `/money`, `/money/18264`, `/legislators/15814` shows Ann Rivers, `curl -I https://wapeople.org/` is 200, the `sync_runs` row for `money` has `mismatched: 0`.
6. January 2027: update `app/wapeople/data/money_officials.json` for officeholders taking office after the November 3, 2026 election.

## How to resume

```bash
# Isolated dev database (never the wapeople_pgdata volume): container wapeople-money-db on 127.0.0.1:55491
docker start wapeople-money-db
cd app && DATABASE_URL=postgresql://wapeople:devonly@localhost:55491/wapeople python -m wapeople.sync money
TEST_DATABASE_URL=postgresql://wapeople:devonly@localhost:55491/wapeople_test python -m pytest -q tests
scripts/ci-local.sh
```

Delete the dev database when finished: `docker rm -f wapeople-money-db && docker volume rm wapeople_money_dev`.

Inputs: all in the repo. The roster and hand-made links are in `app/wapeople/data/money_officials.json`; data comes live from data.wa.gov. The dev database started from `backups/wapeople-20261011-1156.sql.gz` in the main checkout.
