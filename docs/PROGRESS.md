# WA People progress

## Where it stopped (2026-11-05, PST)

- **Live on wapeople.org.** `main` is at `d684aee`, deployed to `WAPeople` (`fe788e34`) and `sync` (`1fbd10b3`). The first production load finished at 04:23 UTC on 2026-11-06.
- Branch `claude/wa-people-money-trails-28ed0d` holds this document's latest update, which is docs only. A docs-only push doesn't redeploy anything (Railway watches `/app/**`).
- Uncommitted: nothing.

## Done and verified

Each item was checked against the live site, production logs, or the PDC's own totals.

| Change | Proof |
| --- | --- |
| Money trails: `/money`, `/money/{id}` (7 tabs), `/money/organizations` | 147 of 147 sitting legislators and 18 of 18 statewide officials and justices linked; pages reviewed in a browser, desktop and phone |
| Money trails for all 12 members of Congress (FEC bulk files, no key): one page per person, `?records=state` for any state records, state and federal never added together | Final dev load (386 s): 616,173 rows, 130 cycles, 23 people (12 members and 11 state officials who ran). Totals match the FEC API to the cent on 5 of 5 spot checks, and 3 more against fec.gov; 12 of 12 sampled rows matched by `sub_id`; no cycle has itemized rows above reported totals |
| Independent review of the whole batch: 98 findings, 17 high (privacy leaks in statements and free text, inflated totals, a connection-pool deadlock, a broken state tab, state and federal added together, a wrong citation) | Addressed in `d6bcbbd`; 12 concurrent requests all 200, slowest 0.7 s. A second independent re-test found 88 fixed and 10 narrow leftovers, all fixed in the next commit |
| Coverage and accuracy check | 177 of 177 current officials have a page. Seven officials' totals matched the PDC or fec.gov to the cent, and all 1,971 loaded campaigns match the live PDC summary |
| 11 sitting state officials' campaigns for Congress were missing ($16.7M) | Found by matching every current official to the FEC's own candidate files, 2000 to 2026; now a federal tab on each page. List is `federal_runs` in `money_officials.json` |
| In-kind gifts read as payments | $19.08M in-kind money in against $19.09M in-kind money out, 8,562 of 8,576 name-and-year groups equal, so these are the gifts themselves. Labeled and left out of "who the campaigns paid" |
| One filing listed under two reports doubled $361,563 of Murray's 2004 gifts | 611 groups share an image number under Q1 and Q2; counted once now |
| `CourtListener` token | Owner set it on `sync` on 2026-11-05. One search request from that service's variables: 200 with the token, 401 without |
| Data matches the source | Final dev load (810 s, 415 people linked, none unlinked): contributions 1,003,969, expenditures 212,544, debts 22,767, loans 1,375, pledges 961, each `mismatched: 0` against the PDC's own count and total |
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

Tests: `scripts/ci-local.sh` must pass on the exact commit before the push. The result goes in the push proposal, then in `docs/OPS_LOG.md` with the deploy.

## In flight

Nothing.

## Live checks after the deploy

| Check | Result |
| --- | --- |
| `sync_runs`, first production load | `legislature`, `courts`, `money`, `fec`, `contact` all `ok`, about 19 minutes in all: state 634 s, federal 321 s |
| State money against the PDC | contributions 1,003,969, expenditures 212,544, debts 22,767, loans 1,375, pledges 961, all `mismatched: 0`; 415 people linked, none unlinked |
| Federal money | 616,173 rows, 130 cycles, 23 people, receipts $338,824,476.78, `cycles_operating_above_reported: 0`. Same figures as the final dev load |
| Coverage | 177 officials in office (147 legislators, 9 statewide, 9 justices, 12 in Congress), none without records |
| Existing data | Placeholder names 15 to 0, empty terms 16 to 0, `/legislators/15814` shows Ann Rivers. The 9 stored contact messages stay `failed`, not `spam`, because the spam rule only reads messages still waiting to send |
| Privacy scan on production | Phones and emails in expenses and statements, and long ZIP codes: 0 |
| Pages | The same page checks as on dev all passed live; 12 requests at once all 200, slowest 0.94 s; `curl -I /` is 200; the Railway address redirects to wapeople.org |
| Courts with the token | Step `ok`, 1,138 cases, no new ones matched; newest case on file is 2026-09-17 |
| Size | Database 835 MB to 1,519 MB of a 50 GB volume |

Not checked on production: the next scheduled run at 12:00 UTC, and whether search engines honor the noindex rules.

## Open decisions

