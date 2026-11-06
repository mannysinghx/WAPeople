"""Regression tests for bugs found on the live site in November 2026."""
from datetime import datetime, timezone

import logging

import httpx
import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from wapeople import config, contact
from wapeople.courts.client import AuthRequired, CourtListener
from wapeople.legislature.sync import LegislatureSync, is_placeholder_member
from wapeople.web.middleware import CanonicalHostMiddleware, HeadMiddleware, NulByteMiddleware, redirect_target


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


# ---- The redirect keeps the URL exactly as it was requested ---------------------------------

@pytest.mark.parametrize("path", ["/%E2%82%AC", "/a%3Fb=c", "/a%23frag", "/caf%C3%A9", "/a%20b",
                                  "/x%0d%0aSet-Cookie:%20a=1", "/x%00y", "/money/organizations/BOEING"])
def test_redirect_keeps_the_path_as_it_was_sent(path):
    r = host_client().get(path, headers={"host": "www.wapeople.org"})
    assert r.status_code == 301
    assert r.headers["location"] == f"https://wapeople.org{path}"


def test_redirect_stays_on_the_public_site():
    r = host_client().get("//evil.example/x", headers={"host": "www.wapeople.org"})
    assert r.headers["location"].startswith("https://wapeople.org/")


def test_redirect_target_encodes_raw_bytes_and_has_a_fallback():
    assert redirect_target({"raw_path": b"/caf\xc3\xa9 x", "query_string": b"q=a b"}) == "/caf%C3%A9%20x?q=a%20b"
    assert redirect_target({"path": "/caf\u00e9 x?", "query_string": b""}) == "/caf%C3%A9%20x%3F"


# ---- A NUL byte can't reach the database ----------------------------------------------------

def nul_client():
    async def page(request):
        return PlainTextResponse("ok")
    app = Starlette(routes=[Route("/{p:path}", page, methods=["GET", "POST"])])
    app.add_middleware(NulByteMiddleware)
    return TestClient(app)


@pytest.mark.parametrize("url", ["/money?q=%00", "/x%00y", "/money?a=1&b=x%00", "/%00"])
def test_a_nul_in_the_url_is_a_400(url):
    assert nul_client().get(url).status_code == 400


@pytest.mark.parametrize("url", ["/", "/money?q=100%25", "/money?q=%2500", "/money?q=a%200"])
def test_other_urls_are_untouched(url):
    assert nul_client().get(url).status_code == 200


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
    assert len(cur.batches) == 2       # a method that writes nothing must not pass
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


@pytest.mark.parametrize("message", [
    "Senator Ann Rivers is not showing up in your search results even though she has 240 bills.",
    "HB 1234 is not showing up in the search results on your site. Please check it.",
    "Your bill pages are missing from Google's search index, so people can't find HB 1234 from a search.",
    # Bill topics, not sales pitches.
    "The traffic camera bill is not showing up in your search results.",
    "The bill on price gouging is not showing up in the search results.",
    "We can't find HB 1500 because it's not showing up in your search results.",
    "The affordable housing bill is not showing up in your search results.",
    "The bill on drug pricing is not showing up in the search results.",
    "The school rankings bill is not showing up in your search results.",
    "HB 1200 on more traffic cameras is not showing up in your search results.",
    # Ordinary ways to describe trying the site's search.
    "I tried to submit a search but HB 1234 is not showing up in your search results.",
    "I tried several keywords and HB 1234 is not showing up in your search results.",
])
def test_reports_about_search_are_not_spam(message):
    assert not contact.looks_like_spam(message)


@pytest.mark.parametrize("message", [
    "Your site is not showing up in search results. We help businesses like yours, ask for a free audit.",
    "Your pages are missing from Google's search index. Our team can fix it, see our pricing.",
    '<a class=x href="https://shop.example/">cheap S E O</a>',
    "Submit your website to Google's search index today.",
    "Your site is not showing up in search results. Boost your rankings with affordable packages.",
    "Your pages are not showing up in search results. Get a quote for organic traffic.",
])
def test_sales_pitches_about_search_are_still_held(message):
    assert contact.looks_like_spam(message)


def test_security_reports_are_never_held():
    payload = "Your search box runs this: <a href=javascript:alert(1)>click</a>"
    assert contact.looks_like_spam(payload)
    assert not contact.looks_like_spam(payload, "security")


def test_a_security_report_is_emailed_and_held_messages_are_counted_not_quoted(monkeypatch, caplog):
    configure_email(monkeypatch)
    rows = [{"id": 1, "topic": "security", "page": None, "name": None, "reply_to": None,
             "message": "Payload: <a href=javascript:alert(1)>x</a>", "created_at": datetime.now(timezone.utc)},
            {"id": 2, "topic": "other", "page": None, "name": None, "reply_to": None,
             "message": 'Buy now <a href="https://secret.example/">here</a>', "created_at": datetime.now(timezone.utc)}]
    updates, sent = [], []

    class FakeConn:
        def execute(self, sql, params=()):
            if sql.lstrip().startswith("SELECT"):
                return type("R", (), {"fetchall": lambda self: rows})()
            updates.append((sql.split("email_status = '")[1].split("'")[0], params[-1]))

        def commit(self):
            pass

    monkeypatch.setattr(contact, "_send", lambda e: sent.append(e))
    with caplog.at_level(logging.WARNING, logger="wapeople.contact"):
        stats = contact.send_pending(FakeConn())
    assert stats == {"contact_sent": 1, "contact_failed": 0, "contact_spam_held": 1}
    assert updates == [("sent", 1), ("spam", 2)]
    assert "1 message(s) held" in caplog.text and "secret.example" not in caplog.text


