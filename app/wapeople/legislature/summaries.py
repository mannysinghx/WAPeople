"""Plain-language bill summaries from the Legislature's nonpartisan staff documents.

Every bill that gets a hearing has staff Bill Analyses / Bill Reports; older sessions also
have short Digests. Reports from about 2003 on carry a "Brief Summary of Bill" section
(1-4 bullets); older reports and Final Bill Reports have a longer "Summary:" section, of
which we keep the first sentences. Bills never heard have no staff document, and the site
falls back to the Legislature's one-line description.
"""
from __future__ import annotations

import html
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import quote

import httpx
import psycopg

from ..config import USER_AGENT
from .client import LegClient
from .parse import TYPE_BY_PREFIX, split_bill_id, version_modifiers

log = logging.getLogger(__name__)

# ---- Text extraction (pure; unit tested) -------------------------------------------

_BULLET_CHARS = "•·▪●◦"
_BRIEF_RE = re.compile(r"Brief\s+Summary\s+of\s+(?:[A-Za-z]+\s+){0,5}?(?:Bill|Amendment|Measure)\b\s*:?", re.I)
_SUMMARY_HEAD_RE = re.compile(
    r"^\s*(?:Summary(?:\s+of\s+(?:[A-Za-z]+\s+){0,5}?(?:Bill|Amendment))?(?:\s+as\s+\w+(?:\s+\w+)?)?|SUMMARY(?:\s+OF\s+[A-Z ]+)?)\s*:\s*",
    re.M,
)
_STOP_RE = re.compile(
    r"^\s*(?:hearing\s+dates?|staff\b|background\b|(?:house|senate)\s+committee\s+on|majority\s+report|"
    r"minority\s+report|brief\s+history|committee\s+activity|summary\s+of|summary\s*:|appropriation|"
    r"fiscal\s+note|effective\s+date|votes\s+on\s+final\s+passage|bill\s+request|title\s*:|sponsors?\b|"
    r"as\s+reported|as\s+passed|staff\s+summary|testimony|persons\s+testifying|rules\s+authority|"
    r"rulemaking|partial\s+veto|other\s+committee|(?:house|senate)\s+amendments?|"
    r"(?:amended|substitute|striking)\s+.*compared|pro\s*:|con\s*:|revised\s+for)",
    re.I,
)


def html_to_text(raw: bytes) -> str:
    """Decode (UTF-8, else Windows-1252 as used by older documents) and strip markup."""
    try:
        s = raw.decode("utf-8")
    except UnicodeDecodeError:
        s = raw.decode("cp1252", errors="replace")
    s = re.sub(r"(?is)<(script|style|head)\b.*?</\1>", " ", s)
    s = re.sub(r"(?i)<br\s*/?>|</p>|</li>|</div>|</tr>|</h\d>|</td>", "\n", s)
    s = re.sub(r"(?i)<li[^>]*>", "\n• ", s)
    s = html.unescape(re.sub(r"<[^>]+>", " ", s))
    s = s.replace("\xa0", " ").replace("‑", "-").replace("�", " ")
    s = re.sub(r"[ \t\r\f\v]+", " ", s)
    return re.sub(r"\n\s*\n+", "\n", s).strip()


def _clean(s: str) -> str:
    return " ".join(s.split()).strip(" " + _BULLET_CHARS)


def _bullets(lines: list[str]) -> list[str]:
    """Group wrapped lines into bullets. Lines starting with a bullet mark begin a new
    bullet; otherwise a line begins a new bullet only if the previous one ended a sentence."""
    has_marks = any(l.lstrip()[:1] in _BULLET_CHARS for l in lines if l.strip())
    out: list[str] = []
    for line in lines:
        if not line.strip():
            continue
        starts_new = (line.lstrip()[:1] in _BULLET_CHARS) if has_marks else (
            not out or out[-1].rstrip().endswith((".", ";", ":", ")"))
        )
        text = _clean(line)
        if not text:
            continue
        if starts_new or not out:
            out.append(text)
        else:
            out[-1] = f"{out[-1]} {text}"
    # Drop version labels such as "(As Amended by House)" that sit on their own line.
    return [b for b in out if len(b) > 12 and not re.fullmatch(r"\(As [^)]*\)", b)]


def extract_brief_summary(text: str) -> list[str] | None:
    m = _BRIEF_RE.search(text)
    if not m:
        return None
    rest = text[m.end():]
    lines: list[str] = []
    for line in rest.split("\n"):
        if lines and _STOP_RE.match(line):
            break
        if not lines and _STOP_RE.match(line) and not line.strip().startswith(tuple(_BULLET_CHARS)):
            break
        lines.append(line)
        if len(lines) > 40:
            break
    bullets = _bullets(lines)
    return bullets[:6] or None


