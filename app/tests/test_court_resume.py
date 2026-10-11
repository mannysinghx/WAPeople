"""Integration test for resumable court collection.

Runs only against a throwaway database: set TEST_DATABASE_URL (it is migrated, then all
court tables are emptied). Never point it at the real database.
"""
import os

import pytest

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="TEST_DATABASE_URL not set")


def hit(cluster_id, name, date, docket):
    return {"cluster_id": cluster_id, "caseName": name, "dateFiled": date, "docketNumber": docket,
            "citation": [], "citeCount": 0, "absolute_url": f"/opinion/{cluster_id}/x/",
            "opinions": [{"id": cluster_id * 10, "snippet": "the <mark>House Bill</mark> 1234 (2019)",
                          "download_url": None}]}


class FakeCL:
    """Two pages per category; raises RateLimited on the 3rd request of the first run."""

    has_token = False

    def __init__(self, fail_after=None):
        self.calls = []
        self.fail_after = fail_after

    def search_page(self, query, filed_after=None, url=None, court="wash"):
        from wapeople.courts.client import RateLimited

        self.calls.append(url)
        if self.fail_after is not None and len(self.calls) > self.fail_after:
            raise RateLimited(80000)
        base = abs(hash(query)) % 1000 * 100
        if url is None:
            return [hit(base + 1, f"A {base}", "2020-01-02", f"{base}-1")], f"cursor://{base}/2"
        return [hit(base + 2, f"B {base}", "2021-01-02", f"{base}-2")], None

    def close(self):
        pass


@pytest.fixture()
def conn():
    os.environ["DATABASE_URL"] = TEST_DB
    from wapeople import db, migrate

    db.DATABASE_URL = TEST_DB
    migrate.connect = lambda: __import__("psycopg").connect(TEST_DB, row_factory=__import__("psycopg").rows.dict_row)
    migrate.migrate()
    c = migrate.connect()
    c.execute("TRUNCATE court_cases, court_sync_state, court_rate_limit CASCADE")
    c.commit()
    yield c
    c.close()


def test_collect_resumes_after_rate_limit(conn):
    from wapeople.courts.client import RateLimited
    from wapeople.courts.sync import CATEGORIES, CourtSync

    cs = CourtSync(conn, cl=FakeCL(fail_after=3))
    with pytest.raises(RateLimited):
        cs.collect()
    # Category 1 finished (2 pages), category 2 got 1 page before the limit.
    states = conn.execute("SELECT category, pages, next_url, completed_at FROM court_sync_state ORDER BY started_at").fetchall()
    assert states[0]["completed_at"] is not None and states[0]["pages"] == 2
    assert states[1]["completed_at"] is None and states[1]["next_url"].startswith("cursor://")
    assert cs.retry_at() is not None
    assert conn.execute("SELECT count(*) AS n FROM court_cases").fetchone()["n"] == 3
    assert cs.backfill_pending()

    # Next run picks up category 2 at its saved cursor and finishes everything.
    fake = FakeCL()
    cs2 = CourtSync(conn, cl=fake)
    cs2.collect()
    assert fake.calls[0] == states[1]["next_url"]
    assert not cs2.backfill_pending()
    assert cs2.retry_at() is None
    assert conn.execute("SELECT count(*) AS n FROM court_cases").fetchone()["n"] == 2 * len(CATEGORIES)
