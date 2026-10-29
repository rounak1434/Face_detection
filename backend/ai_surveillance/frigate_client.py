"""Thin async client for the Frigate HTTP API (Frigate 0.18.x).

Endpoints, request bodies and response shapes were checked against the
Frigate v0.18.2 source code; docs/FRIGATE-API.md lists each one with the
file it comes from. Behaviour against a running instance is recorded in
docs/VERIFICATION.md.
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


class FrigateNotFound(FrigateError):
    """Frigate answered 404 for the requested resource."""


class FrigateConfigRejected(FrigateError):
    """Frigate refused a config in /api/config/save (HTTP 400); its config file is unchanged."""


def _message(resp: httpx.Response) -> str | None:
    """Frigate error bodies look like {"success": false, "message": "..."}."""
    try:
        body = resp.json()
    except ValueError:
        return None
    if isinstance(body, dict) and isinstance(body.get("message"), str):
        return body["message"][:300]
    return None


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
        # Only used against the authenticated port (8971). Frigate answers
        # 200 with an empty body and sets the JWT in the "frigate_token"
        # cookie (auth.cookie_name default); later calls send it as Bearer.
        assert self._settings.frigate_password is not None
        try:
            resp = await self._client.post(
                "/api/login",
                json={
                    "user": self._settings.frigate_username,
                    "password": self._settings.frigate_password.get_secret_value(),
                },
            )
        except httpx.HTTPError as exc:
            raise FrigateError(f"Frigate login failed: {type(exc).__name__}") from exc
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

    async def _request(self, method: str, path: str, headers: dict[str, str] | None = None, **kwargs) -> httpx.Response:
        extra = headers or {}
        for attempt in range(2):
            headers = {**extra, **await self.auth_headers()}
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
            detail = _message(resp)
            text = f"Frigate {method} {path} returned HTTP {resp.status_code}" + (f": {detail}" if detail else "")
            raise (FrigateNotFound if resp.status_code == 404 else FrigateError)(text)
        try:
            return resp.json()
        except ValueError as exc:
            raise FrigateError(f"Frigate {method} {path} returned non-JSON content") from exc

    async def version(self) -> str:
        # Public endpoint; plain-text body.
        resp = await self._request("GET", "/api/version")
        if resp.status_code != 200:
            raise FrigateError(f"Frigate version check returned HTTP {resp.status_code}")
        return resp.text.strip()

    async def cameras(self) -> dict[str, dict]:
        # /api/config masks credentials in ffmpeg input paths, but we still
        # never forward those paths to the browser.
        config = await self._json("GET", "/api/config")
        cameras = config.get("cameras")
        if not isinstance(cameras, dict):
            raise FrigateError("Frigate config has no cameras section")
        return cameras

    async def is_up(self) -> bool:
        """True when Frigate answers /api/version (public); False while it is down or restarting."""
        try:
            await self.version()
        except FrigateError:
            return False
        return True

    async def config_raw(self) -> str:
        """The saved config file, unredacted (admin role). Frigate 0.18.2 sends the text
        through a JSONResponse with media type text/plain, so the body is a JSON string."""
        resp = await self._request("GET", "/api/config/raw")
        if resp.status_code != 200:
            detail = _message(resp)
            raise FrigateError(f"Frigate config/raw returned HTTP {resp.status_code}" + (f": {detail}" if detail else ""))
        try:
            text = resp.json()
        except ValueError:
            text = resp.text
        if not isinstance(text, str):
            raise FrigateError("Frigate config/raw did not return text")
        return text

    async def save_config(self, text: str, restart: bool) -> str:
        """POST /api/config/save?save_option=restart|saveonly with the whole YAML (admin role).

        Frigate validates first; 400 means it refused the config and kept its
        file (FrigateConfigRejected). With restart, Frigate restarts itself
        right after writing, so the response can be lost (FrigateError).
        """
        resp = await self._request(
            "POST", "/api/config/save",
            params={"save_option": "restart" if restart else "saveonly"},
            content=text.encode("utf-8"), headers={"Content-Type": "text/plain"},
        )
        detail = _message(resp) or ""
        if resp.status_code == 400:
            raise FrigateConfigRejected(detail or "Frigate refused the configuration")
        if resp.status_code != 200:
            raise FrigateError(f"Frigate config/save returned HTTP {resp.status_code}" + (f": {detail}" if detail else ""))
        return detail

    async def restart(self) -> None:
        """POST /api/restart (admin role). The response can be lost while Frigate stops."""
        resp = await self._request("POST", "/api/restart")
        if resp.status_code != 200:
            detail = _message(resp)
            raise FrigateError(f"Frigate restart returned HTTP {resp.status_code}" + (f": {detail}" if detail else ""))

    async def camera_fps(self, camera: str) -> float | None:
        """Frames per second Frigate is receiving, from /api/stats; None if not reported."""
        stats = await self._json("GET", "/api/stats")
        value = ((stats.get("cameras") or {}).get(camera) or {}).get("camera_fps")
        return float(value) if isinstance(value, int | float) else None

    async def latest_jpeg(self, camera: str, height: int | None = None) -> tuple[bytes, bool]:
        """Latest frame and whether Frigate flagged it as an offline fallback.

        When the camera is offline Frigate still answers 200, with either a
        recent preview frame (header X-Frigate-Offline: true) or a generic
        error image. Callers that store evidence must also check camera_fps.
        """
        params = {"height": height} if height else None
        resp = await self._request("GET", f"/api/{quote(camera, safe='')}/latest.jpg", params=params)
        if resp.status_code != 200 or not resp.headers.get("content-type", "").startswith("image/"):
            detail = _message(resp)
            raise FrigateError(
                f"Frigate snapshot for {camera!r} returned HTTP {resp.status_code}" + (f": {detail}" if detail else "")
            )
        offline = resp.headers.get("x-frigate-offline", "").lower() == "true"
        return resp.content, offline

    async def start_export(self, camera: str, start: int, end: int, name: str) -> str:
        """Queue an export of recordings; returns the job/export id.

        Frigate answers 202 {"success": true, "export_id": ..., "status": "queued"},
        or 400 "No recordings found for time range", 404 unknown camera,
        503 queue full.
        """
        data = await self._json(
            "POST",
            f"/api/export/{quote(camera, safe='')}/start/{start}/end/{end}",
            json={"source": "recordings", "name": name},
        )
        if not data.get("success") or not data.get("export_id"):
            raise FrigateError(f"Frigate export was not accepted: {data.get('message', 'no message')}")
        return str(data["export_id"])

    async def export_job(self, export_id: str) -> dict | None:
        """Job state (queued/running/success/failed/cancelled); None once Frigate forgets it.

        Frigate drops finished jobs from memory a few seconds after they end.
        """
        try:
            return await self._json("GET", f"/api/jobs/export/{quote(export_id, safe='')}")
        except FrigateNotFound:
            return None

    async def export_record(self, export_id: str) -> dict | None:
        """The stored export row (video_path, in_progress); None if it does not exist."""
        try:
            return await self._json("GET", f"/api/exports/{quote(export_id, safe='')}")
        except FrigateNotFound:
            return None

    async def delete_export(self, export_id: str) -> None:
        """Delete an export (video, thumbnail, row) in Frigate; an unknown id counts as deleted.

        Frigate 0.18.2 has no single-export DELETE; it offers POST /api/exports/delete
        {"ids": [...]} (admin role): 200, 404 if an id is unknown, 400 while ffmpeg
        still writes the file. Raises FrigateError otherwise.
        """
        try:
            await self._json("POST", "/api/exports/delete", json={"ids": [export_id]})
        except FrigateNotFound:
            return

    @asynccontextmanager
    async def stream_export_file(self, video_path: str) -> AsyncIterator[httpx.Response]:
        # Frigate reports exports as /media/frigate/exports/<file>.mp4; its
        # nginx serves /media/frigate at the root, so the URL is /exports/<file>.
        file_name = posixpath.basename(video_path)
        if not file_name.endswith(".mp4"):
            raise FrigateError("Export video path is not an MP4 file")
        headers = await self.auth_headers()
        try:
            async with self._client.stream("GET", f"/exports/{quote(file_name)}", headers=headers) as resp:
                if resp.status_code != 200:
                    raise FrigateError(f"Export download returned HTTP {resp.status_code}")
                yield resp
        except httpx.HTTPError as exc:
            raise FrigateError(f"Export download failed: {type(exc).__name__}") from exc

    def live_ws_url(self, stream: str) -> str:
        """go2rtc MSE websocket for a go2rtc stream name, via Frigate's nginx."""
        base = self.base_url.rstrip("/")
        ws_base = "wss://" + base[len("https://"):] if base.startswith("https://") else "ws://" + base.removeprefix("http://")
        return f"{ws_base}/live/mse/api/ws?src={quote(stream, safe='')}"


def live_stream_name(camera: str, camera_config: dict) -> str:
    """go2rtc stream used for live view.

    Frigate's own UI uses the first entry of `live.streams`; without that
    mapping the go2rtc stream is expected to share the camera's name.
    """
    streams = (camera_config.get("live") or {}).get("streams")
    if isinstance(streams, dict) and streams:
        first = next(iter(streams.values()))
        if isinstance(first, str) and first:
            return first
    return camera
