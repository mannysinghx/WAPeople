"""Client for the Washington State Legislature web services.

https://wslwebservices.leg.wa.gov/ exposes ASMX (SOAP) services; every operation also
answers a plain HTTP GET with query-string parameters and returns namespaced XML.
"""
from __future__ import annotations

import logging
import tempfile
import time
from typing import Any, Iterator

import httpx
from defusedxml.ElementTree import fromstring, iterparse

from ..config import LEG_BASE, USER_AGENT

log = logging.getLogger(__name__)
NS = "{http://WSLWebServices.leg.wa.gov/}"


def _tag(el) -> str:
    t = el.tag
    return t[len(NS):] if t.startswith(NS) else t


def element_to_dict(el) -> Any:
    """Convert an element to plain Python: leaf -> str, repeated children -> list."""
    children = list(el)
    if not children:
        return (el.text or "").strip()
    out: dict[str, Any] = {}
    for c in children:
        k = _tag(c)
        v = element_to_dict(c)
        if k in out:
            if not isinstance(out[k], list):
                out[k] = [out[k]]
            out[k].append(v)
        else:
            out[k] = v
    return out


def as_list(v: Any) -> list:
    """Normalize a field that may be missing, a single item, or a list of items."""
    if v is None or v == "":
        return []
    return v if isinstance(v, list) else [v]


class LegClient:
    def __init__(self, timeout: float = 120.0, retries: int = 5):
        self.http = httpx.Client(
            timeout=timeout, headers={"User-Agent": USER_AGENT}, follow_redirects=True
        )
        self.retries = retries

    def close(self) -> None:
        self.http.close()

    @staticmethod
    def _url(service: str, op: str) -> str:
        return f"{LEG_BASE}/{service}.asmx/{op}"

    def fetch(self, service: str, op: str, **params) -> bytes:
        err: Exception | None = None
        for attempt in range(self.retries):
            try:
                r = self.http.get(self._url(service, op), params=params)
                if r.status_code == 200:
                    return r.content
                err = RuntimeError(f"{op} HTTP {r.status_code}: {r.text[:200]!r}")
            except httpx.HTTPError as e:
                err = e
            wait = min(60, 2 ** attempt)
            log.warning("%s %s failed (%s); retry in %ss", op, params, err, wait)
            time.sleep(wait)
        assert err is not None
        raise err

    def records(self, service: str, op: str, tag: str, **params) -> list[dict]:
        """Top-level <tag> records of a response, as dicts."""
        root = fromstring(self.fetch(service, op, **params))
        return [element_to_dict(el) for el in root if _tag(el) == tag]

    def stream_records(self, service: str, op: str, tag: str, **params) -> Iterator[dict]:
        """Like records(), for very large responses: spool to disk, then iterparse."""
        err: Exception | None = None
        for attempt in range(self.retries):
            fh = tempfile.TemporaryFile()
            try:
                with self.http.stream(
                    "GET", self._url(service, op), params=params, timeout=900
                ) as r:
                    r.raise_for_status()
                    for chunk in r.iter_bytes(1 << 20):
                        fh.write(chunk)
                break
            except httpx.HTTPError as e:
                fh.close()
                err = e
                wait = min(120, 5 * 2 ** attempt)
                log.warning("%s stream failed (%s); retry in %ss", op, e, wait)
                time.sleep(wait)
        else:
            assert err is not None
            raise err
        with fh:
            fh.seek(0)
            depth = 0
            for event, el in iterparse(fh, events=("start", "end")):
                if event == "start":
                    depth += 1
                    continue
                depth -= 1
                if depth == 1 and _tag(el) == tag:
                    yield element_to_dict(el)
                    el.clear()

    # ---- Convenience wrappers -------------------------------------------------

    def members(self, biennium: str) -> list[dict]:
        return self.records("SponsorService", "GetSponsors", "Member", biennium=biennium)

    def legislation_by_year(self, year: int) -> list[dict]:
        return self.records(
            "LegislationService", "GetLegislationByYear", "LegislationInfo", year=year
        )

    def legislation_introduced_since(self, since: str) -> Iterator[dict]:
        return self.stream_records(
            "LegislationService", "GetLegislationIntroducedSince", "Legislation",
            sinceDate=since,
        )

    def legislation(self, biennium: str, bill_number: int) -> list[dict]:
        return self.records(
            "LegislationService", "GetLegislation", "Legislation",
            biennium=biennium, billNumber=bill_number,
        )

    def prefiled(self) -> list[dict]:
        return self.records("LegislationService", "GetPrefiledLegislation", "Legislation")

    def status_changes(self, biennium: str, begin: str, end: str) -> Iterator[dict]:
        return self.stream_records(
            "LegislationService", "GetLegislativeStatusChangesByDateRange",
            "LegislativeStatus", biennium=biennium, beginDate=begin, endDate=end,
        )

    def passed(self, op: str, biennium: str) -> list[dict]:
        # op: GetLegislationPassedHouse | GetLegislationPassedSenate | GetLegislationPassedLegislature
        return self.records("LegislationService", op, "LegislationInfo", biennium=biennium)

    def sponsors(self, biennium: str, bill_id: str) -> list[dict]:
        return self.records(
            "LegislationService", "GetSponsors", "Sponsor", biennium=biennium, billId=bill_id
        )

    def roll_calls(self, biennium: str, bill_number: int) -> list[dict]:
        return self.records(
            "LegislationService", "GetRollCalls", "RollCall",
            biennium=biennium, billNumber=bill_number,
        )
