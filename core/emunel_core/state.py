"""EMUNEL Core — Connection state tracker."""

import time
import uuid
import logging
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple

logger = logging.getLogger("emunel.core.state")


@dataclass
class Connection:
    """Single connection state."""

    id: str
    peer: Tuple[str, int]
    created_at: float
    protocol: Optional[str] = None
    user_id: Optional[str] = None
    bytes_up: int = 0
    bytes_down: int = 0


class ConnectionState:
    """Thread-safe connection state manager."""

    def __init__(self) -> None:
        self._connections: Dict[str, Connection] = {}

    @property
    def active_count(self) -> int:
        return len(self._connections)

    def register(self, peer: Tuple[str, int]) -> str:
        """Register a new connection and return its ID."""
        conn_id = uuid.uuid4().hex[:12]
        self._connections[conn_id] = Connection(
            id=conn_id,
            peer=peer,
            created_at=time.time(),
        )
        return conn_id

    def unregister(self, conn_id: str) -> Optional[Connection]:
        """Remove a connection and return its final state."""
        return self._connections.pop(conn_id, None)

    def update_traffic(self, conn_id: str, bytes_up: int, bytes_down: int) -> None:
        """Update traffic counters for a connection."""
        conn = self._connections.get(conn_id)
        if conn:
            conn.bytes_up += bytes_up
            conn.bytes_down += bytes_down

    def set_protocol(self, conn_id: str, protocol: str) -> None:
        """Set the detected protocol for a connection."""
        conn = self._connections.get(conn_id)
        if conn:
            conn.protocol = protocol

    def set_user(self, conn_id: str, user_id: str) -> None:
        """Associate a user with a connection."""
        conn = self._connections.get(conn_id)
        if conn:
            conn.user_id = user_id

    def get_stats(self) -> dict:
        """Return aggregate connection statistics."""
        total_up = sum(c.bytes_up for c in self._connections.values())
        total_down = sum(c.bytes_down for c in self._connections.values())
        protocols: Dict[str, int] = {}
        for c in self._connections.values():
            proto = c.protocol or "unknown"
            protocols[proto] = protocols.get(proto, 0) + 1

        return {
            "active_connections": self.active_count,
            "total_bytes_up": total_up,
            "total_bytes_down": total_down,
            "protocols": protocols,
        }

    async def close_all(self) -> None:
        """Clean up all connections."""
        count = len(self._connections)
        self._connections.clear()
        logger.info("Closed %d connections.", count)
