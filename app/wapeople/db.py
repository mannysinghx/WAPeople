from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .config import DATABASE_URL

_pool: ConnectionPool | None = None


def pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            DATABASE_URL,
            min_size=1,
            max_size=10,
            kwargs={"row_factory": dict_row, "autocommit": False},
            open=True,
        )
    return _pool


@contextmanager
def conn() -> Iterator[psycopg.Connection]:
    """Pooled connection; commits on success, rolls back on error."""
    with pool().connection() as c:
        yield c


def connect() -> psycopg.Connection:
    """A dedicated (non-pooled) connection, used by long-running sync jobs."""
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)