_SENTENCE_END = re.compile(r"(?<=[a-z0-9\)\]][.!?])\s+(?=[A-Z(])")


def first_sentences(paragraph: str, max_sentences: int = 2, max_chars: int = 420) -> str:
    sentences = _SENTENCE_END.split(_clean(paragraph))
    out = ""
    for s in sentences[:max_sentences]:
        if out and len(out) + len(s) + 1 > max_chars:
            break
        out = f"{out} {s}".strip()
    if len(out) > max_chars:
        out = out[: max_chars - 1].rsplit(" ", 1)[0] + "…"
    return out


def extract_summary_section(text: str) -> str | None:
    m = _SUMMARY_HEAD_RE.search(text)
    if not m:
        return None
    lines: list[str] = []
    for line in text[m.end():].split("\n"):
        if lines and _STOP_RE.match(line):
            break
        lines.append(line)
        if sum(len(l) for l in lines) > 1500:
            break
    body = " ".join(_clean(l) for l in lines if _clean(l))
    # Drop a leading subheading such as "Rent Increases." that precedes the first sentence.
    body = re.sub(r"^(?:[A-Z][\w&,'/-]*(?:\s+[A-Z&][\w&,'/-]*){0,6}\.)\s+(?=[A-Z])", "", body)
    out = first_sentences(body)
    return out if len(out) > 25 else None


def extract_digest(text: str) -> str | None:
    m = re.search(r"DIGEST\s*", text)
    if not m:
        return None
    body = re.sub(r"^(?:[A-Z0-9 ]+-\s*DIGEST\s*)+", "", text[m.end():].strip())
    out = first_sentences(body, max_sentences=3)
    return out if len(out) > 20 else None


# ---- Choosing a document -------------------------------------------------------------

def _kind_tier(doc: dict) -> int:
    name = (doc.get("Name") or "").upper()
    friendly = (doc.get("ShortFriendlyName") or "").lower()
    if doc.get("Class") == "Digests" or name.endswith(".DIG"):
        return 3
    if "historical" in friendly or " BRH " in f" {name} ":
        return 2
    if "FBR" in name or "final bill report" in friendly:
        return 1
    return 0


_DOC_VERSION_RE = re.compile(r"^\d+(?P<s>-S(?P<sn>\d?))?(?P<e>\.E(?P<en>\d?))?(?=[ .]|$)")


def doc_version(name: str) -> tuple[int, int]:
    """(substitute, engrossed) version a document covers, from its name: '1217-S2 HBR APP 25'
    -> (2, 0), '1296-S.E HBR PL 25' -> (1, 1). The service's BillId tag can't be used: it is
    often the bill's current id on every document."""
    m = _DOC_VERSION_RE.match(name or "")
    if not m:
        return (0, 0)
    sub = int(m["sn"] or 1) if m["s"] else 0
    eng = int(m["en"] or 1) if m["e"] else 0
    return (sub, eng)


def version_id(base_prefix: str, number: int, version: tuple[int, int]) -> str:
    """(2, 1) + 'HB' 1217 -> 'E2SHB 1217'."""
    sub, eng = version
    mods = (f"{eng}E" if eng > 1 else "E" if eng else "") + (f"{sub}S" if sub > 1 else "S" if sub else "")
    return f"{mods}{base_prefix} {number}"


def rank_candidates(docs: list[dict], current_version: str | None = None) -> list[dict]:
    """Best first: modern reports, then final reports, historical reports, digests.
    Within a tier, reports on the bill's live version (the one that advanced or became
    law) come first, then later versions, then the most recently modified document."""
    live = version_modifiers(current_version) if current_version else None
    return sorted(
        docs,
        key=lambda d: (_kind_tier(d),
                       0 if live is not None and doc_version(d.get("Name") or "") == live else 1,
                       tuple(-x for x in doc_version(d.get("Name") or "")),
                       _neg_date(d.get("HtmLastModifiedDate"))),
    )


def _neg_date(s: str | None) -> float:
    try:
        return -datetime.fromisoformat((s or "")[:19]).timestamp()
    except ValueError:
        return 0.0


def _source_label(doc: dict | None, bill: dict) -> str | None:
    """e.g. 'House Bill Report (2SHB 1217)'."""
    if not doc:
        return None
    kind = doc.get("ShortFriendlyName") or doc.get("Class") or "staff report"
    base = (bill.get("bill_id") or "").split(" ")[0]
    if not base:
        return kind
    return f"{kind} ({version_id(base, bill['bill_number'], doc_version(doc.get('Name') or ''))})"


