"""Small ASGI middlewares: answer HEAD requests, send every address to the one canonical host, and
turn away URLs with a NUL byte."""
from __future__ import annotations

from urllib.parse import quote, quote_from_bytes, urlsplit

from starlette.datastructures import Headers


class HeadMiddleware:
    """FastAPI routes only answer GET, so HEAD got 405 on every page, which breaks uptime monitors
    and link checkers. Run the GET and send the same headers with no body."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method") != "HEAD":
            await self.app(scope, receive, send)
            return

        async def send_headers_only(message):
            if message["type"] == "http.response.start":
                await send(message)
            elif not message.get("more_body"):
                await send({"type": "http.response.body", "body": b"", "more_body": False})

        await self.app({**scope, "method": "GET"}, receive, send_headers_only)


class NulByteMiddleware:
    """PostgreSQL text can't hold a NUL, so a %00 in a URL can only fail further in, as a 500."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            query = scope.get("query_string", b"")
            if "\x00" in scope.get("path", "") or b"%00" in query or b"\x00" in query:
                body = b"Bad request."
                await send({"type": "http.response.start", "status": 400,
                            "headers": [(b"content-type", b"text/plain; charset=utf-8"),
                                        (b"content-length", str(len(body)).encode())]})
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)


# What may stay as it is in a redirect's path and query. "%" is here so existing escapes are kept.
_URL_SAFE = "/%:@!$&'()*+,;=~-._"


def redirect_target(scope) -> str:
    """The path and query exactly as requested. scope["path"] is percent-decoded, so rebuilding the
    URL from it would change its meaning (%3F becomes a real ?) or make a header the server refuses."""
    raw = scope.get("raw_path")
    path = quote_from_bytes(raw, safe=_URL_SAFE) if raw else quote(scope.get("path", "/"), safe="/")
    query = quote_from_bytes(scope.get("query_string", b""), safe=_URL_SAFE + "?")
    return f"{path}?{query}" if query else path


class CanonicalHostMiddleware:
    """301 the www name and the hosting provider's default address to the public site, so one page
    has one address. Only those two kinds of host are touched, and only for page loads; the
    provider's health check and local development never match."""

    def __init__(self, app, site_url: str = ""):
        self.app = app
        parts = urlsplit(site_url or "")
        self.scheme = parts.scheme or "https"
        self.host = (parts.hostname or "").lower()

    def should_redirect(self, host: str, path: str) -> bool:
        host = host.split(":")[0].lower()
        if not self.host or host == self.host or path == "/healthz":
            return False
        return host == f"www.{self.host}" or host.endswith(".up.railway.app")

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope.get("method") in ("GET", "HEAD"):
            if self.should_redirect(Headers(scope=scope).get("host", ""), scope.get("path", "/")):
                location = f"{self.scheme}://{self.host}{redirect_target(scope)}"
                await send({"type": "http.response.start", "status": 301,
                            "headers": [(b"location", location.encode("ascii")),
                                        (b"content-length", b"0")]})
                await send({"type": "http.response.body", "body": b""})
                return
        await self.app(scope, receive, send)
