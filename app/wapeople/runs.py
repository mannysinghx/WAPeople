"""Record each sync job in sync_runs so the site can show data freshness."""
from __future__ import annotations

import traceback
from contextlib import contextmanager
from typing import Iterator

from psycopg.types.json import Jsonb

from .db import connect


@contextmanager
def tracked_run(job: str, scope: str | None = None) -> Iterator[dict]:
    stats: dict = {}
    with connect() as c:
        run_id = c.execute(
            "INSERT INTO sync_runs (job, scope) VALUES (%s, %s) RETURNING id", (job, scope)
        ).fetchone()["id"]
        c.commit()
    try:
        yield stats
    except BaseException as e:
        with connect() as c:
            c.execute(
                "UPDATE sync_runs SET finished_at = now(), status = 'error', stats = %s,"
                " error = %s WHERE id = %s",
                (Jsonb(stats), "".join(traceback.format_exception(e))[-4000:], run_id),
            )
        raise
    else:
        with connect() as c:
            c.execute(
                "UPDATE sync_runs SET finished_at = now(), status = 'ok', stats = %s"
                " WHERE id = %s",
                (Jsonb(stats), run_id),
            )
