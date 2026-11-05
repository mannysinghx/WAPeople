"""Read the Federal Election Commission's public bulk data files.

The files need no API key and have no rate limit, and their transaction files have no street
address columns. They're large (indiv24.zip is 4 GB, 10 GB of text inside), so nothing is saved
to disk: SeekableHTTP lets zipfile read one member straight off the network, and an interrupted
transfer picks up where it stopped.
"""
from __future__ import annotations

import csv
import io
import logging
import time
import zipfile
from dataclasses import dataclass
from typing import Iterable, Iterator

import httpx

from ..config import USER_AGENT

log = logging.getLogger(__name__)

BULK_BASE = "https://www.fec.gov/files/bulk-downloads"
CHUNK = 1 << 20


@dataclass
class FileInfo:
    url: str
    size: int
    etag: str | None
    last_modified: str | None


class SeekableHTTP:
    """A read-only, seekable file over HTTP range requests, streaming between seeks."""

    def __init__(self, client: httpx.Client, info: FileInfo, retries: int = 8):
        self.client = client
        self.info = info
        self.retries = retries
        self.pos = 0
        self.transferred = 0
        self._response: httpx.Response | None = None
        self._chunks: Iterator[bytes] | None = None
        self._buffer = bytearray()
        self._buffer_pos = 0            # file position of self._buffer[0]

    def seekable(self) -> bool:
        return True

    def readable(self) -> bool:
        return True

    def tell(self) -> int:
        return self.pos

    def seek(self, offset: int, whence: int = 0) -> int:
        self.pos = {0: offset, 1: self.pos + offset, 2: self.info.size + offset}[whence]
        return self.pos

    def close(self) -> None:
        self._drop_stream()

    def _drop_stream(self) -> None:
        if self._response is not None:
            self._response.close()
        self._response = self._chunks = None
        self._buffer = bytearray()

    def _open_stream(self) -> None:
        self._drop_stream()
        request = self.client.build_request("GET", self.info.url, headers={"Range": f"bytes={self.pos}-"})
        response = self.client.send(request, stream=True)
        if response.status_code != 206:
            response.close()
            raise httpx.HTTPStatusError(f"expected 206, got {response.status_code}",
                                        request=request, response=response)
        self._response, self._chunks = response, response.iter_raw(CHUNK)
        self._buffer_pos = self.pos

    def read(self, n: int = -1) -> bytes:
        if n is None or n < 0:
            n = self.info.size - self.pos
        n = min(n, self.info.size - self.pos)
        if n <= 0:
            return b""
        backoff = 5.0
        for attempt in range(self.retries):
            try:
                in_buffer = self._buffer_pos <= self.pos <= self._buffer_pos + len(self._buffer)
                if self._chunks is None or not in_buffer:
                    self._open_stream()
                while self._buffer_pos + len(self._buffer) < self.pos + n:
                    chunk = next(self._chunks, None)
                    if chunk is None:
                        break
                    # Keep only what hasn't been read yet, so the buffer stays small.
                    drop = self.pos - self._buffer_pos
                    if drop > 0:
                        del self._buffer[:drop]
                        self._buffer_pos = self.pos
                    self._buffer += chunk
                    self.transferred += len(chunk)
                start = self.pos - self._buffer_pos
                data = bytes(self._buffer[start:start + n])
                if not data:
                    raise httpx.ReadError("connection ended early")
                self.pos += len(data)
                return data
            except httpx.HTTPError as e:
                if attempt == self.retries - 1:
                    raise
                log.warning("FEC transfer interrupted at %d (%s); resuming in %.0fs", self.pos, e, backoff)
                self._drop_stream()
                time.sleep(backoff)
                backoff = min(backoff * 2, 300)
        raise RuntimeError("unreachable")


class FecBulk:
    def __init__(self, client: httpx.Client | None = None):
        self.http = client or httpx.Client(timeout=httpx.Timeout(120, read=300), follow_redirects=True,
                                           headers={"User-Agent": USER_AGENT})

    def close(self) -> None:
        self.http.close()

    @staticmethod
    def zip_name(prefix: str, cycle: int) -> str:
        return f"{cycle}/{prefix}{str(cycle)[2:]}.zip"

    @staticmethod
    def ie_name(cycle: int) -> str:
        return f"{cycle}/independent_expenditure_{cycle}.csv"

    def info(self, name: str) -> FileInfo | None:
        """Size and version of a file, or None if the FEC doesn't publish it for that cycle."""
        response = self.http.head(f"{BULK_BASE}/{name}")
        if response.status_code in (403, 404):
            return None
        response.raise_for_status()
        return FileInfo(str(response.url), int(response.headers.get("content-length") or 0),
                        response.headers.get("etag"), response.headers.get("last-modified"))

    def zip_lines(self, name: str, info: FileInfo | None = None) -> Iterator[bytes]:
        """Raw lines of the data member of a zip. Zips of individual gifts also hold by_date/ copies
        of the same rows; only the one full file is read, so nothing is counted twice."""
        info = info or self.info(name)
        if info is None:
            return
        source = SeekableHTTP(self.http, info)
        try:
            with zipfile.ZipFile(source) as z:
                members = [m for m in z.infolist() if not m.filename.startswith("by_date/") and not m.is_dir()]
                for member in members:
                    with z.open(member) as raw:
                        yield from io.BufferedReader(raw, CHUNK)
        finally:
            source.close()

    def rows(self, name: str, wanted: frozenset[bytes] | None = None) -> Iterator[list[str]]:
        """Pipe-separated rows. `wanted` keeps only rows whose first field (9 characters: a committee
        or candidate id) is listed, which skips decoding the tens of millions of rows we don't need."""
        for line in self.zip_lines(name):
            if wanted is None or line[:9] in wanted:
                yield line.decode("utf-8", "replace").rstrip("\r\n").split("|")

    def csv_rows(self, name: str) -> Iterator[dict]:
        info = self.info(name)
        if info is None:
            return
        with self.http.stream("GET", info.url) as response:
            response.raise_for_status()
            yield from csv.DictReader(_decoded_lines(response.iter_lines()))


def _decoded_lines(lines: Iterable[str]) -> Iterator[str]:
    for line in lines:
        yield line.replace("\x00", "")
