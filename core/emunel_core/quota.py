"""EMUNEL Core — fail-closed traffic quota enforcement."""

import logging
from typing import Optional
from .config import CoreConfig

logger = logging.getLogger("emunel.core.quota")

class QuotaResult:
    __slots__ = ("allowed", "remaining_bytes", "reason")
    def __init__(self, allowed: bool, remaining_bytes: Optional[int] = None, reason: str = ""):
        self.allowed = allowed
        self.remaining_bytes = remaining_bytes
        self.reason = reason

class QuotaManager:
    """Enforce per-credential quotas.

    Unknown credentials are denied. A control plane must explicitly load a link
    before the Core can open an outbound connection; this prevents an empty or
    unavailable state store from turning the listener into an open proxy.
    """
    def __init__(self, config: CoreConfig) -> None:
        self.config = config
        self._cache: dict[str, dict] = {}

    async def check(self, user_id: str) -> QuotaResult:
        if not user_id or not self.config.enable_quota:
            return QuotaResult(allowed=not self.config.require_registered_credentials,
                               reason="Credential registration is required." if self.config.require_registered_credentials else "")
        user_quota = self._cache.get(user_id)
        if user_quota is None:
            return QuotaResult(False, 0, "Unknown credential.") if self.config.require_registered_credentials else QuotaResult(True)
        if not user_quota.get("active", True):
            return QuotaResult(False, 0, "Credential disabled.")
        expires_at = user_quota.get("expires_at")
        if expires_at is not None and expires_at <= __import__("time").time():
            return QuotaResult(False, 0, "Credential expired.")
        limit = user_quota.get("traffic_limit_bytes")
        used = max(0, int(user_quota.get("traffic_used_bytes", 0)))
        if limit is None or int(limit) <= 0:
            return QuotaResult(True)
        remaining = int(limit) - used
        return QuotaResult(True, remaining) if remaining > 0 else QuotaResult(False, 0, "Traffic quota exceeded.")

    async def consume(self, user_id: str, bytes_count: int) -> None:
        if bytes_count <= 0 or user_id not in self._cache:
            return
        record = self._cache[user_id]
        record["traffic_used_bytes"] = max(0, int(record.get("traffic_used_bytes", 0))) + bytes_count

    async def load_user_quota(self, user_id: str, quota_data: dict) -> None:
        self._cache[user_id] = dict(quota_data)

    async def evict(self, user_id: str) -> None:
        self._cache.pop(user_id, None)
