"""Shared test fixtures (reusable by all phases).

Source data: the **synthetic** workbook + mapping of ``tests/synthetic.py`` (fake tenants, serials and
readings with every quirk of the original spreadsheet), generated once per session:
- ``xlsx_path`` / ``mapping_path`` / ``import_mapping``: the synthetic XLSX, its TOML mapping, the parsed mapping
- ``real_private``: (xlsx, mapping) of the real installation from the private dir (``LKHC_PRIVATE_DIR``,
  default ``private/``); tests using it are skipped with a reason when it is not available

Databases (temp SQLite files, schema created by the real Alembic migrations):
- ``db_url`` / ``db``: empty migrated DB (URL / open Session)
- ``imported_db_url`` / ``imported_db``: copy of a DB pre-loaded by the importer from the synthetic XLSX
  **as exported** (``corrections=False``: the oracle compares every cell against the sheet; many tests use
  its dates)
- ``imported_corrected_db``: the same with the confirmed source corrections applied (like a real import)
  (the import runs once per test session; each test gets its own copy, so mutations are safe)

Apps & clients (FastAPI TestClient; app built by ``create_app(Settings(...))``):
- ``make_settings(**overrides)``: Settings for tests (APP_ENV=test, temp DATA_DIR)
- ``make_app(db_url, **settings_overrides)``: app factory
- ``app`` / ``imported_app``: app on the empty / imported DB
- ``anon_client``: not logged in
- ``admin_client`` / ``reader_client``: logged in on ``app`` (empty DB)
- ``imported_admin_client`` / ``imported_reader_client``: logged in on ``imported_app``
- ``login_client(app, role="admin", email=None)``: factory for more users (default ``<role>@example.org``)

Logged-in clients send the session's CSRF token as ``X-CSRF-Token`` header by default
(``client.csrf_token`` holds it, e.g. for form posts: ``data={"csrf_token": client.csrf_token}``).
"""

from __future__ import annotations

import shutil
import warnings
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from alembic.config import Config
from fastapi import FastAPI, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from alembic import command

warnings.filterwarnings("ignore", message=".*httpx2.*")
from fastapi.testclient import TestClient  # noqa: E402

from app.config import Settings  # noqa: E402

# Tests never read a developer's .env (e.g. APP_ENV=production without SECRET_KEY would make the
# module-level `app = create_app()` in app.main refuse to start). Set before app.main is imported.
Settings.model_config["env_file"] = None

from app.auth import session_login  # noqa: E402
from app.csrf import get_csrf_token  # noqa: E402
from app.db import make_engine, make_sessionmaker  # noqa: E402
from app.importer import import_workbook  # noqa: E402
from app.importer.mapping import ImportMapping, load_mapping  # noqa: E402
from app.main import create_app  # noqa: E402
from app.models import User  # noqa: E402
from app.private import ENV_VAR, MAPPING_FILE, private_dir  # noqa: E402
from tests import synthetic  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
REAL_XLSX_NAME = "Odečty elektřiny.xlsx"
TEST_DOMAIN = "example.org"
TEST_SECRET = "test-secret-key-0123456789abcdef0123456789"


def migrate(db_url: str) -> None:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", db_url)
    cfg.attributes["configure_logger"] = False
    command.upgrade(cfg, "head")


def sqlite_url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


