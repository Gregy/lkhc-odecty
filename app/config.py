"""Application configuration loaded from environment variables (and `.env`)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

DEV_SECRET_KEY = "dev-insecure-secret-key-change-me"
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "testserver"})


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: Literal["development", "test", "production"] = "development"
    secret_key: str = DEV_SECRET_KEY
    base_url: str = "http://localhost:8000"
    data_dir: Path = Path("./data")
    # Optional explicit DB URL; defaults to sqlite file inside DATA_DIR.
    database_url: str | None = None

    google_client_id: str = ""
    google_client_secret: str = ""
    # Google Workspace domain whose accounts may log in. Installation data: set in .env only.
    # Empty = nobody can log in via Google (fail closed); required in production.
    allowed_domain: str = ""
    admin_emails: Annotated[list[str], NoDecode] = Field(default_factory=list)
    auth_dev_bypass: bool = False

    extractor: Literal["none", "openai_compat", "fake"] = "none"
    extractor_base_url: str = ""
    extractor_api_key: str = ""
    extractor_model: str = ""
    # [capture] request timeout (s; local CPU models are slow) and longer image side sent to the model.
    extractor_timeout: float = 120.0
    extractor_max_side: int = 1600
    # [capture] second, independent read of every photo that yielded digits; a value the second read
    # does not reproduce is flagged doubtful (low_confidence) for the review. Doubles the per-photo cost.
    extractor_verify: bool = False

    tz: str = "Europe/Prague"

    @field_validator("admin_emails", mode="before")
    @classmethod
    def _split_emails(cls, v: object) -> object:
        if isinstance(v, str):
            return [e.strip().lower() for e in v.replace(";", ",").split(",") if e.strip()]
        if isinstance(v, list):
            return [str(e).strip().lower() for e in v if str(e).strip()]
        return v

    @field_validator("allowed_domain")
    @classmethod
    def _lower_domain(cls, v: str) -> str:
        return v.strip().lower()

    @property
    def is_local(self) -> bool:
        """BASE_URL points at this machine (local development)."""
        return (urlsplit(self.base_url).hostname or "").lower() in LOCAL_HOSTS

    @model_validator(mode="after")
    def _check_production(self) -> Settings:
        # Fail closed: a deployment reachable under a real host name is treated as production even
        # when APP_ENV was forgotten (the default "development" must not accept the public dev key).
        strict = self.app_env == "production" or (self.app_env != "test" and not self.is_local)
        if strict:
            where = "APP_ENV=production" if self.app_env == "production" else f"BASE_URL={self.base_url}"
            if self.auth_dev_bypass:
                raise ValueError(f"AUTH_DEV_BYPASS must not be enabled with {where} (only for localhost)")
            if self.secret_key == DEV_SECRET_KEY or len(self.secret_key) < 32:
                raise ValueError(f"SECRET_KEY must be set to a random value (>= 32 chars) with {where}")
            if not self.allowed_domain:
                raise ValueError(f"ALLOWED_DOMAIN (Google Workspace domain allowed to log in) must be set with {where}")
        return self

    @property
    def sqlalchemy_url(self) -> str:
        if self.database_url:
            return self.database_url
        return f"sqlite:///{(self.data_dir / 'app.db').as_posix()}"

    @property
    def photos_dir(self) -> Path:
        return self.data_dir / "photos"

    @property
    def cookie_secure(self) -> bool:
        return self.base_url.startswith("https://")

    def is_admin_email(self, email: str) -> bool:
        return email.strip().lower() in self.admin_emails


@lru_cache
def get_settings() -> Settings:
    return Settings()
