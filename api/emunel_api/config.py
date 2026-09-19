"""EMUNEL API configuration loaded from environment variables."""

import logging
from typing import List
from pydantic import Field, field_validator
from pydantic import AliasChoices
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger("emunel.api.config")

DEFAULT_SQLITE = "sqlite+aiosqlite:///./emunel.db"


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


class Settings(BaseSettings):
    app_name: str = "EMUNEL"
    debug: bool = False
    secret_key: str = "change-me-to-a-random-secret-key"
    # accepts both EMUNEL_DATABASE_URL and the conventional DATABASE_URL
    database_url: str = Field(
        default=DEFAULT_SQLITE,
        validation_alias=AliasChoices("EMUNEL_DATABASE_URL", "DATABASE_URL", "database_url"),
    )
    jwt_secret_key: str = "change-me-jwt-secret"
    jwt_algorithm: str = "HS256"
    jwt_expiry_minutes: int = 1440
    admin_username: str = "admin"
    admin_password: str = "change-me"
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

    def validate_runtime(self) -> None:
        """Reject unsafe production defaults before accepting traffic."""
        if self.debug:
            return
        placeholders = {"change-me", "change-me-to-a-random-secret-key", "change-me-jwt-secret"}
        if self.secret_key in placeholders or self.jwt_secret_key in placeholders:
            raise RuntimeError("EMUNEL_SECRET_KEY and JWT_SECRET_KEY must be replaced before production startup")
        if len(self.secret_key) < 32 or len(self.jwt_secret_key) < 32:
            raise RuntimeError("EMUNEL secrets must contain at least 32 characters")
        if self.admin_password in placeholders or len(self.admin_password) < 12:
            raise RuntimeError("ADMIN_PASSWORD must be a non-placeholder value of at least 12 characters")
        if "*" in self.cors_origins:
            raise RuntimeError("CORS_ORIGINS must be an explicit origin list when credentials are enabled")
        if self.jwt_algorithm not in {"HS256", "HS384", "HS512"}:
            raise RuntimeError("Only HMAC JWT algorithms are supported")


settings = Settings()
