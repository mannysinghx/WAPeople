"""Small ASGI middlewares: answer HEAD requests, and send every address to the one canonical host."""
from __future__ import annotations

from urllib.parse import urlsplit

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
            path = scope.get("path", "/")
            if self.should_redirect(Headers(scope=scope).get("host", ""), path):
                query = scope.get("query_string", b"").decode("latin-1")
                location = f"{self.scheme}://{self.host}{path}" + (f"?{query}" if query else "")
                await send({"type": "http.response.start", "status": 301,
                            "headers": [(b"location", location.encode("latin-1")),
                                        (b"content-length", b"0")]})
                await send({"type": "http.response.body", "body": b""})
                return
        await self.app(scope, receive, send)
