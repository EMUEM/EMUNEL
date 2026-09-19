"""EMUNEL Core — Traffic quota enforcement."""

import logging
from typing import Optional

from .config import CoreConfig

logger = logging.getLogger("emunel.core.quota")


class QuotaResult:
    """Result of a quota check."""

    __slots__ = ("allowed", "remaining_bytes", "reason")

    def __init__(
        self,
        allowed: bool,
        remaining_bytes: Optional[int] = None,
        reason: str = "",
    ):
        self.allowed = allowed
        self.remaining_bytes = remaining_bytes
        self.reason = reason


class QuotaManager:
    """Enforce per-user traffic quotas."""

    def __init__(self, config: CoreConfig) -> None:
        self.config = config
        self._cache: dict[str, dict] = {}

    async def check(self, user_id: str) -> QuotaResult:
        """Check if user has remaining quota.

        In production this queries the database; here we provide
        the interface and a simple in-memory cache fallback.
        """
        if not self.config.enable_quota:
            return QuotaResult(allowed=True)

        user_quota = self._cache.get(user_id)
        if user_quota is None:
            # Load from database in production
            return QuotaResult(allowed=True)

        limit = user_quota.get("traffic_limit_bytes")
        used = user_quota.get("traffic_used_bytes", 0)

        if limit is None:
            return QuotaResult(allowed=True)

        remaining = limit - used
        if remaining <= 0:
            return QuotaResult(
                allowed=False,
                remaining_bytes=0,
                reason="Traffic quota exceeded.",
            )

        return QuotaResult(allowed=True, remaining_bytes=remaining)

    async def consume(self, user_id: str, bytes_count: int) -> None:
        """Record traffic consumption."""
        if user_id in self._cache:
            self._cache[user_id]["traffic_used_bytes"] = (
                self._cache[user_id].get("traffic_used_bytes", 0) + bytes_count
            )

    async def load_user_quota(self, user_id: str, quota_data: dict) -> None:
        """Load user quota data into cache."""
        self._cache[user_id] = quota_data

    async def evict(self, user_id: str) -> None:
        """Remove user from quota cache."""
        self._cache.pop(user_id, None)
