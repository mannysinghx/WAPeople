from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from wapeople.web.security import MAX_BODY_BYTES, SecurityMiddleware, content_security_policy


async def echo(request: Request):
    body = await request.body()
    return PlainTextResponse(f"{len(body)}")


async def page(request: Request):
    return PlainTextResponse("ok")


def client(base_url="https://testserver"):
    app = Starlette(routes=[Route("/", page), Route("/static/x.css", page), Route("/echo", echo, methods=["POST"])])
    app.add_middleware(SecurityMiddleware)
    return TestClient(app, base_url=base_url)


def test_security_headers_on_every_response():
    r = client().get("/")
    h = r.headers
    assert "script-src 'self'" in h["content-security-policy"]
    assert "frame-ancestors 'none'" in h["content-security-policy"]
    assert h["x-content-type-options"] == "nosniff"
    assert h["x-frame-options"] == "DENY"
    assert h["referrer-policy"] == "strict-origin-when-cross-origin"
    assert "geolocation=(self)" in h["permissions-policy"] and "camera=()" in h["permissions-policy"]
    assert h["strict-transport-security"].startswith("max-age=")
    assert "set-cookie" not in h


def test_no_hsts_or_upgrade_over_plain_http():
    r = client("http://testserver").get("/")
    assert "strict-transport-security" not in r.headers
    assert "upgrade-insecure-requests" not in r.headers["content-security-policy"]
    assert "upgrade-insecure-requests" in content_security_policy(True)


def test_csp_allows_only_the_map_tile_host_as_third_party():
    csp = content_security_policy(True)
    hosts = {tok for tok in csp.replace(";", " ").split() if tok.startswith("https://")}
    assert hosts == {"https://tiles.openfreemap.org"}


def test_static_assets_get_cache_headers():
    assert "max-age" in client().get("/static/x.css").headers["cache-control"]


def test_request_body_limit():
    c = client()
    assert c.post("/echo", content=b"a" * 1000).text == "1000"            # normal posts pass through intact
    assert c.post("/echo", content=b"a" * (MAX_BODY_BYTES + 1)).status_code == 413

    def chunks():  # no Content-Length header: limit enforced while streaming
        for _ in range(MAX_BODY_BYTES // 1024 + 2):
            yield b"a" * 1024
    assert c.post("/echo", content=chunks()).status_code == 413


async def form_echo(request: Request):
    form = await request.form()
    return PlainTextResponse(form["message"])


def test_form_posts_still_parse():
    app = Starlette(routes=[Route("/f", form_echo, methods=["POST"])])
    app.add_middleware(SecurityMiddleware)
    r = TestClient(app).post("/f", data={"message": "hello there"})
    assert r.text == "hello there"
