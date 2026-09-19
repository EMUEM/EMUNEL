"""EMUNEL API configuration loaded from environment variables.

Deployment philosophy (Railway/Docker friendly):
* Secrets — if the operator provides EMUNEL_SECRET_KEY / EMUNEL_JWT_SECRET_KEY /
  EMUNEL_ADMIN_PASSWORD they are validated strictly (fail-closed on weak
  values). If they are NOT provided, strong random ones are generated once,
  persisted under EMUNEL_DATA_ROOT (mode 0600) and reused across restarts, so
  a zero-config cloud deployment boots cleanly and sessions survive redeploys.
* Database — accepts EMUNEL_DATABASE_URL, DATABASE_URL or DATABASE_PRIVATE_URL
  (Railway Postgres convention), normalized to an async SQLAlchemy DSN.
  Default is embedded SQLite stored inside EMUNEL_DATA_ROOT so one mounted
  volume persists everything.
"""

import json
import logging
import os
import secrets
from pathlib import Path
from typing import List

from pydantic import Field, field_validator
from pydantic import AliasChoices
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger("emunel.api.config")

DATA_ROOT = Path(os.environ.get("EMUNEL_DATA_ROOT", "./emunel-data"))
DEFAULT_SQLITE = "sqlite+aiosqlite:///" + str(DATA_ROOT / "emunel.db")

_SECRET_ENV_VARS = (
    "EMUNEL_SECRET_KEY",
    "EMUNEL_JWT_SECRET_KEY",
    "EMUNEL_ADMIN_PASSWORD",
)
SECRETS_FILE = DATA_ROOT / "secrets.json"


def _normalize_database_url(raw: str) -> str:
    """Normalize a DSN into a SQLAlchemy async URL; fall back on garbage.

    Accepts: postgresql://, postgres://, postgresql+asyncpg://,
    sqlite:///..., sqlite://... . Anything unparseable (e.g. a foreign
    tool's ``file:/...`` DSN leaking into the environment) falls back to
    the embedded SQLite default rather than crashing startup.
    """
    value = (raw or "").strip()
    if value.startswith(("postgresql+asyncpg://", "sqlite+aiosqlite://")):
        return value
    if value.startswith("postgresql://"):
        return value.replace("postgresql://", "postgresql+asyncpg://", 1)
    if value.startswith("postgres://"):
        return value.replace("postgres://", "postgresql+asyncpg://", 1)
    if value.startswith("sqlite://"):
        return "sqlite+aiosqlite://" + value[len("sqlite://"):]
    if value:
        logger.warning(
            "DATABASE_URL %r is not a supported async DSN — falling back to %s",
            value[:60], DEFAULT_SQLITE,
        )
    return DEFAULT_SQLITE


