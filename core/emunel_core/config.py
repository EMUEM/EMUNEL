"""EMUNEL Core — Configuration."""

import os
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class CoreConfig:
    """Core proxy configuration."""

    listen_host: str = "0.0.0.0"
    listen_port: int = 443
    tls_cert: Optional[str] = None
    tls_key: Optional[str] = None
    max_connections: int = 10000
    connection_timeout: float = 30.0
    buffer_size: int = 8192
    log_level: str = "INFO"

    # Quota
    default_traffic_limit_gb: Optional[float] = None
    enable_quota: bool = True

    # Database URL for quota lookups
    database_url: str = "sqlite+aiosqlite:///./emunel.db"

    @classmethod
    def from_env(cls) -> "CoreConfig":
        """Load configuration from environment variables."""
        return cls(
            listen_host=os.getenv("CORE_LISTEN_HOST", "0.0.0.0"),
            listen_port=int(os.getenv("CORE_LISTEN_PORT", "443")),
            tls_cert=os.getenv("CORE_TLS_CERT"),
            tls_key=os.getenv("CORE_TLS_KEY"),
            max_connections=int(os.getenv("CORE_MAX_CONNECTIONS", "10000")),
            connection_timeout=float(os.getenv("CORE_CONN_TIMEOUT", "30.0")),
            buffer_size=int(os.getenv("CORE_BUFFER_SIZE", "8192")),
            log_level=os.getenv("CORE_LOG_LEVEL", "INFO"),
            database_url=os.getenv(
                "DATABASE_URL", "sqlite+aiosqlite:///./emunel.db"
            ),
        )
