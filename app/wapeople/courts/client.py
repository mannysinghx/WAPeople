"""Polite clients for CourtListener (Free Law Project) and official opinion PDFs."""
from __future__ import annotations

import io
import logging
import re
import threading
import time
from html import unescape
from typing import Iterator

import httpx

from ..config import COURTLISTENER_BASE, COURTLISTENER_TOKEN, USER_AGENT

log = logging.getLogger(__name__)


class Throttle:
    def __init__(self, min_interval: float):
        self.min_interval = min_interval
        self._last = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            delay = self._last + self.min_interval - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            self._last = time.monotonic()


class CourtListener:
    """CourtListener REST v4. Anonymous use is allowed for search but heavily rate limited."""

    def __init__(self, token: str | None = COURTLISTENER_TOKEN):
        headers = {"User-Agent": USER_AGENT}
        if token:
            headers["Authorization"] = f"Token {token}"
        self.has_token = bool(token)
        self.http = httpx.Client(timeout=90, headers=headers, follow_redirects=True)
        # Anonymous access is limited to 50 requests/hour; a free token allows 5,000/hour.
        self.throttle = Throttle(1.0 if token else 75.0)

    def close(self) -> None:
        self.http.close()

    def get_json(self, url: str, params: dict | None = None) -> dict:
        backoff = 30.0
        for attempt in range(8):
            self.throttle.wait()
            try:
                r = self.http.get(url, params=params)
            except httpx.HTTPError as e:
                log.warning("CourtListener request failed (%s); retry in %.0fs", e, backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, 600)
                continue
            if r.status_code == 200:
                return r.json()
            if r.status_code in (429, 500, 502, 503, 504):
                retry_after = r.headers.get("Retry-After", "")
                wait = float(retry_after) if retry_after.isdigit() else backoff
                log.info("CourtListener HTTP %s; waiting %.0fs", r.status_code, wait)
                time.sleep(wait)
                backoff = min(backoff * 2, 600)
                continue
            raise RuntimeError(f"CourtListener HTTP {r.status_code}: {r.text[:300]}")
        raise RuntimeError("CourtListener: too many retries")

    def search(self, query: str, court: str = "wash", filed_after: str | None = None) -> Iterator[dict]:
        """All opinion-cluster hits for a query, newest first, following cursor pagination."""
        params: dict | None = {
            "type": "o",
            "court": court,
            "q": query,
            "highlight": "on",
            "order_by": "dateFiled desc",
        }
        if filed_after:
            params["filed_after"] = filed_after
        url: str | None = f"{COURTLISTENER_BASE}/search/"
        while url:
            data = self.get_json(url, params)
            yield from data.get("results", [])
            url = data.get("next")
            params = None  # the "next" URL already carries every parameter

    def opinion_text(self, opinion_id: int) -> str | None:
        """Full opinion text (needs an API token)."""
        if not self.has_token:
            return None
        data = self.get_json(
            f"{COURTLISTENER_BASE}/opinions/{opinion_id}/",
            {"fields": "plain_text,html_with_citations,html,html_lawbox,html_columbia,xml_harvard"},
        )
        if (data.get("plain_text") or "").strip():
            return data["plain_text"]
        for field in ("html_with_citations", "html", "html_lawbox", "html_columbia", "xml_harvard"):
            if (data.get(field) or "").strip():
                return html_to_text(data[field])
        return None


class PdfFetcher:
    """Downloads official opinion PDFs (courts.wa.gov) and extracts their text."""

    def __init__(self):
        self.http = httpx.Client(timeout=120, headers={"User-Agent": USER_AGENT}, follow_redirects=True)
        self.throttle = Throttle(1.5)

    def close(self) -> None:
        self.http.close()

    def text(self, url: str) -> str | None:
        from pypdf import PdfReader  # imported lazily: only the sync job needs it

        for attempt in range(3):
            self.throttle.wait()
            try:
                r = self.http.get(url)
            except httpx.HTTPError as e:
                log.warning("PDF %s failed: %s", url, e)
                time.sleep(10 * (attempt + 1))
                continue
            if r.status_code == 404:
                return None
            if r.status_code != 200 or not r.content.startswith(b"%PDF"):
                time.sleep(10 * (attempt + 1))
                continue
            try:
                reader = PdfReader(io.BytesIO(r.content))
                return "\n".join((page.extract_text() or "") for page in reader.pages)
            except Exception as e:  # malformed PDF
                log.warning("PDF parse failed for %s: %s", url, e)
                return None
        return None


_TAG_RE = re.compile(r"<[^>]+>")


def html_to_text(s: str) -> str:
    s = re.sub(r"(?i)<br\s*/?>|</p>|</div>", "\n", s)
    return unescape(_TAG_RE.sub("", s))
