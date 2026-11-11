"""CSRF protection for mutating requests (POST/PUT/PATCH/DELETE).

The per-session token is accepted from either:
- header ``X-CSRF-Token`` (``app.js`` adds it to every HTMX request and to
  same-origin ``fetch`` calls; the token is in ``<meta name="csrf-token">``), or
- form field ``csrf_token`` (``app.js`` injects it into every ``<form method=post>``;
  templates can also use the ``csrf_field()`` macro).

Requests without a session token (no session yet) are rejected as well.
"""

from __future__ import annotations

import secrets

from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

SESSION_KEY = "csrf_token"
HEADER_NAME = "x-csrf-token"
FORM_FIELD = "csrf_token"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})
# Largest form body buffered to look for the token field. JS uploads (walk, batch) send the header and
# are never buffered here; this only covers plain no-JS form posts.
MAX_BUFFERED_FORM_BYTES = 64 * 1024 * 1024
SESSION_USER_KEY = "user_id"


def get_csrf_token(request: Request) -> str:
    """Return the session's CSRF token, creating it if needed."""
    token = request.session.get(SESSION_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        request.session[SESSION_KEY] = token
    return token


def rotate_csrf_token(request: Request) -> str:
    request.session[SESSION_KEY] = secrets.token_urlsafe(32)
    return request.session[SESSION_KEY]


class CSRFMiddleware:
    """Pure ASGI middleware; must run *inside* SessionMiddleware."""

    def __init__(self, app: ASGIApp, exempt_paths: frozenset[str] = frozenset()) -> None:
        self.app = app
        self.exempt_paths = exempt_paths

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] in SAFE_METHODS or scope["path"] in self.exempt_paths:
            await self.app(scope, receive, send)
            return
        expected = scope.get("session", {}).get(SESSION_KEY)
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        provided = headers.get(HEADER_NAME)
        if provided and expected and secrets.compare_digest(provided, expected):
            await self.app(scope, receive, send)
            return

        ctype = headers.get("content-type", "")
        logged_in = scope.get("session", {}).get(SESSION_USER_KEY) is not None
        if expected and logged_in and ctype.startswith(("application/x-www-form-urlencoded", "multipart/form-data")):
            # Buffer the body (bounded), read the token from the form, then replay the body downstream.
            too_big = PlainTextResponse("Požadavek je příliš velký.", 413)
            if int(headers.get("content-length") or 0) > MAX_BUFFERED_FORM_BYTES:
                await too_big(scope, receive, send)
                return
            chunks: list[bytes] = []
            size = 0
            more = True
            while more:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                chunk = message.get("body", b"")
                size += len(chunk)
                if size > MAX_BUFFERED_FORM_BYTES:
                    await too_big(scope, receive, send)
                    return
                chunks.append(chunk)
                more = message.get("more_body", False)
            body = b"".join(chunks)

            async def replay_once() -> Message:
                return {"type": "http.request", "body": body, "more_body": False}

            req = Request(scope, replay_once)
            async with req.form() as form:
                value = form.get(FORM_FIELD)
            if isinstance(value, str) and secrets.compare_digest(value, expected):
                sent = False

                async def replay() -> Message:
                    nonlocal sent
                    if not sent:
                        sent = True
                        return {"type": "http.request", "body": body, "more_body": False}
                    return await receive()

                await self.app(scope, replay, send)
                return

        response = PlainTextResponse("Neplatný nebo chybějící CSRF token. Obnovte stránku a zkuste to znovu.", 403)
        await response(scope, receive, send)
