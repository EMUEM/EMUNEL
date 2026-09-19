"""EMUNEL API configuration loaded from environment variables."""

from typing import List
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "EMUNEL"
    debug: bool = False
    secret_key: str = "change-me-to-a-random-secret-key"
    database_url: str = "sqlite+aiosqlite:///./emunel.db"
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
        env_prefix="",
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

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