def test_the_daily_retention_run_reports_how_many_messages_are_held(caplog):
    class Result:
        rowcount = 0

        def fetchone(self):
            return {"n": 3}

    class FakeConn:
        def execute(self, sql, params=()):
            return Result()

        def commit(self):
            pass

    with caplog.at_level(logging.WARNING, logger="wapeople.contact"):
        stats = contact.apply_retention(FakeConn())
    assert stats["contact_spam_on_file"] == 3
    assert "3 message(s) held" in caplog.text


@pytest.mark.parametrize("token", ["1.\u00e9", "9" * 400 + ".abc", "9" * 5000 + ".abc", "\u00b2.abc", "nope", "", ".", "1.\udc80"])
def test_malformed_form_tokens_are_just_invalid(token):
    assert contact.token_ok(token) is False


def test_a_good_token_still_passes():
    assert contact.token_ok(contact.form_token(now=1000.0), now=1010.0)
    assert not contact.token_ok(contact.form_token(now=1000.0), now=1001.0)


def test_nul_bytes_never_reach_the_contact_table():
    clean, errors = contact.validate({"topic": "other", "name": "A\x00B", "email": "a\x00@example.org", "page": "/x\x00",
                                      "message": "This is a long enough\x00 message to pass."})
    assert not errors
    assert not any("\x00" in (v or "") for v in clean.values())
    assert clean["message"] == "This is a long enough message to pass."


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


# ---- Number formatting ----------------------------------------------------------------------

def test_values_that_round_to_zero_have_no_minus_sign():
    from wapeople.web.main import usd, usd_cents, usd_short

    assert usd(-0.4) == "$0" and usd(-0.6) == "-$1" and usd(0) == "$0"
    assert usd_cents(-0.001) == "$0.00" and usd_cents(-69.93) == "-$69.93"
    assert usd_short(-0.4) == "$0" and usd_short(-600) == "-$600"


@pytest.mark.parametrize("value, label", [
    (9_999, "$9,999"), (9_999.6, "$10,000"), (10_000, "$10K"), (45_300, "$45K"), (999_499, "$999K"),
    (999_500, "$1.0M"), (999_950, "$1.0M"), (999_999.99, "$1.0M"), (1_000_000, "$1.0M"), (1_234_567, "$1.2M"),
    (-999_950, "-$1.0M"), (-45_300, "-$45K"),
])
def test_usd_short_at_its_thresholds(value, label):
    from wapeople.web.main import usd_short

    assert usd_short(value) == label


@pytest.mark.parametrize("raw, label", [
    ("0-29999", "$0–$29,999"), ("500000+", "$500,000 or more"), ("1000000-", "$1,000,000 or more"),
    ("129042.00", "$129,042.00"), ("311347", "$311,347"), ("1000.50-2000.75", "$1,000.50–$2,000.75"),
    ("Unknown", "Unknown"), ("\u00b2", "\u00b2"), ("", "—"), (None, "—"),
])
def test_range_label(raw, label):
    from wapeople.web.main import range_label

    assert range_label(raw) == label


@pytest.mark.parametrize("raw, label", [
    ("-69.93", "-$69.93"), ("710.82", "$710.82"), ("$1,200", "$1,200.00"), ("about 50", "about 50"), ("", ""),
])
def test_money_text(raw, label):
    from wapeople.web.main import money_text

    assert money_text(raw) == label


@pytest.mark.parametrize("raw, label", [("100", "100%"), ("100%", "100%"), ("20.96", "20.96%"), (".001", ".001%"), ("half", "half")])
def test_percent_text(raw, label):
    from wapeople.web.main import percent_text

    assert percent_text(raw) == label


def test_filter_numbers_must_be_short_ascii_digits():
    from wapeople.web.main import year_param
    from wapeople.web.money_routes import _int

    assert _int("2024") == 2024 and _int("999999999") == 999999999
    for junk in ("", "0", "abc", "\u00b2", "\u0663", "-1", "1.5", "2147483648", "9" * 5000):
        assert _int(junk) is None
    assert year_param("2024") == 2024
    for junk in ("", "99999", "\u00b2", "\u0663\u0663\u0663\u0663", "20 4"):
        assert year_param(junk) is None


# ---- A page must never wait for a second pooled connection while holding one ------------------

def test_no_route_renders_while_holding_a_connection():
    """render() takes its own connection. Doing that inside `with conn()` froze the whole site at ten
    overlapping requests. The integration test watches the pool on pages with data in its fixture;
    this reads the code, so a route it can't reach is covered too."""
    import ast
    import inspect

    from wapeople.web import main, money_routes

    blocks = 0
    for module in (main, money_routes):
        for node in ast.walk(ast.parse(inspect.getsource(module))):
            if isinstance(node, ast.With) and any(getattr(i.context_expr, "func", None) is not None
                                                  and getattr(i.context_expr.func, "id", "") == "conn" for i in node.items):
                blocks += 1
                called = {n.func.id for n in ast.walk(node) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
                assert "render" not in called, f"{module.__name__} line {node.lineno}"
    assert blocks > 10      # the walk really found the routes
