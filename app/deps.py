"""FastAPI dependencies: DB session, settings, current user, role checks.

Usage in routers::

    router = APIRouter(dependencies=[Depends(require_user)])

    @router.post("/x")
    def edit(db: Session = Depends(get_db), user: User = Depends(require_admin)): ...
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from datetime import datetime
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import User

log = logging.getLogger(__name__)

SESSION_USER_KEY = "user_id"
SESSION_LOGIN_AT_KEY = "login_at"
# Absolute session lifetime: the signed cookie is re-issued on every response (sliding expiry), so a
# login is additionally limited to this many seconds since it happened.
SESSION_ABSOLUTE_MAX_AGE = 14 * 24 * 3600


class LoginRequired(Exception):  # noqa: N818
    """Raised when an anonymous user hits a protected route (handled in main.py)."""


def get_settings_dep(request: Request) -> Settings:
    return request.app.state.settings


def get_db(request: Request) -> Iterator[Session]:
    db = request.app.state.sessionmaker()
    try:
        yield db
    finally:
        db.close()


DbSession = Annotated[Session, Depends(get_db)]


def ensure_dev_user(db: Session, settings: Settings) -> User:
    """Fictional admin used with AUTH_DEV_BYPASS."""
    email = f"dev@{settings.allowed_domain or 'localhost'}"
    user = db.scalar(select(User).where(User.email == email))
    if user is None:
        user = User(email=email, name="Vývojář (dev bypass)", role="admin", active=True)
        db.add(user)
    user.role = "admin"
    user.active = True
    user.last_login_at = datetime.now()
    db.commit()
    return user


def get_current_user(request: Request, db: DbSession) -> User | None:
    """Logged-in user or None. With AUTH_DEV_BYPASS, auto-logs in the dev admin."""
    if hasattr(request.state, "user"):
        return request.state.user
    user: User | None = None
    uid = request.session.get(SESSION_USER_KEY)
    login_at = request.session.get(SESSION_LOGIN_AT_KEY)
    if uid is not None and (not isinstance(login_at, int | float) or time.time() - login_at > SESSION_ABSOLUTE_MAX_AGE):
        request.session.clear()  # expired (or pre-dates login_at tracking): log in again
        uid = None
    if uid is not None:
        user = db.get(User, uid)
        if user is not None and not user.active:
            user = None
        if user is None:
            request.session.pop(SESSION_USER_KEY, None)
    if user is None and request.app.state.settings.auth_dev_bypass:
        user = ensure_dev_user(db, request.app.state.settings)
        request.session[SESSION_USER_KEY] = user.id
        request.session[SESSION_LOGIN_AT_KEY] = int(time.time())
    request.state.user = user
    return user


def require_user(user: Annotated[User | None, Depends(get_current_user)]) -> User:
    if user is None:
        raise LoginRequired()
    return user


def require_admin(user: Annotated[User, Depends(require_user)]) -> User:
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Tato akce vyžaduje roli administrátora.")
    return user


CurrentUser = Annotated[User, Depends(require_user)]
AdminUser = Annotated[User, Depends(require_admin)]
