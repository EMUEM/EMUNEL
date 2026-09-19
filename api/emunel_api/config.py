"""EMUNEL API configuration loaded from environment variables.

Deployment philosophy (Lunel reference parity — zero-config, never crash):
* Secrets — if the operator provides EMUNEL_SECRET_KEY / EMUNEL_JWT_SECRET_KEY
  they are used as-is. If NOT provided, strong random ones are generated once,
  persisted under EMUNEL_DATA_ROOT (mode 0600) and reused across restarts, so
  a zero-config cloud deployment boots cleanly and sessions survive redeploys.
* Admin — default credentials are admin/admin (exactly like the reference
  project); the console warns loudly to change the password after first
  sign-in. Override with EMUNEL_ADMIN_USERNAME / EMUNEL_ADMIN_PASSWORD.
* Database — accepts EMUNEL_DATABASE_URL, DATABASE_URL or DATABASE_PRIVATE_URL
  (Railway Postgres convention), normalized to an async SQLAlchemy DSN.
  Default is embedded SQLite stored inside EMUNEL_DATA_ROOT so one mounted
  volume persists everything.
* Startup NEVER raises on weak/missing configuration — misconfiguration logs a
  CRITICAL warning and keeps the container alive (crash-looping helps no one);
  only genuinely broken states (e.g. an unwritable state root) surface per-
  request. Secrets defaults are always strong generated values.
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

# generated-when-missing secrets (admin credentials are NOT here: they use
# the fixed reference-style admin/admin default instead of random values)
_SECRET_ENV_VARS = (
    "EMUNEL_SECRET_KEY",
    "EMUNEL_JWT_SECRET_KEY",
)
SECRETS_FILE = DATA_ROOT / "secrets.json"

# reference-parity default credentials (Lunel seeds admin/admin too)
DEFAULT_ADMIN_USERNAME = "admin"
DEFAULT_ADMIN_PASSWORD = "admin"

_PLACEHOLDERS = {
    "change-me", "change-me-to-a-random-secret-key", "change-me-jwt-secret",
    "change-me-at-least-12-chars", "admin123", "password", "123456",
}


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
    """Zero-config deploy support (auto-provisioned variables).

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
        key = env_name.removeprefix("EMUNEL_").lower()  # secret_key / jwt_secret_key
        value = data.get(key)
        if not isinstance(value, str) or len(value) < 32:
            data[key] = secrets.token_urlsafe(48)
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
                DATA_ROOT, exc, _SECRET_ENV_VARS[0],
            )
    logger.info(
        "secrets not provided via environment — auto-provisioned and persisted in %s "
        "(override any time with %s)",
        SECRETS_FILE, ", ".join(needed),
    )
    return data


_BOOTSTRAP = _load_or_create_bootstrap_secrets()


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
    admin_username: str = DEFAULT_ADMIN_USERNAME
    admin_password: str = DEFAULT_ADMIN_PASSWORD
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
    def using_default_admin(self) -> bool:
        """True when the reference-style default credentials are in effect —
        the console warns about this until the password is changed."""
        return self.admin_password == DEFAULT_ADMIN_PASSWORD

    def validate_runtime(self) -> None:
        """Surface misconfiguration LOUDLY but never crash the container.

        The reference project seeds admin/admin and tolerates any config at
        boot; a crash-looping deployment is strictly worse than a degraded
        one, so weak values log CRITICAL warnings and startup continues.
        (Defaults for secrets are always strong generated values, so the
        no-env-var path is safe by construction.)
        """
        if self.debug:
            return
        if self.secret_key in _PLACEHOLDERS or len(self.secret_key) < 32:
            logger.critical(
                "EMUNEL_SECRET_KEY is weak (%d chars) — sessions may be forgeable. "
                "Unset it to auto-generate a strong one.", len(self.secret_key),
            )
        if self.jwt_secret_key in _PLACEHOLDERS or len(self.jwt_secret_key) < 32:
            logger.critical(
                "EMUNEL_JWT_SECRET_KEY is weak (%d chars) — tokens may be forgeable. "
                "Unset it to auto-generate a strong one.", len(self.jwt_secret_key),
            )
        if self.using_default_admin:
            logger.warning(
                "admin account uses the default password — change it after first login"
            )
        if "*" in self.cors_origins:
            logger.warning("CORS_ORIGINS contains '*' — restrict it to real origins")
        if self.jwt_algorithm not in {"HS256", "HS384", "HS512"}:
            logger.critical(
                "JWT algorithm %r is not HMAC — falling back to HS256", self.jwt_algorithm,
            )
            self.jwt_algorithm = "HS256"


settings = Settings()