| Item | Where | Who |
| --- | --- | --- |
| Delete `COURTLISTENER_API_TOKEN` from `WAPeople` (harmless, not used there) | Railway variables | Owner |
| Set `SMTP_PASSWORD` (Gmail App Password) on `WAPeople` and `sync` | Railway variables | Owner: only they can create it |
| `www.wapeople.org` has no DNS record | CNAME at Namecheap, then add the domain in Railway | Owner |
| County and city officials | New work, not started | Owner |
| Rank `/money` on new money only? Miscellaneous receipts and transfers from a candidate's own accounts are re-counted money (30% of one official's raised). Disclosed on each page, but the list ranks on the gross figure | `money_queries.py` `PEOPLE_SORTS` | Owner |
| Itemize leadership and joint fundraising committees? 77 are listed but not counted | `fec.py` | Owner |
| Load state outside-spending vendors and ad descriptions (PDC C6.2 rows)? The federal tab shows both; the state tab doesn't | `sync.py` `sync_outside` | Owner |

Defaults chosen, owner can overrule:

| Default | Where |
| --- | --- |
| Domestic violence detail removed from Rep. Lauren Davis's financial statement | `app/wapeople/money/disclosure.py` `sanitize` |
| Spouse or partner income shown with a label and no name | `app/wapeople/money/disclosure.py` `WHO` |
| Inslee titled "Former State Representative" | `app/wapeople/data/money_officials.json` |
| Public records citation is RCW 42.56.070(8), though the PDC's notice still prints (9) | `privacy.html`, `about.html` |
| A spouse's employer and job title stay on financial statements, without a name, because they're what the disclosure is for. They can still identify someone. "Child support" lines with no names stay too | `app/wapeople/money/disclosure.py` |
| A sitting state official who ran for Congress keeps a state page with a federal tab; former legislators' federal campaigns aren't loaded | `federal_runs` in `money_officials.json` |

Known limits:

- PDC candidate records begin with the 2009–10 cycle. "Mini reporting" campaigns have no itemized rows.
- The committee funding trail matches names by fixed rules, so it stops at names that don't match.
- Scrubbing is by rule, so it can miss things.
- Outside spending can run high when filers restate without an amendment (Women Vote!/Murray 2022).
- A plain two- or three-word name with no organization word, under $5,000 or fewer than 3 officials, stays out of search and off indexed pages, because it could be a person's. That catches small businesses too (`KENT BOWL`), and they're still in the Contributions tab. About 260 names ($95K) that most filers report as individuals have no organization page, including a few real firms.
- Donors are grouped by name and state, so two people with one name in one state still merge. Grouping is on the page's Top donors table only.
- State "Who the campaigns paid" and "Where the money went" don't add up to the same figure on purpose: in-kind gifts are spending as filed, but nobody was paid.
- Surplus accounts' received and spent figures are the PDC's own and don't match the sum of their payment rows, and many of those rows are $0.00 lines filers use for small expenses.
- `MICROSOFT` and `MICROSOFT CORP` now rank as one employer; other spellings still don't merge.
- A person-shaped row with an occupation keeps the "Businesses" source label.
- One statement with `person.name` "New filer" over-redacts ("Mullet Moo LLC"). "CATHY DALQUIST SURPLUS FUNDS" reads as an own account.

## Next steps, in order

1. Check that the 12:00 UTC `sync` run on 2026-11-06 finishes with every step `ok` and reloads little.
2. Owner: set `SMTP_PASSWORD`, add the `www` CNAME, and delete the unused token from `WAPeople`.
3. January 2027: update `app/wapeople/data/money_officials.json` (statewide, justices, `federal` and `federal_runs`) for officeholders taking office after the November 3, 2026 election.

## How to resume

```bash
# A dev database, separate from the wapeople_pgdata volume. The one used for this work has been deleted.
docker run -d --name wapeople-money-db -p 127.0.0.1:55491:5432 -e POSTGRES_USER=wapeople -e POSTGRES_PASSWORD=devonly -e POSTGRES_DB=wapeople -v wapeople_money_dev:/var/lib/postgresql postgres:18-alpine
gunzip -c backups/wapeople-20261011-1156.sql.gz | docker exec -i wapeople-money-db psql -q -U wapeople -d wapeople
cd app
DATABASE_URL=postgresql://wapeople:devonly@localhost:55491/wapeople python -m wapeople.migrate
DATABASE_URL=postgresql://wapeople:devonly@localhost:55491/wapeople python -m wapeople.sync money
DATABASE_URL=postgresql://wapeople:devonly@localhost:55491/wapeople python -m wapeople.sync fec
cd .. && scripts/ci-local.sh
```

Read production without writing: `railway ssh --project 1b422108-9d10-4bed-9514-96a15124f8d0 --environment production --service WAPeople -- python -`, with a script that connects using `options="-c default_transaction_read_only=on"`.

Roll back: Railway deployments `efbecf38` (`WAPeople`) and `a2df1aee` (`sync`) are the last ones before this work.

Inputs: all in the repo. The roster and hand-made links are in `app/wapeople/data/money_officials.json`; data comes live from data.wa.gov and the FEC's public bulk files. The backup above is in the main checkout.

A deployment of the `sync` service can start a run on its own, without waiting for the schedule, and redeploying stops a run in progress. Keep parallel work low on this Mac: 19 agents at once crashed it on 2026-11-05. At most 2, and no crawlers.
