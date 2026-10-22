"""Thin async client for the Frigate HTTP API.

Endpoint paths follow the Frigate documentation for the 0.18 series. Each
one must be checked against the running instance's /api/docs page; the
results are recorded in docs/VERIFICATION.md.
"""

import asyncio
import posixpath
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from urllib.parse import quote

import httpx

from .config import Settings


class FrigateError(Exception):
    """Frigate is unreachable or returned an unexpected response."""


class FrigateClient:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._settings = settings
        self._client = httpx.AsyncClient(
            base_url=settings.frigate_url.rstrip("/"),
            timeout=settings.frigate_timeout_seconds,
            verify=settings.frigate_verify_tls,
            transport=transport,
        )
        self._token: str | None = None
        self._login_lock = asyncio.Lock()

    @property
    def base_url(self) -> str:
        return str(self._client.base_url)

    @property
    def uses_auth(self) -> bool:
        return bool(self._settings.frigate_username and self._settings.frigate_password)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _login(self) -> None:
        # Only used against the authenticated port (8971).
        assert self._settings.frigate_password is not None
        resp = await self._client.post(
            "/api/login",
            json={
                "user": self._settings.frigate_username,
                "password": self._settings.frigate_password.get_secret_value(),
            },
        )
        if resp.status_code != 200:
            raise FrigateError(f"Frigate login failed with HTTP {resp.status_code}")
        token = resp.cookies.get("frigate_token")
        if not token:
            raise FrigateError("Frigate login response did not include a session token")
        self._token = token

    async def auth_headers(self) -> dict[str, str]:
        if not self.uses_auth:
            return {}
        async with self._login_lock:
            if self._token is None:
                await self._login()
        return {"Authorization": f"Bearer {self._token}"}

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        for attempt in range(2):
            headers = await self.auth_headers()
            try:
                resp = await self._client.request(method, path, headers=headers, **kwargs)
            except httpx.HTTPError as exc:
                raise FrigateError(f"Frigate request failed: {type(exc).__name__}") from exc
            if resp.status_code == 401 and self.uses_auth and attempt == 0:
                self._token = None  # expired; log in again once
                continue
            return resp
        return resp

    async def _json(self, method: str, path: str, **kwargs):
        resp = await self._request(method, path, **kwargs)
        if resp.status_code >= 400:
            raise FrigateError(f"Frigate {method} {path} returned HTTP {resp.status_code}")
        try:
            return resp.json()
        except ValueError as exc:
            raise FrigateError(f"Frigate {method} {path} returned non-JSON content") from exc

    async def version(self) -> str:
        resp = await self._request("GET", "/api/version")
        if resp.status_code != 200:
            raise FrigateError(f"Frigate version check returned HTTP {resp.status_code}")
        return resp.text.strip()

    async def cameras(self) -> dict[str, dict]:
        config = await self._json("GET", "/api/config")
        cameras = config.get("cameras")
        if not isinstance(cameras, dict):
            raise FrigateError("Frigate config has no cameras section")
        return cameras

    async def latest_jpeg(self, camera: str, height: int | None = None) -> bytes:
        params = {"h": height} if height else None
        resp = await self._request("GET", f"/api/{quote(camera, safe='')}/latest.jpg", params=params)
        if resp.status_code != 200 or not resp.headers.get("content-type", "").startswith("image/"):
            raise FrigateError(f"Frigate snapshot for {camera!r} returned HTTP {resp.status_code}")
        return resp.content

    async def start_export(self, camera: str, start: int, end: int, name: str) -> str:
        data = await self._json(
            "POST",
            f"/api/export/{quote(camera, safe='')}/start/{start}/end/{end}",
            json={"playback": "realtime", "name": name},
        )
        if not data.get("success", True) or not data.get("export_id"):
            raise FrigateError(f"Frigate export was not accepted: {data.get('message', 'no message')}")
        return str(data["export_id"])

    async def export_status(self, export_id: str) -> dict:
        return await self._json("GET", f"/api/exports/{quote(export_id, safe='')}")

    @asynccontextmanager
    async def stream_export_file(self, video_path: str) -> AsyncIterator[httpx.Response]:
        # Frigate reports exports as /media/frigate/exports/<file>.mp4 and
        # serves that directory at /exports/.
        file_name = posixpath.basename(video_path)
        if not file_name.endswith(".mp4"):
            raise FrigateError("Export video path is not an MP4 file")
        headers = await self.auth_headers()
        async with self._client.stream("GET", f"/exports/{quote(file_name)}", headers=headers) as resp:
            if resp.status_code != 200:
                raise FrigateError(f"Export download returned HTTP {resp.status_code}")
            yield resp

    def live_ws_url(self, camera: str) -> str:
        """go2rtc MSE websocket for a camera, as proxied by Frigate."""
        base = self.base_url.rstrip("/")
        ws_base = "wss://" + base[len("https://"):] if base.startswith("https://") else "ws://" + base.removeprefix("http://")
        return f"{ws_base}/live/mse/api/ws?src={quote(camera, safe='')}"
