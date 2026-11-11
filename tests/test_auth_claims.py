"""Server-side claim validation, user provisioning, settings guards."""

from collections.abc import Callable

import pytest
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.auth import AuthError, login_or_create_user, safe_next, validate_claims
from app.config import Settings
from app.models import User

GOOD = {"email": "Pilot@example.org", "email_verified": True, "hd": "example.org", "name": "Pilot"}


def test_valid_claims(make_settings: Callable[..., Settings]) -> None:
    assert validate_claims(GOOD, make_settings()) == ("pilot@example.org", "Pilot")


@pytest.mark.parametrize(
    "override",
    [
        {"hd": None},  # personal gmail account
        {"hd": "evil.example.net"},
        {"email": "pilot@evil.example.net"},  # hd claim present but email from another domain
        {"email_verified": False},
        {"email_verified": None},
        {"email": ""},
    ],
)
def test_rejected_claims(make_settings: Callable[..., Settings], override: dict) -> None:
    claims = {**GOOD, **override}
    with pytest.raises(AuthError):
        validate_claims(claims, make_settings())


def test_new_user_is_reader_admin_emails_are_admin(db: Session, make_settings: Callable[..., Settings]) -> None:
    s = make_settings()
    reader = login_or_create_user(db, "pilot@example.org", "Pilot", s)
    assert reader.role == "reader" and reader.last_login_at is not None
    boss = login_or_create_user(db, "boss@example.org", "Boss", s)
    assert boss.role == "admin"
    # role changed in UI persists for non-ADMIN_EMAILS users
    reader.role = "admin"
    db.commit()
    assert login_or_create_user(db, "pilot@example.org", "", s).role == "admin"


def test_inactive_user_rejected(db: Session, make_settings: Callable[..., Settings]) -> None:
    db.add(User(email="gone@example.org", name="", role="reader", active=False))
    db.commit()
    with pytest.raises(AuthError):
        login_or_create_user(db, "gone@example.org", "", make_settings())


def test_empty_domain_fails_closed(make_settings: Callable[..., Settings]) -> None:
    """The Workspace domain is installation data (.env only): no default, nobody gets in without it."""
    assert Settings(_env_file=None).allowed_domain == ""  # type: ignore[call-arg]
    s = make_settings(allowed_domain="")
    for claims in (GOOD, {**GOOD, "hd": None, "email": "pilot@"}, {**GOOD, "hd": "", "email": "x@gmail.com"}):
        with pytest.raises(AuthError):
            validate_claims(claims, s)
    with pytest.raises(ValidationError, match="ALLOWED_DOMAIN"):
        make_settings(app_env="production", allowed_domain="")
    with pytest.raises(ValidationError, match="ALLOWED_DOMAIN"):  # a real host name implies production
        Settings(_env_file=None, base_url="https://odecty.example.org", secret_key="x" * 40)  # type: ignore[call-arg]


def test_dev_bypass_refused_in_production(make_settings: Callable[..., Settings]) -> None:
    with pytest.raises(ValidationError):
        make_settings(app_env="production", auth_dev_bypass=True)
    with pytest.raises(ValidationError):
        make_settings(app_env="production", secret_key="short")
    assert make_settings(app_env="production").app_env == "production"


def test_admin_emails_parsing() -> None:
    s = Settings(_env_file=None, admin_emails=" A@example.org; b@example.org ,")  # type: ignore[call-arg]
    assert s.admin_emails == ["a@example.org", "b@example.org"]


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        (None, "/"),
        ("/spotreba?rok=2025", "/spotreba?rok=2025"),
        ("https://evil", "/"),
        ("//evil", "/"),
        ("/\\evil", "/"),
    ],
)
def test_safe_next(target: str | None, expected: str) -> None:
    assert safe_next(target) == expected