def _load_or_create_bootstrap_secrets() -> dict:
    """Zero-config deploy support.

    Returns generated values ONLY for secrets the operator did not provide
    via the environment. Values are persisted to ``secrets.json`` (0600) under
    EMUNEL_DATA_ROOT so they survive restarts and redeploys. If persistence
    is impossible (read-only filesystem) the values stay ephemeral: the app
    still boots, with a loud warning, instead of crashing the deployment.
    """
    needed = [name for name in _SECRET_ENV_VARS if not os.environ.get(name, "").strip()]
    if not needed:
        return {}

    data: dict = {}
    try:
        loaded = json.loads(SECRETS_FILE.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            data = loaded
    except (OSError, json.JSONDecodeError):
        pass

    changed = False
    for env_name in needed:
        key = env_name.removeprefix("EMUNEL_").lower()  # secret_key / jwt_secret_key / admin_password
        value = data.get(key)
        # trust only values that already satisfy the production minimums
        minimum = 12 if env_name == "EMUNEL_ADMIN_PASSWORD" else 32
        if not isinstance(value, str) or len(value) < minimum:
            data[key] = (
                secrets.token_urlsafe(12) if env_name == "EMUNEL_ADMIN_PASSWORD"
                else secrets.token_urlsafe(48)
            )
            changed = True

    if changed or not SECRETS_FILE.exists():
        try:
            DATA_ROOT.mkdir(parents=True, exist_ok=True)
            tmp = SECRETS_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
            os.chmod(tmp, 0o600)
            os.replace(tmp, SECRETS_FILE)
        except OSError as exc:
            logger.warning(
                "could not persist generated secrets under %s (%s): they will be "
                "regenerated on every restart — set %s or attach a writable volume",
                DATA_ROOT, exc, "/".join(_SECRET_ENV_VARS[:1]),
            )
    logger.info(
        "secrets not provided via environment — using auto-generated ones "
        "(persisted in %s; override any time with %s)",
        SECRETS_FILE, ", ".join(needed),
    )
    return data


_BOOTSTRAP = _load_or_create_bootstrap_secrets()

_PLACEHOLDERS = {"change-me", "change-me-to-a-random-secret-key", "change-me-jwt-secret"}


class Settings(BaseSettings):
    app_name: str = "EMUNEL"
    debug: bool = False
    # generated+persisted when the env var is absent (see module docstring)
    secret_key: str = Field(
        default_factory=lambda: _BOOTSTRAP.get("secret_key") or "change-me-to-a-random-secret-key"
    )
    # accepts both EMUNEL_DATABASE_URL and the conventional DATABASE_URL /
    # DATABASE_PRIVATE_URL (Railway Postgres) variables
    database_url: str = Field(
        default=DEFAULT_SQLITE,
        validation_alias=AliasChoices(
            "EMUNEL_DATABASE_URL", "DATABASE_URL", "DATABASE_PRIVATE_URL", "database_url"
        ),
    )
    jwt_secret_key: str = Field(
        default_factory=lambda: _BOOTSTRAP.get("jwt_secret_key") or "change-me-jwt-secret"
    )
    jwt_algorithm: str = "HS256"
    jwt_expiry_minutes: int = 1440
    admin_username: str = "admin"
    admin_password: str = Field(
        default_factory=lambda: _BOOTSTRAP.get("admin_password") or "change-me"
    )
    cors_origins: List[str] = Field(default_factory=lambda: ["http://localhost:8080"])
    metrics_enabled: bool = True
    metrics_port: int = 9090
    rate_limit_per_minute: int = 60

    model_config = SettingsConfigDict(
        env_prefix="EMUNEL_",
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    @field_validator("database_url", mode="before")
    @classmethod
    def _validate_db_url(cls, v):
        return _normalize_database_url(v)

    @property
    def admin_password_generated(self) -> bool:
        """True when the bootstrap admin password was auto-generated (the
        seed routine prints it once so the operator can log in)."""
        return "admin_password" in _BOOTSTRAP

    def validate_runtime(self) -> None:
        """Reject unsafe values before accepting traffic.

        With the env vars unset the defaults are strong generated secrets, so
        reaching a placeholder/short value here means the operator explicitly
        configured something weak — that fails closed with instructions.
        """
        if self.debug:
            return
        if self.secret_key in _PLACEHOLDERS or len(self.secret_key) < 32:
            raise RuntimeError(
                "EMUNEL_SECRET_KEY must be at least 32 random characters "
                "(unset it to let EMUNEL generate one automatically)"
            )
        if self.jwt_secret_key in _PLACEHOLDERS or len(self.jwt_secret_key) < 32:
            raise RuntimeError(
                "EMUNEL_JWT_SECRET_KEY must be at least 32 random characters "
                "(unset it to let EMUNEL generate one automatically)"
            )
        if self.admin_password in _PLACEHOLDERS or len(self.admin_password) < 12:
            raise RuntimeError(
                "EMUNEL_ADMIN_PASSWORD must be at least 12 characters "
                "(unset it to let EMUNEL generate one and print it in the logs)"
            )
        if "*" in self.cors_origins:
            raise RuntimeError("CORS_ORIGINS must be an explicit origin list when credentials are enabled")
        if self.jwt_algorithm not in {"HS256", "HS384", "HS512"}:
            raise RuntimeError("Only HMAC JWT algorithms are supported")


settings = Settings()
