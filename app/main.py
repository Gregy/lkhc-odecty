"""Application factory: settings, DB, middleware, routers, error pages.

Run locally: ``AUTH_DEV_BYPASS=1 uv run uvicorn app.main:app``.
"""

from __future__ import annotations

import contextlib
import logging
import os
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app import auth
from app.config import Settings, get_settings
from app.csrf import CSRFMiddleware
from app.db import make_engine, make_sessionmaker
from app.deps import LoginRequired
from app.extraction import queue as extraction_queue
from app.routers import dashboard, fotky, mista, najemci, obchuzka, odecty, spotreba, sprava, vyuctovani
from app.templating import templates

log = logging.getLogger("app")

STATIC_DIR = Path(__file__).parent / "static"
SESSION_COOKIE = "lkhc_session"
SESSION_MAX_AGE = 14 * 24 * 3600

PAGE_ROUTERS = (dashboard, spotreba, mista, najemci, vyuctovani, sprava, odecty, obchuzka, fotky)

ERROR_TITLES = {403: "Přístup odepřen", 404: "Stránka nenalezena", 405: "Nepovolená metoda"}


class SecurityHeadersMiddleware:
    """Baseline security headers on every response (an endpoint's own value wins)."""

    def __init__(self, app: ASGIApp, *, hsts: bool) -> None:
        self.app = app
        self.headers = [
            (b"x-content-type-options", b"nosniff"),
            (b"x-frame-options", b"DENY"),
            (b"referrer-policy", b"same-origin"),
        ]
        if hsts:
            self.headers.append((b"strict-transport-security", b"max-age=31536000"))

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                present = {k.lower() for k, _ in message.get("headers", [])}
                extra = [(k, v) for k, v in self.headers if k not in present]
                message["headers"] = [*message.get("headers", []), *extra]
            await send(message)

        await self.app(scope, receive, send_with_headers)


def _warn_if_not_migrated(url: str) -> None:
    """Migrations are not run by the web app itself (see app/migrate.py, run by the container entrypoint)."""
    from app.migrate import revisions

    try:
        current, head = revisions(url)
    except Exception:  # pragma: no cover - never block the start on this check
        log.exception("cannot determine the database revision")
        return
    if current != head:
        log.error(
            "Databáze má revizi %s, aplikace potřebuje %s – spusťte `python -m app.migrate` "
            "(v kontejneru se to děje automaticky při startu).",
            current or "(prázdná)",
            head,
        )


def _check_revision_on_startup(app: FastAPI, url: str) -> None:
    """Run the revision check when the server starts (not when the module is merely imported)."""
    previous = app.router.lifespan_context

    @contextlib.asynccontextmanager
    async def lifespan(a: FastAPI) -> AsyncIterator[Any]:
        _warn_if_not_migrated(url)
        async with previous(a) as state:
            yield state

    app.router.lifespan_context = lifespan


def _wants_html(request: Request) -> bool:
    return "text/html" in request.headers.get("accept", "") and not request.url.path.startswith("/api/")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    # The app works with naive local times (readings at 00:00 of the day, "today", EXIF, audit):
    # make the process clock local to TZ (Europe/Prague) even in a UTC container.
    if os.environ.get("TZ") != settings.tz and hasattr(time, "tzset"):
        os.environ["TZ"] = settings.tz
        time.tzset()
    if settings.auth_dev_bypass:
        # Settings validation already refuses this in production.
        banner = "!" * 72
        log.warning(
            "%s\nAUTH_DEV_BYPASS je ZAPNUTÝ: každý požadavek je přihlášen jako fiktivní admin. "
            "Nikdy nepoužívat v produkci!\n%s",
            banner,
            banner,
        )
    elif not (settings.google_client_id and settings.google_client_secret and settings.allowed_domain):
        log.warning(
            "GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET / ALLOWED_DOMAIN nejsou nastavené: aplikace běží, ale "
            "přihlásit se nejde (stránka /login to vysvětlí). Doplňte je do .env a vytvořte kontejner znovu."
        )
    settings.data_dir.mkdir(parents=True, exist_ok=True)

    app = FastAPI(title="LKHC Odečty", docs_url=None, redoc_url=None, openapi_url=None)
    engine = make_engine(settings.sqlalchemy_url)
    app.state.settings = settings
    app.state.engine = engine
    app.state.sessionmaker = make_sessionmaker(engine)
    app.state.oauth = auth.make_oauth(settings)

    # Order matters: the last added middleware is the outermost; CSRF needs the session.
    # [capture] the Web Share Target fallback is a non-mutating navigation POST from the OS share sheet
    # (see app/routers/obchuzka.py: share_fallback); everything else needs the token.
    app.add_middleware(CSRFMiddleware, exempt_paths=obchuzka.CSRF_EXEMPT_PATHS)
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key,
        session_cookie=SESSION_COOKIE,
        max_age=SESSION_MAX_AGE,
        same_site="lax",
        https_only=settings.cookie_secure,
    )
    app.add_middleware(SecurityHeadersMiddleware, hsts=settings.cookie_secure)

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.include_router(auth.router)
    for module in PAGE_ROUTERS:
        app.include_router(module.router)
    app.include_router(sprava.overview_router)  # /sprava landing page, open to readers too
    extraction_queue.install(app)  # [capture] background extraction worker in the app lifespan
    _check_revision_on_startup(app, settings.sqlalchemy_url)

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> PlainTextResponse:
        return PlainTextResponse("ok")

    @app.exception_handler(LoginRequired)
    async def _login_required(request: Request, exc: LoginRequired) -> Response:
        target = auth.login_redirect_url(request)
        if request.headers.get("hx-request"):
            return Response(status_code=401, headers={"HX-Redirect": target})
        if not _wants_html(request) and request.method != "GET":
            return JSONResponse({"detail": "Nepřihlášen."}, status_code=401)
        return RedirectResponse(target, status_code=303)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> Response:
        if _wants_html(request) and exc.status_code in ERROR_TITLES:
            return templates.TemplateResponse(
                request,
                "error.html",
                {
                    "page_title": ERROR_TITLES[exc.status_code],
                    "status_code": exc.status_code,
                    "detail": exc.detail if isinstance(exc.detail, str) else "",
                },
                status_code=exc.status_code,
                headers=getattr(exc, "headers", None),
            )
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=getattr(exc, "headers", None))

    return app


app = create_app()
