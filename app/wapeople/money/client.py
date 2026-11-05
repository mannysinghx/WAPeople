"""Client for the Public Disclosure Commission's open data on data.wa.gov (Socrata SODA API)."""
from __future__ import annotations

import logging
import time
from typing import Iterable, Iterator

import httpx

from ..config import PDC_BASE, SOCRATA_APP_TOKEN, USER_AGENT

log = logging.getLogger(__name__)

# Dataset ids. Descriptions: https://www.pdc.wa.gov/political-disclosure-reporting-data/open-data
SUMMARY = "3h9x-7bvm"           # one row per campaign per election year
CONTRIBUTIONS = "kv7h-kjye"
EXPENDITURES = "tijg-9zyp"
LOANS = "d2ig-r3q4"
DEBTS = "3r6b-hsaa"
PLEDGES = "8bva-rkeb"
OUTSIDE = "67cp-h962"           # independent expenditures and electioneering (form C-6)
SURPLUS = "3cbn-54c3"           # latest report for each surplus funds account
SURPLUS_SPENDING = "ti55-mvy5"
DISCLOSURES = "ehbc-shxw"       # personal financial affairs statements (formerly F-1)


def quoted(values: Iterable) -> str:
    """A SoQL value list: 'a','b'. Values are escaped by doubling single quotes."""
    return ",".join("'" + str(v).replace("'", "''") + "'" for v in values)


def chunks(items: list, size: int) -> Iterator[list]:
    for i in range(0, len(items), size):
        yield items[i:i + size]


class PDC:
    PAGE_SIZE = 20000

    def __init__(self, token: str | None = SOCRATA_APP_TOKEN):
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        if token:
            headers["X-App-Token"] = token
        self.http = httpx.Client(timeout=300, headers=headers, follow_redirects=True)

    def close(self) -> None:
        self.http.close()

    def get(self, dataset: str, **params) -> list[dict]:
        """One SODA request. Keyword names are SoQL parameters without the '$'."""
        query = {f"${k}": v for k, v in params.items()}
        backoff = 5.0
        for attempt in range(8):
            try:
                r = self.http.get(f"{PDC_BASE}/{dataset}.json", params=query)
            except httpx.HTTPError as e:
                log.warning("data.wa.gov request failed (%s); retry in %.0fs", e, backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, 300)
                continue
            if r.status_code == 200:
                return r.json()
            if r.status_code in (429, 500, 502, 503, 504):
                log.info("data.wa.gov HTTP %s; retry in %.0fs", r.status_code, backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, 300)
                continue
            raise RuntimeError(f"data.wa.gov HTTP {r.status_code}: {r.text[:300]}")
        raise RuntimeError("data.wa.gov: too many retries")

    def rows(self, dataset: str, where: str, select: str, order: str = "id") -> Iterator[dict]:
        """Every row matching a filter, one page at a time."""
        offset = 0
        while True:
            page = self.get(dataset, where=where, select=select, order=order,
                            limit=self.PAGE_SIZE, offset=offset)
            yield from page
            if len(page) < self.PAGE_SIZE:
                return
            offset += len(page)
