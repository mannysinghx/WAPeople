"""Regression tests for bugs found on the live site in November 2026."""
from datetime import datetime, timezone

import httpx
import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from wapeople import config, contact
from wapeople.courts.client import AuthRequired, CourtListener
from wapeople.legislature.sync import LegislatureSync, is_placeholder_member
from wapeople.web.middleware import CanonicalHostMiddleware, HeadMiddleware


# ---- HEAD answered 405 on every page ------------------------------------------------------

def head_client():
    async def page(request):
        return PlainTextResponse("hello", headers={"X-Marker": "yes"})
    app = Starlette(routes=[Route("/", page, methods=["GET"])])
    app.add_middleware(HeadMiddleware)
    return TestClient(app)


def test_head_gets_the_same_headers_and_no_body():
    r = head_client().head("/")
    assert r.status_code == 200
    assert r.headers["x-marker"] == "yes"
    assert r.content == b""


def test_get_is_untouched_by_head_middleware():
    assert head_client().get("/").text == "hello"


def test_post_still_rejected_where_not_allowed():
    assert head_client().post("/").status_code == 405


# ---- One page, one address -----------------------------------------------------------------

def host_client(site="https://wapeople.org"):
    async def page(request):
        return PlainTextResponse("ok")
    app = Starlette(routes=[Route("/{p:path}", page, methods=["GET", "POST"])])
    app.add_middleware(CanonicalHostMiddleware, site_url=site)
    return TestClient(app, follow_redirects=False)


@pytest.mark.parametrize("host", ["www.wapeople.org", "wapeople-production.up.railway.app"])
def test_other_hosts_redirect_with_path_and_query(host):
    r = host_client().get("/bills?q=rent", headers={"host": host})
    assert r.status_code == 301
    assert r.headers["location"] == "https://wapeople.org/bills?q=rent"


@pytest.mark.parametrize("host", ["wapeople.org", "wapeople.org:443", "healthcheck.railway.app", "localhost:8095"])
def test_canonical_and_unknown_hosts_are_served(host):
    assert host_client().get("/", headers={"host": host}).status_code == 200


def test_health_check_is_never_redirected():
    assert host_client().get("/healthz", headers={"host": "wapeople-production.up.railway.app"}).status_code == 200


def test_form_posts_are_never_redirected():
    assert host_client().post("/contact", headers={"host": "www.wapeople.org"}).status_code == 200


def test_no_site_url_means_no_redirects():
    assert host_client(site="").get("/", headers={"host": "www.wapeople.org"}).status_code == 200


# ---- Real legislators were being renamed "Senator" ------------------------------------------

def test_placeholder_roster_rows_are_recognized():
    assert is_placeholder_member({"Id": "15814", "Name": "", "LongName": "Senator"})
    assert is_placeholder_member({"Id": "15814", "Name": "  ", "LongName": "Senator ", "LastName": None})
    assert not is_placeholder_member({"Id": "1", "Name": "Ann Rivers", "LastName": "Rivers"})


class RecordingCursor:
    def __init__(self):
        self.batches = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def executemany(self, sql, rows):
        self.batches.append((sql, rows))


def test_placeholder_rows_never_overwrite_a_member():
    cur = RecordingCursor()
    leg = LegislatureSync.__new__(LegislatureSync)
    leg.conn = type("C", (), {"cursor": lambda self: cur})()
    leg._upsert_members("2025-26", [
        {"Id": "15814", "Name": "", "LongName": "Senator", "Agency": "Senate"},
        {"Id": "2", "Name": "Real Person", "FirstName": "Real", "LastName": "Person", "Agency": "House",
         "Party": "D", "District": "1", "LongName": "Representative Person"},
    ])
    for _, rows in cur.batches:
        assert [r[0] for r in rows] == [2]


# ---- CourtListener started requiring a token ----------------------------------------------

def test_courtlistener_401_says_what_to_do(monkeypatch):
    cl = CourtListener(token=None)
    cl.throttle.wait = lambda: None
    transport = httpx.MockTransport(lambda req: httpx.Response(401, json={"detail": "Authentication credentials were not provided."}))
    cl.http = httpx.Client(transport=transport)
    with pytest.raises(AuthRequired, match="COURTLISTENER_API_TOKEN"):
        cl.search_page("anything")


# ---- Contact email ------------------------------------------------------------------------

def configure_email(monkeypatch, password="app-password"):
    monkeypatch.setattr(config, "CONTACT_TO", "owner@example.net")
    monkeypatch.setattr(config, "SMTP_HOST", "smtp.example.net")
    monkeypatch.setattr(config, "SMTP_USER", "sender@example.net")
    monkeypatch.setattr(config, "SMTP_FROM", "sender@example.net")
    monkeypatch.setattr(config, "SMTP_PASSWORD", password)


