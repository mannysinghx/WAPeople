"""Apply SQL migrations in wapeople/migrations in filename order, once each."""
from __future__ import annotations

import logging
import time
from pathlib import Path

import psycopg

from .db import connect

log = logging.getLogger(__name__)
MIGRATIONS = Path(__file__).parent / "migrations"


def migrate() -> list[str]:
    applied_now: list[str] = []
    for attempt in range(30):
        try:
            c = connect()
            break
        except psycopg.OperationalError:
            if attempt == 29:
                raise
            time.sleep(2)
    with c:
        # Serialize concurrent migrators (web + sync containers start together).
        c.execute("SELECT pg_advisory_xact_lock(727274)")
        c.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
        )
        done = {r["name"] for r in c.execute("SELECT name FROM schema_migrations")}
        for path in sorted(MIGRATIONS.glob("*.sql")):
            if path.name in done:
                continue
            log.info("applying migration %s", path.name)
            c.execute(path.read_text())
            c.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (path.name,))
            applied_now.append(path.name)
        # Reference data bundled with the code (district boundaries).
        from .districts import ensure_loaded
        ensure_loaded(c)
    return applied_now


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    names = migrate()
    print("migrations applied:", ", ".join(names) if names else "none (up to date)")
