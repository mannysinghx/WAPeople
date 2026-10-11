"""Command-line entry point for loading and refreshing data.

  python -m wapeople.sync initial            # first load: current sessions, courts, then full history
  python -m wapeople.sync refresh            # daily update (current session + new court decisions)
  python -m wapeople.sync legislature --biennium 2025-26 [--force-details]
  python -m wapeople.sync legislature --all
  python -m wapeople.sync courts [--since 2020-01-01] [--no-text]
  python -m wapeople.sync link               # re-link court cases to bills
  python -m wapeople.sync schedule           # long-running: resume any incomplete load, then refresh daily
"""
from __future__ import annotations

import argparse
import logging
import time
from datetime import date, datetime

from .config import SYNC_HOUR_LOCAL
from .courts.sync import CourtSync
from .db import connect
from .legislature.sync import LegislatureSync, all_bienniums, window
from .migrate import migrate
from .runs import tracked_run

log = logging.getLogger("wapeople.sync")


def sync_legislature(bienniums: list[str], details: bool = True, force_details: bool = False,
                     scope: str | None = None) -> None:
    """Sync the given bienniums (newest first), sharing one bulk download."""
    if not bienniums:
        return
    bienniums = sorted(set(bienniums), reverse=True)
    with connect() as conn:
        leg = LegislatureSync(conn)
        oldest = bienniums[-1]
        bulk = leg.load_bulk(window(oldest)[0])
        for b in bienniums:
            with tracked_run("legislature", scope or b) as stats:
                stats.update(leg.sync_biennium(b, versions=bulk.get(b, []), details=details,
                                               force_details=force_details))
        leg.client.close()


def sync_upcoming() -> None:
    """Bills prefiled for a biennium that has not started yet (December before a long session)."""
    known = set(all_bienniums())
    with connect() as conn:
        leg = LegislatureSync(conn)
        upcoming = sorted({r.get("Biennium") for r in leg.client.prefiled()} - known - {None})
        for b in upcoming:
            with tracked_run("legislature", f"{b} (prefiled)") as stats:
                stats.update(leg.sync_biennium(b, versions=[]))
        leg.client.close()


def sync_courts(since: str | None = None, texts: bool = True) -> None:
    with connect() as conn, tracked_run("courts", f"since {since or 'default'}") as stats:
        cs = CourtSync(conn)
        stats.update(cs.collect(filed_after=since))
        if texts:
            stats.update(cs.fetch_texts())
        stats.update(cs.extract_and_link())
        cs.cl.close()


def link() -> None:
    with connect() as conn, tracked_run("link") as stats:
        stats.update(CourtSync(conn).extract_and_link())


def incomplete_bienniums() -> list[str]:
    with connect() as conn:
        done = {
            r["id"] for r in conn.execute(
                "SELECT id FROM bienniums WHERE bills_synced_at IS NOT NULL AND details_synced_at IS NOT NULL"
            )
        }
    return [b for b in all_bienniums() if b not in done]


def initial() -> None:
    """First load. Ordered so the site becomes useful quickly; safe to re-run (resumes)."""
    migrate()
    bs = all_bienniums()
    todo = incomplete_bienniums()
    recent = [b for b in bs[-2:] if b in todo]
    log.info("initial load: recent bienniums %s", recent)
    sync_legislature(recent, scope="initial (recent)")
    sync_upcoming()
    try:
        with connect() as conn:
            n_cases = conn.execute("SELECT count(*) AS n FROM court_cases").fetchone()["n"]
        if n_cases == 0:
            sync_courts()
        else:
            with connect() as conn, tracked_run("courts", "texts") as stats:
                stats.update(CourtSync(conn).fetch_texts())
    except Exception:
        # Court data is independent of bill history; don't let it block the backfill.
        log.exception("court sync failed during initial load; continuing with bill history")
    older = [b for b in incomplete_bienniums() if b not in bs[-2:]]
    log.info("initial load: backfilling %d older bienniums", len(older))
    sync_legislature(older, scope=None)
    link()


def refresh(today: date | None = None) -> None:
    today = today or date.today()
    bs = all_bienniums(today)
    targets = [bs[-1]] + ([bs[-2]] if today.month == 1 else [])
    sync_legislature(targets, scope="refresh")
    sync_upcoming()
    with connect() as conn, tracked_run("courts", "refresh") as stats:
        cs = CourtSync(conn)
        stats.update(cs.refresh())
        cs.cl.close()


def last_ok(job_scope: str) -> datetime | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT max(finished_at) AS t FROM sync_runs WHERE status = 'ok' AND (job || ':' || coalesce(scope,'')) LIKE %s",
            (job_scope,),
        ).fetchone()
    return row["t"] if row else None


def schedule() -> None:
    migrate()
    while True:
        try:
            if incomplete_bienniums():
                initial()
            now = datetime.now().astimezone()
            last = last_ok("courts:refresh")
            due = last is None or (now - last).total_seconds() > 20 * 3600
            if due and now.hour >= SYNC_HOUR_LOCAL:
                log.info("daily refresh starting")
                refresh()
        except Exception:
            log.exception("sync cycle failed; will retry")
            time.sleep(1800)
        time.sleep(900)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    p = argparse.ArgumentParser(prog="wapeople.sync")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("initial")
    sub.add_parser("refresh")
    sub.add_parser("schedule")
    sub.add_parser("link")
    sub.add_parser("migrate")
    lp = sub.add_parser("legislature")
    lp.add_argument("--biennium", action="append", default=[])
    lp.add_argument("--all", action="store_true")
    lp.add_argument("--no-details", action="store_true")
    lp.add_argument("--force-details", action="store_true")
    cp = sub.add_parser("courts")
    cp.add_argument("--since")
    cp.add_argument("--no-text", action="store_true")
    a = p.parse_args()

    if a.cmd == "migrate":
        migrate()
    elif a.cmd == "initial":
        initial()
    elif a.cmd == "refresh":
        migrate()
        refresh()
    elif a.cmd == "schedule":
        schedule()
    elif a.cmd == "link":
        link()
    elif a.cmd == "legislature":
        migrate()
        targets = all_bienniums() if a.all else a.biennium
        if not targets:
            p.error("give --biennium YYYY-YY (repeatable) or --all")
        sync_legislature(targets, details=not a.no_details, force_details=a.force_details)
    elif a.cmd == "courts":
        migrate()
        sync_courts(since=a.since, texts=not a.no_text)


if __name__ == "__main__":
    main()
