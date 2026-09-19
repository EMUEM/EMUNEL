"""EMUNEL CoreClient — typed client for a running Core's management API.

Thin async wrapper around the /core/api/* endpoints (links CRUD, stats,
connections, logs, metrics, share). The per-instance bearer token never
leaves this module boundary.
"""

from __future__ import annotations

import logging
from typing import Optional

import httpx

logger = logging.getLogger("emunel.api.core_client")

DEFAULT_TIMEOUT = httpx.Timeout(connect=3.0, read=8.0, write=8.0, pool=3.0)


class CoreUnavailable(RuntimeError):
    pass


class CoreClient:
    def __init__(self, port: int, token: str, timeout: httpx.Timeout = DEFAULT_TIMEOUT):
        self._base = f"http://127.0.0.1:{port}"
        self._token = token
        self._timeout = timeout

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self._base,
            headers={"Authorization": f"Bearer {self._token}"},
            timeout=self._timeout,
        )

    async def _call(self, method: str, path: str, **kwargs) -> httpx.Response:
        try:
            async with self._client() as client:
                return await client.request(method, path, **kwargs)
        except (httpx.HTTPError, OSError) as exc:
            raise CoreUnavailable(f"core unreachable: {type(exc).__name__}") from exc

    # ---- health (public endpoints, no token needed) ---------------------
    async def health(self) -> Optional[dict]:
        try:
            async with httpx.AsyncClient(base_url=self._base, timeout=3.0) as client:
                r = await client.get("/health")
                return r.json() if r.status_code == 200 else None
        except (httpx.HTTPError, OSError):
            return None

    # ---- links -----------------------------------------------------------
    async def create_link(self, body: dict) -> dict:
        r = await self._call("POST", "/core/api/links", json=body)
        if r.status_code != 200:
            raise CoreUnavailable(f"link create rejected: {r.text[:200]}")
        return r.json()

    async def update_link(self, uuid: str, body: dict) -> dict:
        r = await self._call("PATCH", f"/core/api/links/{uuid}", json=body)
        if r.status_code != 200:
            raise CoreUnavailable(f"link update rejected: {r.text[:200]}")
        return r.json()

    async def delete_link(self, uuid: str) -> None:
        r = await self._call("DELETE", f"/core/api/links/{uuid}")
        if r.status_code != 200:
            raise CoreUnavailable(f"link delete rejected: {r.text[:200]}")

    async def list_links(self) -> list[dict]:
        r = await self._call("GET", "/core/api/links")
        if r.status_code != 200:
            raise CoreUnavailable("link list failed")
        return r.json().get("links", [])

    async def share_links(self, host: str, uuids: Optional[list[str]] = None,
                          path_prefix: str = "") -> list[dict]:
        r = await self._call("POST", "/core/api/share", json={
            "host": host,
            "uuids": uuids or [],
            "path_prefix": path_prefix,
        })
        if r.status_code != 200:
            raise CoreUnavailable(f"share failed: {r.text[:200]}")
        return r.json().get("links", [])

    # ---- stats / connections / logs / metrics -----------------------------
    async def stats(self) -> dict:
        r = await self._call("GET", "/core/api/stats")
        if r.status_code != 200:
            raise CoreUnavailable("stats failed")
        return r.json()

    async def connections(self) -> dict:
        r = await self._call("GET", "/core/api/connections")
        if r.status_code != 200:
            raise CoreUnavailable("connections failed")
        return r.json()

    async def logs(self, limit: int = 200) -> list[dict]:
        r = await self._call("GET", f"/core/api/logs?limit={max(1, min(limit, 500))}")
        if r.status_code != 200:
            raise CoreUnavailable("logs failed")
        return r.json().get("logs", [])

    async def metrics(self) -> dict:
        r = await self._call("GET", "/core/api/metrics")
        if r.status_code != 200:
            raise CoreUnavailable("metrics failed")
        return r.json()

    async def flush_state(self) -> None:
        await self._call("POST", "/core/api/state/flush")
