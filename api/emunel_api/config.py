"""EMUNEL API — Configuration via environment variables."""

import os
from typing import List, Optional

from pydantic_settings import BaseSettings
from pydantic import Field


class Settings(BaseSettings):
    """Application settings loaded from environment."""

    # General
    app_name: str = "EMUNEL"
    debug: bool = False
    secret_key: str = "change-me-to-a-random-secret-key"

    # Database
    database_url: str = "sqlite+aiosqlite:///./emunel.db"

    # JWT
    jwt_secret_key: str = "change-me-jwt-secret"
    jwt_algorithm: str = "HS256"
    jwt_expiry_minutes: int = 1440  # 24 hours

    # Admin
    admin_username: str = "admin"
    admin_password: str = "change-me"

    # CORS
    cors_origins: List[str] = ["*"]

    # Metrics
    metrics_enabled: bool = True
    metrics_port: int = 9090

    # Rate limiting
    rate_limit_per_minute: int = 60

    class Config:
        env_prefix = ""
        env_file = ".env"
        env_file_encoding = "utf-8"
        case_sensitive = False


settings = Settings()
