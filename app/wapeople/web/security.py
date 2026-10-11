"""Security headers on every response, and a cap on request body size (pure ASGI middleware)."""
from __future__ import annotations

from starlette.datastructures import MutableHeaders

MAX_BODY_BYTES = 64 * 1024   # the contact form is the only thing that posts; 5,000 characters fit easily
BODY_METHODS = {"POST", "PUT", "PATCH"}

# Third parties the browser may contact: OpenFreeMap serves the map's base tiles, fonts and styles.
MAP_TILES = "https://tiles.openfreemap.org"


def content_security_policy(https: bool) -> str:
    parts = [
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self' 'unsafe-inline'",            # style="" attributes in templates
        f"img-src 'self' data: blob: {MAP_TILES}",
        "font-src 'self'",
        f"connect-src 'self' {MAP_TILES}",
        "worker-src 'self' blob:",                     # MapLibre renders map tiles in a blob worker
        "child-src blob:",
        "frame-ancestors 'none'",
        "base-uri 'self'",
        "form-action 'self'",
        "object-src 'none'",
    ]
    if https:
        parts.append("upgrade-insecure-requests")
    return "; ".join(parts)


def security_headers(https: bool) -> dict[str, str]:
    h = {
        "Content-Security-Policy": content_security_policy(https),
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "strict-origin-when-cross-origin",
        "Permissions-Policy": "geolocation=(self), camera=(), microphone=(), payment=(), usb=()",
        "Cross-Origin-Opener-Policy": "same-origin",
    }
    if https:
        h["Strict-Transport-Security"] = "max-age=31536000"
    return h


class SecurityMiddleware:
    def __init__(self, app, max_body: int = MAX_BODY_BYTES):
        self.app = app
        self.max_body = max_body

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        https = scope.get("scheme") == "https"
        extra = security_headers(https)
        static = scope.get("path", "").startswith("/static/")

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for k, v in extra.items():
                    if k not in headers:
                        headers[k] = v
                if static and "cache-control" not in headers:
                    headers["Cache-Control"] = "public, max-age=604800"  # assets are versioned (?v=)
            await send(message)

        if scope.get("method") in BODY_METHODS:
            # Read the body up front (bounded) so an oversized upload never reaches the app.
            declared = dict(scope.get("headers") or []).get(b"content-length", b"")
            if declared.isdigit() and int(declared) > self.max_body:
                await self._too_large(send_with_headers)
                return
            chunks, size, more = [], 0, True
            while more:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                chunk = message.get("body", b"")
                size += len(chunk)
                if size > self.max_body:
                    await self._too_large(send_with_headers)
                    return
                chunks.append(chunk)
                more = message.get("more_body", False)
            body = b"".join(chunks)
            replayed = False

            async def replay():
                nonlocal replayed
                if not replayed:
                    replayed = True
                    return {"type": "http.request", "body": body, "more_body": False}
                return await receive()

            await self.app(scope, replay, send_with_headers)
            return

        await self.app(scope, receive, send_with_headers)

    @staticmethod
    async def _too_large(send):
        await send({"type": "http.response.start", "status": 413,
                    "headers": [(b"content-type", b"text/plain; charset=utf-8")]})
        await send({"type": "http.response.body", "body": b"Request too large."})