def summarize(text: str, doc: dict) -> str | None:
    """Summary text (bullets separated by newlines) from one document, or None."""
    if _kind_tier(doc) == 3:
        return extract_digest(text)
    bullets = extract_brief_summary(text)
    if bullets:
        return "\n".join(bullets)
    return extract_summary_section(text)


# ---- Sync ---------------------------------------------------------------------------

class SummarySync:
    def __init__(self, conn: psycopg.Connection, client: LegClient | None = None, workers: int = 4):
        self.conn = conn
        self.client = client or LegClient()
        self.workers = workers
        self.http = httpx.Client(timeout=60, headers={"User-Agent": USER_AGENT}, follow_redirects=True)

    def close(self) -> None:
        self.http.close()

    def _docs_by_bill(self, biennium: str) -> dict[tuple[int, str], list[dict]]:
        out: dict[tuple[int, str], list[dict]] = {}
        for cls in ("Bill Reports", "Digests"):
            for d in self.client.records("LegislativeDocumentService", "GetAllDocumentsByClass",
                                         "LegislativeDocument", biennium=biennium, documentClass=cls):
                parts = split_bill_id(d.get("BillId") or "")
                if not parts or not d.get("HtmUrl"):
                    continue
                d["Class"] = cls
                out.setdefault((parts[1], TYPE_BY_PREFIX[parts[0]]), []).append(d)
        return out

    def _fetch(self, url: str) -> bytes | None:
        url = url.replace("http://", "https://", 1)
        url = quote(url, safe=":/.%-_~")
        for attempt in range(3):
            try:
                r = self.http.get(url)
                if r.status_code == 200:
                    return r.content
                if r.status_code == 404:
                    return None
            except httpx.HTTPError:
                pass
            time.sleep(2 * (attempt + 1))
        return None

    def _best_summary(self, candidates: list[dict]) -> tuple[str | None, dict | None]:
        """Try up to three documents; prefer one with a brief summary."""
        fallback: tuple[str | None, dict | None] = (None, None)
        for doc in candidates[:3]:
            raw = self._fetch(doc["HtmUrl"])
            if not raw:
                continue
            text = html_to_text(raw)
            if _kind_tier(doc) < 3:
                bullets = extract_brief_summary(text)
                if bullets:
                    return "\n".join(bullets), doc
            s = summarize(text, doc)
            if s and fallback[0] is None:
                fallback = (s, doc)
        if fallback[0] is None:
            digests = [d for d in candidates[3:] if _kind_tier(d) == 3][:1]
            for doc in digests:
                raw = self._fetch(doc["HtmUrl"])
                if raw:
                    s = extract_digest(html_to_text(raw))
                    if s:
                        return s, doc
        return fallback

    def sync(self, biennium: str, force: bool = False) -> dict:
        t0 = time.time()
        docs = self._docs_by_bill(biennium)
        bills = self.conn.execute(
            "SELECT id, bill_number, bill_id, type_code, current_version, summary_doc FROM bills WHERE biennium = %s",
            (biennium,),
        ).fetchall()
        self.conn.commit()
        todo = []
        for b in bills:
            cands = rank_candidates(docs.get((b["bill_number"], b["type_code"]), []), b["current_version"])
            if not cands:
                continue
            if not force and b["summary_doc"] == cands[0]["Name"]:
                continue  # best document unchanged since last time
            todo.append((b, cands))
        found = 0
        with ThreadPoolExecutor(self.workers) as ex:
            futs = {ex.submit(self._best_summary, cands): (b, cands) for b, cands in todo}
            for i, fut in enumerate(as_completed(futs), 1):
                b, cands = futs[fut]
                try:
                    text, doc = fut.result()
                except Exception as e:
                    log.warning("summary %s %s failed: %s", biennium, b["bill_number"], e)
                    continue
                # Remember the top candidate even when it had no usable text, so it isn't
                # re-fetched daily; a newer report changes the top candidate and retriggers.
                self.conn.execute(
                    "UPDATE bills SET summary = %s, summary_source = %s, summary_doc = %s, summary_url = %s,"
                    " summary_doc_date = %s, summary_checked_at = now() WHERE id = %s",
                    (text, _source_label(doc, b),
                     cands[0]["Name"], ((doc or {}).get("HtmUrl") or "").replace("http://", "https://", 1) or None,
                     ((doc or {}).get("HtmLastModifiedDate") or None), b["id"]),
                )
                found += bool(text)
                if i % 250 == 0:
                    self.conn.commit()
                    log.info("%s: summaries %d/%d (%.0fs)", biennium, i, len(todo), time.time() - t0)
        self.conn.commit()
        log.info("%s: %d summaries from %d documents checked (%.0fs)", biennium, found, len(todo), time.time() - t0)
        return {"summaries_checked": len(todo), "summaries_found": found}
