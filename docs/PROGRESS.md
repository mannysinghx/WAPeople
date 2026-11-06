# WA People progress

## Where it stopped (2026-11-05)

- Branch `claude/wa-people-money-trails-28ed0d`, based on `main` at `15ce984`. See `git log` for the last commit.
- Nothing is pushed or deployed from this work yet; wapeople.org is unchanged. Waiting for the owner's OK on one push (see Next steps).
- Uncommitted: only `.claude/launch.json`, a local preview config that is not part of the project.

## Done and verified

Each item was checked against the live site, production logs, or the PDC's own totals.

| Change | Proof |
| --- | --- |
| Money trails: `/money`, `/money/{id}` (7 tabs), `/money/organizations` | 147 of 147 sitting legislators and 18 of 18 statewide officials and justices linked; pages reviewed in a browser, desktop and phone |
| Money trails for all 12 members of Congress (FEC bulk files, no key): one page per person, `?records=state` for any state records, state and federal never added together | Dev load: 591,951 rows, 107 cycles. Totals match the FEC API to the cent on 5 of 5 spot checks; 12 of 12 sampled rows matched by `sub_id`; no cycle has itemized rows above reported totals |
| Independent review of the whole batch: 98 findings, 17 high (privacy leaks in statements and free text, inflated totals, a connection-pool deadlock, a broken state tab, state and federal added together, a wrong citation) | All addressed in `d6bcbbd`; full reload on the final code; 12 concurrent requests all 200, slowest 0.7 s |
| Data matches the source | Final dev load (644 s, 415 people linked, none unlinked): contributions 1,003,969, expenditures 212,544, debts 22,767, loans 1,375, pledges 961, each `mismatched: 0` against the PDC's own count and total |
| 15 real legislators were displayed as "Senator" or "Representative" | Live: `/legislators/15814` shows "Senator" for Ann Rivers (240 bills, 9,359 votes). Fix restored 15 of 15 on the dev database from the Legislature's older rosters |
| Daily `sync` job crashed every day since 2026-10-28 | Log: CourtListener 401. Fixed to run every other step and name the fix; still needs a token (below) |
| Contact email never delivered | `SMTP_PASSWORD` is unset on both Railway services; retries were being used up. No attempts now without a password; still needs the password (below) |
| Contact spam | All 9 stored messages are spam (checked by reading them); rules hold back this kind without blocking other languages |
| HEAD answered 405 on every page | `curl -I https://wapeople.org/`; now 200 |
| No sitemap, canonical links, share tags or favicon | `/sitemap.xml` and `/favicon.ico` were 404 |
| Duplicate site at the Railway address, `SITE_URL` pointing there | Live values checked; redirect added, env change is part of the deploy |
| `/legislators` showed 151 members for 147 seats with no explanation | Members who left during the term are now labeled |
| Home page said "so far" after final adjournment | Wording follows session status |
| `pypdf` 6.20.0 had 16 known vulnerabilities | `pip-audit` in `scripts/ci-local.sh`; upgraded to 6.21.4, audit now clean |
| Street addresses typed into city, employer and description fields | Dev load: 113 city rows and 5 employer rows matched before; all free-text fields scan at 0 after, except business names such as `515 Park Place LLC` |

Tests: `scripts/ci-local.sh` must pass on the exact commit before the push; the last result is recorded under Next steps.

## In flight

Nothing that writes. The dev database and dev server below can be deleted at any time.

## Open decisions

| Item | Where | Who |
| --- | --- | --- |
| Approve the one push to `main` | below | Owner |
| Set `SMTP_PASSWORD` (Gmail App Password) on `WAPeople` and `sync` | Railway variables | Owner: only they can create it |
| Set `COURTLISTENER_API_TOKEN` on `sync` (free account) | Railway variables | Owner: creating accounts isn't something Claude does |
| `www.wapeople.org` has no DNS record | CNAME at Namecheap, then add the domain in Railway | Owner |
| County and city officials | New work, not started | Owner |

Defaults chosen, owner can overrule:

| Default | Where |
| --- | --- |
| Domestic violence detail removed from Rep. Lauren Davis's financial statement | `app/wapeople/money/disclosure.py` `sanitize` |
| Spouse or partner income shown with a label and no name | `app/wapeople/money/disclosure.py` `WHO` |
| Inslee titled "Former State Representative" | `app/wapeople/data/money_officials.json` |
| Public records citation is RCW 42.56.070(8), though the PDC's notice still prints (9) | `privacy.html`, `about.html` |

Known limits:

- PDC candidate records begin with the 2009–10 cycle. "Mini reporting" campaigns have no itemized rows.
- The committee funding trail matches names by fixed rules, so it stops at names that don't match.
- Scrubbing is by rule, so it can miss things.
- Outside spending can run high when filers restate without an amendment (Women Vote!/Murray 2022).
- Some small businesses read as people's names (`JED DOORS`) and get no organization page.
- A person-shaped row with an occupation keeps the "Businesses" source label.
- One statement with `person.name` "New filer" over-redacts ("Mullet Moo LLC"). "CATHY DALQUIST SURPLUS FUNDS" reads as an own account.

## Next steps, in order

1. Owner approves the push.
2. Set `SITE_URL=https://wapeople.org` on `WAPeople` and `sync` (Railway, no deploy of its own).
3. Run `scripts/ci-local.sh` on the exact commit, then push to `main` (`git push origin HEAD:main`; fast-forward from `15ce984`). Railway redeploys `WAPeople` and `sync` (both watch `/app/**`). The web service applies migrations 006, 007 and 008 at startup. All new tables except up to 15 member names restored, about 16 empty `member_terms` rows deleted, and spam contact rows marked `spam` when the sync runs.
4. Run the first load: run the `sync` service now or wait for the 12:00 UTC cron. State about 11 minutes, FEC 7 to 32 minutes, about 940 MB more in Postgres; `/money` says "loading" until then.
5. Verify live: `/money`, `/money/18264`, `/money/299`, `/legislators/15814` shows Ann Rivers, `curl -I https://wapeople.org/` is 200, the `sync_runs` row for `money` has `mismatched: 0`, the `fec` row has `cycles_operating_above_reported: 0`.
6. January 2027: update `app/wapeople/data/money_officials.json` (statewide, justices and `federal`) for officeholders taking office after the November 3, 2026 election.

## How to resume

```bash
# Isolated dev database (never the wapeople_pgdata volume): container wapeople-money-db on 127.0.0.1:55491
docker start wapeople-money-db
cd app && DATABASE_URL=postgresql://wapeople:devonly@localhost:55491/wapeople python -m wapeople.sync money
DATABASE_URL=postgresql://wapeople:devonly@localhost:55491/wapeople python -m wapeople.sync fec
TEST_DATABASE_URL=postgresql://wapeople:devonly@localhost:55491/wapeople_test python -m pytest -q tests
scripts/ci-local.sh
```

Delete the dev database when finished: `docker rm -f wapeople-money-db && docker volume rm wapeople_money_dev`.

Inputs: all in the repo. The roster and hand-made links are in `app/wapeople/data/money_officials.json`; data comes live from data.wa.gov and the FEC's public bulk files. The dev database started from `backups/wapeople-20261011-1156.sql.gz` in the main checkout.

Keep parallel work low on this Mac: 19 agents at once crashed it on 2026-11-05. At most 2, and no crawlers.
