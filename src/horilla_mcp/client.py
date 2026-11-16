"""Async HTTP client for the Horilla HRMS REST API (JWT auth)."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx


class HorillaError(Exception):
    """An error returned by the Horilla API, with a message fit to show the model."""


class HorillaClient:
    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._username = username
        self._password = password
        self._access: str | None = None
        self._refresh: str | None = None
        self._login_lock = asyncio.Lock()
        self.me: dict[str, Any] | None = None
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/") + "/api/",
            timeout=timeout,
            headers={"Accept": "application/json"},
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    # ---- auth -----------------------------------------------------------

    async def login(self) -> None:
        resp = await self._http.post(
            "auth/login/", json={"username": self._username, "password": self._password}
        )
        if resp.status_code != 200:
            raise HorillaError(f"Login to Horilla failed ({resp.status_code}): {_body(resp)}")
        data = resp.json()
        self._access, self._refresh = data["access"], data.get("refresh")
        self.me = data.get("employee")

    async def _refresh_access(self) -> None:
        async with self._login_lock:
            if self._refresh:
                resp = await self._http.post("auth/refresh/", json={"refresh": self._refresh})
                if resp.status_code == 200:
                    data = resp.json()
                    self._access = data["access"]
                    self._refresh = data.get("refresh", self._refresh)
                    return
            await self.login()

    # ---- requests -------------------------------------------------------

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
    ) -> Any:
        if self._access is None:
            async with self._login_lock:
                if self._access is None:
                    await self.login()
        params = {k: v for k, v in (params or {}).items() if v is not None}
        for attempt in range(2):
            resp = await self._http.request(
                method,
                path,
                params=params,
                json=json,
                headers={"Authorization": f"Bearer {self._access}"},
            )
            if resp.status_code == 401 and attempt == 0:
                await self._refresh_access()
                continue
            break
        if resp.status_code >= 400:
            raise HorillaError(f"Horilla returned {resp.status_code} for {method} /api/{path}: {_body(resp)}")
        if not resp.content:
            return {"ok": True, "status": resp.status_code}
        try:
            return resp.json()
        except ValueError:
            return {"ok": True, "status": resp.status_code, "text": resp.text[:500]}

    async def get(self, path: str, **params: Any) -> Any:
        return await self.request("GET", path, params=params)


def _body(resp: httpx.Response) -> str:
    try:
        return str(resp.json())[:1000]
    except ValueError:
        return resp.text[:300]