def test_login_without_password_is_not_configured(monkeypatch):
    configure_email(monkeypatch, password=None)
    assert not contact.email_configured()
    configure_email(monkeypatch)
    assert contact.email_configured()


def test_missing_password_does_not_use_up_retries(monkeypatch):
    configure_email(monkeypatch, password=None)

    class NoDatabase:
        def execute(self, *a):
            raise AssertionError("should not touch messages when email can't work")

    assert contact.send_pending(NoDatabase())["contact_sent"] == 0


@pytest.mark.parametrize("message", [
    "Submit example.org in Google's Search Index so it can show up in web search results!",
    "I would like to discuss AI SEO! May I send you a quote?",
    "We help businesses rank higher on Google while also increasing their presence.",
    "It was not showing up in any search results for the majority of keywords.",
    "Search Engine Optimization steps may still be incomplete.",
    'Great brand <a href="https://shop.example/">buy now</a>',
    "[url=https://shop.example/]buy[/url]",
])
def test_sales_pitches_are_held_back(message):
    assert contact.looks_like_spam(message)


@pytest.mark.parametrize("message", [
    "HB 1217 shows the wrong status, can you check it?",
    "The rent stabilization bill passed the Senate, your page says otherwise.",
    "Why doesn't this site show up in Google when I search for my legislator?",
    "Pregunta sobre el proyecto de ley 1217 y los alquileres en mi distrito.",
    "Этот законопроект неправильно указан на вашем сайте, пожалуйста проверьте.",
    "See https://app.leg.wa.gov/billsummary?BillNumber=1217&Year=2025 for the right date.",
    "Representative Doe's price list of fees in HB 2000 looks wrong.",
])
def test_real_messages_are_not_mistaken_for_spam(message):
    assert not contact.looks_like_spam(message)


def test_spam_is_kept_but_not_emailed(monkeypatch):
    configure_email(monkeypatch)
    rows = [{"id": 1, "topic": "feature", "page": None, "name": None, "reply_to": None,
             "message": "List example.org in Google's Search Index today!", "created_at": datetime.now(timezone.utc)},
            {"id": 2, "topic": "correction", "page": None, "name": None, "reply_to": None,
             "message": "HB 1217 shows the wrong status.", "created_at": datetime.now(timezone.utc)}]
    updates, sent = [], []

    class FakeConn:
        def execute(self, sql, params=()):
            if sql.lstrip().startswith("SELECT"):
                return type("R", (), {"fetchall": lambda self: rows})()
            updates.append((sql.split("email_status = '")[1].split("'")[0], params[-1]))

        def commit(self):
            pass

    monkeypatch.setattr(contact, "_send", lambda e: sent.append(e))
    stats = contact.send_pending(FakeConn())
    assert stats == {"contact_sent": 1, "contact_failed": 0, "contact_spam_held": 1}
    assert updates == [("spam", 1), ("sent", 2)]
    assert len(sent) == 1 and "HB 1217" in sent[0].get_content()


# ---- pypdf was upgraded for security: court opinion text must still come out ----------------

def minimal_pdf(line: str) -> bytes:
    stream = f"BT /F1 12 Tf 72 720 Td ({line}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R"
        b" /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out, offsets = b"%PDF-1.4\n", []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += str(number).encode() + b" 0 obj\n" + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 " + str(len(objects) + 1).encode() + b"\n0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += b"trailer\n<< /Size " + str(len(objects) + 1).encode() + b" /Root 1 0 R >>\nstartxref\n"
    return out + str(xref).encode() + b"\n%%EOF\n"


def pdf_fetcher(response):
    from wapeople.courts.client import PdfFetcher

    fetcher = PdfFetcher()
    fetcher.throttle.wait = lambda: None
    fetcher.http = httpx.Client(transport=httpx.MockTransport(lambda req: response))
    return fetcher


def test_opinion_pdf_text_is_extracted():
    text = pdf_fetcher(httpx.Response(200, content=minimal_pdf("Laws of 2021, ch. 196"))).text("https://x/o.pdf")
    assert "Laws of 2021, ch. 196" in text


def test_missing_and_malformed_pdfs_return_nothing():
    assert pdf_fetcher(httpx.Response(404)).text("https://x/o.pdf") is None
    assert pdf_fetcher(httpx.Response(200, content=b"%PDF-1.4 not really a pdf")).text("https://x/o.pdf") in (None, "")
