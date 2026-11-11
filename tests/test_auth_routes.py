"""Login flow (mocked Google), protection of routes, roles, CSRF, dev bypass, stub pages."""

import re
from collections.abc import Callable

import pytest
from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.models import User

STUB_ROUTES = [
    "/",
    "/spotreba",
    "/mista",
    "/mista/1",
    "/najemci",
    "/najemci/1",
    "/vyuctovani",
    "/vyuctovani/1",
    "/odecty",
    "/odecty/davka",
    "/odecty/kontrola",
    "/obchuzka",
    "/fotky",
    "/fotky/1",
    "/sprava",
]


class FakeGoogle:
    def __init__(self, claims: dict) -> None:
        self.claims = claims

    async def authorize_redirect(self, request, redirect_uri, **kwargs):
        self.redirect_kwargs = {"redirect_uri": redirect_uri, **kwargs}
        return RedirectResponse("https://accounts.google.com/o/oauth2/auth?fake=1", status_code=302)

    async def authorize_access_token(self, request):
        return {"userinfo": self.claims}


class FakeOAuth:
    def __init__(self, claims: dict) -> None:
        self.google = FakeGoogle(claims)


def test_anonymous_is_redirected_to_login(anon_client: TestClient) -> None:
    r = anon_client.get("/spotreba?rok=2025", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login?next=/spotreba%3Frok%3D2025"
    r = anon_client.get("/spotreba", headers={"HX-Request": "true"}, follow_redirects=False)
    assert r.status_code == 401 and r.headers["HX-Redirect"].startswith("/login")
    page = anon_client.get("/login")
    assert page.status_code == 200 and "Přihlásit se účtem Google" in page.text


def test_google_login_flow(app: FastAPI) -> None:
    fake = FakeOAuth({"email": "Pilot@example.org", "email_verified": True, "hd": "example.org", "name": "Pilot"})
    app.state.oauth = fake
    with TestClient(app) as c:
        r = c.get("/auth/google?next=/mista", follow_redirects=False)
        assert r.status_code == 302 and fake.google.redirect_kwargs["hd"] == "example.org"
        assert fake.google.redirect_kwargs["redirect_uri"] == "http://testserver/auth/callback"
        r = c.get("/auth/callback", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/mista"
        page = c.get("/mista")
        assert page.status_code == 200 and "pilot@example.org" in page.text
        assert c.get("/sprava").status_code == 403  # new users are readers
    with app.state.sessionmaker() as s:
        assert s.scalar(select(User).where(User.email == "pilot@example.org")).role == "reader"


def test_login_disabled_without_domain(make_app, db_url: str) -> None:
    app = make_app(db_url, allowed_domain="")
    app.state.oauth = FakeOAuth({"email": "pilot@", "email_verified": True, "hd": "", "name": "P"})
    with TestClient(app) as c:
        assert "Přihlásit se účtem Google" not in c.get("/login").text
        r = c.get("/auth/google", follow_redirects=True)
        assert "ALLOWED_DOMAIN" in r.text and fake_not_called(app)
        r = c.get("/auth/callback", follow_redirects=True)
        assert "/login" in str(r.url) and c.get("/", follow_redirects=False).status_code == 303


def fake_not_called(app: FastAPI) -> bool:
    return not getattr(app.state.oauth.google, "redirect_kwargs", None)


def test_google_login_wrong_domain_rejected(app: FastAPI) -> None:
    app.state.oauth = FakeOAuth({"email": "x@gmail.com", "email_verified": True, "name": "X"})
    with TestClient(app) as c:
        r = c.get("/auth/callback", follow_redirects=True)
        assert "/login" in str(r.url) and "domény example.org" in r.text
        assert c.get("/", follow_redirects=False).status_code == 303


def test_roles(admin_client: TestClient, reader_client: TestClient) -> None:
    assert admin_client.get("/sprava").status_code == 200
    r = reader_client.get("/sprava", headers={"Accept": "text/html"})
    assert r.status_code == 403 and "Přístup odepřen" in r.text
    assert "Správa" in admin_client.get("/").text
    assert 'href="/sprava"' not in reader_client.get("/").text


@pytest.mark.parametrize("path", STUB_ROUTES)
def test_stub_pages_render(admin_client: TestClient, path: str) -> None:
    # Originally asserted the "připravuje se" stubs; the pages are implemented now. On the empty DB
    # every page must still render (detail pages of non-existent records give a 404 page).
    r = admin_client.get(path, headers={"Accept": "text/html"})
    assert r.status_code == (404 if path.rstrip("/").split("/")[-1].isdigit() else 200)
    assert '<html lang="cs">' in r.text


def _add_echo(app: FastAPI) -> None:
    from fastapi import Request

    async def echo(request: Request) -> dict:
        form = await request.form()
        return {"ok": True, "fields": sorted(k for k in form if k != "csrf_token")}

    app.add_api_route("/__test__/echo", echo, methods=["POST"])


def test_csrf(app: FastAPI, login_client: Callable[..., TestClient]) -> None:
    _add_echo(app)
    c = login_client(app, "admin")
    token = c.headers.pop("X-CSRF-Token")
    assert c.post("/__test__/echo", data={"a": "1"}).status_code == 403
    assert c.post("/__test__/echo", data={"a": "1", "csrf_token": "wrong"}).status_code == 403
    # header
    assert c.post("/__test__/echo", data={"a": "1"}, headers={"X-CSRF-Token": token}).json()["fields"] == ["a"]
    # urlencoded hidden field - body must still reach the endpoint
    assert c.post("/__test__/echo", data={"a": "1", "csrf_token": token}).json() == {"ok": True, "fields": ["a"]}
    # multipart (file upload) with hidden field
    r = c.post(
        "/__test__/echo", data={"csrf_token": token}, files={"photo": ("x.jpg", b"\xff\xd8" * 1000, "image/jpeg")}
    )
    assert r.json() == {"ok": True, "fields": ["photo"]}
    # JSON with header
    assert c.post("/__test__/echo", json={}, headers={"X-CSRF-Token": token}).status_code == 200
    # logout requires CSRF too
    assert c.post("/logout", follow_redirects=False).status_code == 403
    assert c.post("/logout", data={"csrf_token": token}, follow_redirects=False).status_code == 303


def test_csrf_token_in_page_meta(admin_client: TestClient) -> None:
    html = admin_client.get("/").text
    m = re.search(r'<meta name="csrf-token" content="([^"]+)">', html)
    assert m and m.group(1) == admin_client.csrf_token  # type: ignore[attr-defined]


def test_logout(admin_client: TestClient) -> None:
    r = admin_client.post("/logout", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert admin_client.get("/", follow_redirects=False).status_code == 303


def test_dev_bypass_logs_in_fake_admin(make_app: Callable[..., FastAPI], db_url: str) -> None:
    app = make_app(db_url, auth_dev_bypass=True)
    with TestClient(app) as c:
        r = c.get("/sprava")
        assert r.status_code == 200 and "dev@example.org" in r.text and "AUTH_DEV_BYPASS" in r.text


def test_session_cookie_flags(
    make_app: Callable[..., FastAPI], db_url: str, login_client: Callable[..., TestClient]
) -> None:
    app = make_app(db_url)
    c = login_client(app, "reader")
    r = c.get("/__test__/login", params={"email": "r2@example.org"})
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie
    secure_app = make_app(db_url, base_url="https://odecty.example.org")
    c2 = login_client(secure_app, "reader")
    r = c2.get("/__test__/login", params={"email": "r3@example.org"})
    assert "secure" in r.headers["set-cookie"].lower()


def test_healthz_and_static(anon_client: TestClient) -> None:
    assert anon_client.get("/healthz").text == "ok"
    for p in (
        "/static/css/app.css",
        "/static/js/app.js",
        "/static/vendor/htmx-2.0.4.min.js",
        "/static/vendor/alpinejs-3.14.8.min.js",
        "/static/vendor/chart-4.4.7.umd.min.js",
    ):
        assert anon_client.get(p).status_code == 200, p