@pytest.fixture(scope="session")
def _synthetic_private(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    return synthetic.write_private_dir(tmp_path_factory.mktemp("synthetic-private"))


@pytest.fixture(scope="session")
def xlsx_path(_synthetic_private: tuple[Path, Path]) -> Path:
    """The synthetic workbook (see tests/synthetic.py)."""
    return _synthetic_private[0]


@pytest.fixture(scope="session")
def mapping_path(_synthetic_private: tuple[Path, Path]) -> Path:
    return _synthetic_private[1]


@pytest.fixture(scope="session")
def import_mapping(mapping_path: Path) -> ImportMapping:
    return load_mapping(mapping_path)


def find_real_private() -> tuple[Path | None, Path | None, str]:
    """(xlsx, mapping, reason if incomplete) of the real installation's private dir."""
    d = private_dir()
    xlsx = d / REAL_XLSX_NAME
    if not xlsx.is_file():
        found = sorted(d.glob("*.xlsx")) if d.is_dir() else []
        xlsx = found[0] if len(found) == 1 else None
    mapping = d / MAPPING_FILE
    missing = [what for what, ok in (("XLSX", xlsx is not None), (MAPPING_FILE, mapping.is_file())) if not ok]
    reason = f"private data not available ({', '.join(missing)} missing in {d}; set {ENV_VAR})" if missing else ""
    return xlsx, (mapping if mapping.is_file() else None), reason


@pytest.fixture(scope="session")
def real_private() -> tuple[Path, ImportMapping]:
    """The real spreadsheet + mapping (opt-in: skipped visibly when the private dir is absent)."""
    xlsx, mapping, reason = find_real_private()
    if reason:
        pytest.skip(reason)
    assert xlsx is not None and mapping is not None
    return xlsx, load_mapping(mapping)


@pytest.fixture
def db_url(tmp_path: Path) -> str:
    url = sqlite_url(tmp_path / "test.db")
    migrate(url)
    return url


def import_template(path: Path, xlsx_path: Path, mapping: ImportMapping, *, corrections: bool) -> Path:
    url = sqlite_url(path)
    migrate(url)
    engine = make_engine(url)
    with make_sessionmaker(engine)() as s:
        import_workbook(s, xlsx_path, mapping=mapping, corrections=corrections)
        s.commit()
    with engine.connect() as conn:
        conn.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")
    engine.dispose()
    return path


@pytest.fixture(scope="session")
def _imported_template(
    tmp_path_factory: pytest.TempPathFactory, xlsx_path: Path, import_mapping: ImportMapping
) -> Path:
    # the sheet exactly as exported (see app/importer/corrections.py): oracle reference
    path = tmp_path_factory.mktemp("imported") / "imported.db"
    return import_template(path, xlsx_path, import_mapping, corrections=False)


@pytest.fixture(scope="session")
def _imported_corrected_template(
    tmp_path_factory: pytest.TempPathFactory, xlsx_path: Path, import_mapping: ImportMapping
) -> Path:
    path = tmp_path_factory.mktemp("corrected") / "corrected.db"
    return import_template(path, xlsx_path, import_mapping, corrections=True)


@pytest.fixture
def imported_corrected_db_url(tmp_path: Path, _imported_corrected_template: Path) -> str:
    dst = tmp_path / "corrected.db"
    shutil.copyfile(_imported_corrected_template, dst)
    return sqlite_url(dst)


@pytest.fixture
def imported_corrected_db(imported_corrected_db_url: str) -> Iterator[Session]:
    yield from _session(imported_corrected_db_url)


@pytest.fixture
def imported_db_url(tmp_path: Path, _imported_template: Path) -> str:
    dst = tmp_path / "imported.db"
    shutil.copyfile(_imported_template, dst)
    return sqlite_url(dst)


def _session(url: str) -> Iterator[Session]:
    engine = make_engine(url)
    s = make_sessionmaker(engine)()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


@pytest.fixture
def db(db_url: str) -> Iterator[Session]:
    yield from _session(db_url)


@pytest.fixture
def imported_db(imported_db_url: str) -> Iterator[Session]:
    yield from _session(imported_db_url)


@pytest.fixture
def make_settings(tmp_path: Path) -> Callable[..., Settings]:
    def factory(**overrides: object) -> Settings:
        values: dict[str, object] = {
            "app_env": "test",
            "secret_key": TEST_SECRET,
            "base_url": "http://testserver",
            "data_dir": tmp_path / "data",
            "allowed_domain": TEST_DOMAIN,
            "admin_emails": [f"boss@{TEST_DOMAIN}"],
            "auth_dev_bypass": False,
            "google_client_id": "test-client-id",
            "google_client_secret": "test-client-secret",
        }
        values.update(overrides)
        return Settings(_env_file=None, **values)  # type: ignore[call-arg]

    return factory


def _install_test_login(app: FastAPI) -> None:
    """Test-only route: GET /__test__/login?email=..&role=.. logs in (creating the user)."""
    if getattr(app.state, "_test_login", False):
        return

    def test_login(request: Request, email: str, role: str = "reader") -> dict[str, str]:
        with app.state.sessionmaker() as s:
            user = s.scalar(select(User).where(User.email == email))
            if user is None:
                user = User(email=email, name=email.split("@")[0], role=role, active=True)
                s.add(user)
            user.role = role
            s.commit()
            session_login(request, user)
        return {"csrf_token": get_csrf_token(request)}

    app.add_api_route("/__test__/login", test_login, methods=["GET"])
    app.state._test_login = True


@pytest.fixture
def make_app(make_settings: Callable[..., Settings]) -> Callable[..., FastAPI]:
    def factory(db_url: str, **overrides: object) -> FastAPI:
        app = create_app(make_settings(database_url=db_url, **overrides))
        _install_test_login(app)
        return app

    return factory


@pytest.fixture
def app(make_app: Callable[..., FastAPI], db_url: str) -> FastAPI:
    return make_app(db_url)


@pytest.fixture
def imported_app(make_app: Callable[..., FastAPI], imported_db_url: str) -> FastAPI:
    return make_app(imported_db_url)


@pytest.fixture
def login_client() -> Iterator[Callable[..., TestClient]]:
    clients: list[TestClient] = []

    def factory(app: FastAPI, role: str = "admin", email: str | None = None) -> TestClient:
        _install_test_login(app)
        c = TestClient(app)
        clients.append(c)
        r = c.get("/__test__/login", params={"email": email or f"{role}@{TEST_DOMAIN}", "role": role})
        assert r.status_code == 200, r.text
        token = r.json()["csrf_token"]
        c.headers["X-CSRF-Token"] = token
        c.csrf_token = token  # type: ignore[attr-defined]
        return c

    yield factory
    for c in clients:
        c.close()


@pytest.fixture
def anon_client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


@pytest.fixture
def admin_client(app: FastAPI, login_client: Callable[..., TestClient]) -> TestClient:
    return login_client(app, "admin")


@pytest.fixture
def reader_client(app: FastAPI, login_client: Callable[..., TestClient]) -> TestClient:
    return login_client(app, "reader")


@pytest.fixture
def imported_admin_client(imported_app: FastAPI, login_client: Callable[..., TestClient]) -> TestClient:
    return login_client(imported_app, "admin")


@pytest.fixture
def imported_reader_client(imported_app: FastAPI, login_client: Callable[..., TestClient]) -> TestClient:
    return login_client(imported_app, "reader")
