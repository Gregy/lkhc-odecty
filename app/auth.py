"""Google OIDC login (Authlib), domain restriction, logout.

Security checks happen server-side on the ID token claims: ``email_verified`` must be
true and ``hd`` must equal ALLOWED_DOMAIN (the ``hd`` URL parameter is only a UI hint).
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any
from urllib.parse import quote

from authlib.integrations.starlette_client import OAuth, OAuthError
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.csrf import rotate_csrf_token
from app.deps import SESSION_LOGIN_AT_KEY, SESSION_USER_KEY, DbSession, get_current_user
from app.models import User
from app.templating import flash, templates

log = logging.getLogger(__name__)

GOOGLE_DISCOVERY_URL = "https://accounts.google.com/.well-known/openid-configuration"

router = APIRouter()


class AuthError(Exception):
    """Login rejected; ``message`` is shown to the user (Czech)."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def make_oauth(settings: Settings) -> OAuth:
    oauth = OAuth()
    oauth.register(
        name="google",
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret,
        server_metadata_url=GOOGLE_DISCOVERY_URL,
        client_kwargs={"scope": "openid email profile"},
    )
    return oauth


def validate_claims(claims: dict[str, Any], settings: Settings) -> tuple[str, str]:
    """Validate ID-token claims; return (email, name). Raises AuthError."""
    email = str(claims.get("email") or "").strip().lower()
    if not email:
        raise AuthError("Účet Google nevrátil e-mail.")
    verified = claims.get("email_verified")
    if verified is not True and str(verified).lower() != "true":
        raise AuthError("E-mail účtu Google není ověřený.")
    domain = settings.allowed_domain
    if not domain:  # fail closed: without a configured Workspace domain nobody gets in
        raise AuthError("Přihlášení není nakonfigurováno (chybí ALLOWED_DOMAIN).")
    hd = str(claims.get("hd") or "").lower()
    if hd != domain or not email.endswith("@" + domain):
        raise AuthError(f"Přihlášení je povoleno jen pro účty domény {domain}.")
    return email, str(claims.get("name") or "")


def login_or_create_user(db: Session, email: str, name: str, settings: Settings) -> User:
    """Find or create the user. ADMIN_EMAILS are always admin; new users are readers."""
    user = db.scalar(select(User).where(func.lower(User.email) == email.lower()))
    if user is None:
        user = User(email=email, name=name, role="reader", active=True)
        db.add(user)
    elif not user.active:
        raise AuthError("Váš účet je deaktivován. Obraťte se na správce.")
    if settings.is_admin_email(email):
        user.role = "admin"
    if name and not user.name:
        user.name = name
    user.last_login_at = datetime.now()
    db.commit()
    return user


def session_login(request: Request, user: User) -> None:
    """Start a fresh session for ``user`` (prevents session fixation)."""
    flashes = request.session.get("_flashes")
    request.session.clear()
    if flashes:
        request.session["_flashes"] = flashes
    request.session[SESSION_USER_KEY] = user.id
    request.session[SESSION_LOGIN_AT_KEY] = int(time.time())
    rotate_csrf_token(request)


def safe_next(target: str | None) -> str:
    """Only allow local absolute paths as redirect targets."""
    if not target or not target.startswith("/") or target.startswith("//") or "\\" in target:
        return "/"
    return target


@router.get("/login", include_in_schema=False)
def login_page(request: Request, db: DbSession, next: str | None = None) -> Response:
    if get_current_user(request, db) is not None:
        return RedirectResponse(safe_next(next), status_code=303)
    settings: Settings = request.app.state.settings
    return templates.TemplateResponse(
        request,
        "login.html",
        {
            "page_title": "Přihlášení",
            "next": safe_next(next),
            "google_configured": bool(
                settings.google_client_id and settings.google_client_secret and settings.allowed_domain
            ),
            "allowed_domain": settings.allowed_domain,
        },
    )


@router.get("/auth/google", include_in_schema=False)
async def auth_google(request: Request, next: str | None = None) -> Response:
    settings: Settings = request.app.state.settings
    if not (settings.google_client_id and settings.google_client_secret and settings.allowed_domain):
        flash(
            request,
            "Přihlášení přes Google není nakonfigurováno (GOOGLE_CLIENT_ID/SECRET, ALLOWED_DOMAIN).",
            "error",
        )
        return RedirectResponse("/login", status_code=303)
    request.session["login_next"] = safe_next(next)
    redirect_uri = settings.base_url.rstrip("/") + "/auth/callback"
    client = request.app.state.oauth.google
    return await client.authorize_redirect(request, redirect_uri, hd=settings.allowed_domain, prompt="select_account")


@router.get("/auth/callback", include_in_schema=False)
async def auth_callback(request: Request, db: DbSession) -> Response:
    settings: Settings = request.app.state.settings
    client = request.app.state.oauth.google
    try:
        token = await client.authorize_access_token(request)
        claims = dict(token.get("userinfo") or {})
        email, name = validate_claims(claims, settings)
        user = login_or_create_user(db, email, name, settings)
    except OAuthError as exc:
        log.warning("OAuth error: %s", exc)
        flash(request, "Přihlášení se nezdařilo. Zkuste to prosím znovu.", "error")
        return RedirectResponse("/login", status_code=303)
    except AuthError as exc:
        log.warning("login rejected: %s", exc.message)
        flash(request, exc.message, "error")
        return RedirectResponse("/login", status_code=303)
    target = safe_next(request.session.get("login_next"))
    session_login(request, user)
    log.info("user %s logged in (%s)", user.email, user.role)
    return RedirectResponse(target, status_code=303)


@router.post("/logout", include_in_schema=False)
def logout(request: Request) -> Response:
    request.session.clear()
    settings: Settings = request.app.state.settings
    if settings.auth_dev_bypass:
        # with the bypass, /login would immediately log in again
        return RedirectResponse("/", status_code=303)
    flash(request, "Byli jste odhlášeni.", "info")
    return RedirectResponse("/login", status_code=303)


def login_redirect_url(request: Request) -> str:
    path = request.url.path
    if request.url.query:
        path += "?" + request.url.query
    return "/login?next=" + quote(path, safe="/")
