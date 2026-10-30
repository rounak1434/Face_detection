"""Camera/NVR device configuration: stream path handling, URL building and
connection tests.

A connection test reports "connected" only when ffprobe actually decoded a
video stream from the device. Anything less gets a weaker status.
"""

import asyncio
import ipaddress
import json
import logging
import re
import socket
from dataclasses import asdict, dataclass
from urllib.parse import quote, urlsplit

from .config import Settings
from .netguard import AddressNotAllowed, parse_networks, resolve_allowed
from .rtsp import ffprobe_available, mask_credentials, probe

log = logging.getLogger(__name__)

KINDS = ("ip_camera", "nvr", "synthetic")
SOURCE_LABELS = {
    "ip_camera": "IP camera (real device)",
    "nvr": "NVR (real device)",
    "synthetic": "Synthetic RTSP test source",
}

# Statuses shown in the UI. Only "connected" means a stream was decoded.
NOT_TESTED = "not_tested"
CONNECTED = "connected"
UNREACHABLE = "unreachable"
INVALID_CREDENTIALS = "invalid_credentials"
STREAM_NOT_FOUND = "stream_not_found"
REACHABLE_UNVERIFIED = "reachable_unverified"
NOT_ALLOWED = "address_not_allowed"
ERROR = "error"

TCP_CONNECT_TIMEOUT = 3.0

# RFC 3986 path + query characters. Spaces, quotes, backslashes, control
# characters and "#" are refused.
_PATH = re.compile(r"^/[A-Za-z0-9._~!$&'()*+,;=:@%/?-]{0,511}$")


def normalize_stream_path(value: str, host: str, port: int) -> str:
    """Accept "/path?query" or a full rtsp:// URL for this same device; return "/path?query".

    A full URL must point at the device's own host and port and must not
    carry credentials (they belong in the username/password fields).
    """
    value = value.strip()
    if value.lower().startswith(("rtsp://", "rtsps://")):
        parts = urlsplit(value)
        if parts.username is not None or parts.password is not None or "@" in parts.netloc:
            raise ValueError("Remove the username/password from the URL and use the credential fields instead")
        if (parts.hostname or "").lower() != host.lower().strip("[]"):
            raise ValueError("The stream URL must use the same host as the device")
        if (parts.port or 554) != port:
            raise ValueError("The stream URL must use the same RTSP port as the device")
        value = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    elif "://" in value:
        raise ValueError("Only rtsp:// URLs or a path starting with / are accepted")
    if not value.startswith("/"):
        value = "/" + value
    if not _PATH.match(value):
        raise ValueError("The stream path contains characters that are not allowed")
    return value


def _netloc(host: str, port: int) -> str:
    try:
        if ipaddress.ip_address(host).version == 6:
            return f"[{host}]:{port}"
    except ValueError:
        pass
    return f"{host}:{port}"


def build_url(host: str, port: int, path: str, username: str | None, password: str | None) -> str:
    userinfo = ""
    if username:
        userinfo = quote(username, safe="")
        if password:
            userinfo += ":" + quote(password, safe="")
        userinfo += "@"
    return f"rtsp://{userinfo}{_netloc(host, port)}{path}"


def display_url(host: str, port: int, path: str, has_credentials: bool) -> str:
    """URL safe to show in the browser: credentials are always replaced."""
    return f"rtsp://{'***@' if has_credentials else ''}{_netloc(host, port)}{path}"


def synthetic_target(settings: Settings) -> tuple[str, int, str]:
    parts = urlsplit(settings.synthetic_rtsp_url)
    path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    return parts.hostname or "", parts.port or 554, path


@dataclass
class StreamTest:
    status: str
    detail: str
    codec: str | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None


def classify_probe_error(message: str) -> str:
    text = message.lower()
    if "401" in text or "unauthorized" in text or "403" in text or "forbidden" in text:
        return INVALID_CREDENTIALS
    if "404" in text or "not found" in text:
        return STREAM_NOT_FOUND
    if any(s in text for s in ("connection refused", "no route to host", "timed out", "network is unreachable",
                               "host is down", "connection reset")):
        return UNREACHABLE
    return ERROR


async def _tcp_open(ip: str, port: int) -> bool:
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(ip, port), TCP_CONNECT_TIMEOUT)
    except (OSError, asyncio.TimeoutError):
        return False
    writer.close()
    try:
        await writer.wait_closed()
    except OSError:
        pass
    return True


async def _probe_stream(url: str, timeout: float) -> StreamTest:
    result = await asyncio.to_thread(probe, url, timeout)
    if result.ok:
        video = result.video[0]
        return StreamTest(CONNECTED, f"Video decoded: {video.codec} {video.width}x{video.height}",
                          video.codec, video.width, video.height, video.fps)
    detail = mask_credentials(result.error or "Unknown error")
    return StreamTest(classify_probe_error(detail), detail)


async def test_device(
    settings: Settings,
    kind: str,
    host: str,
    port: int,
    main_path: str,
    sub_path: str | None,
    username: str | None,
    password: str | None,
) -> dict[str, StreamTest]:
    """Test the configured streams of one device; returns {"main": ..., "sub": ...}."""
    streams = {"main": main_path, **({"sub": sub_path} if sub_path else {})}

    try:
        if kind == "synthetic":
            # Server-defined address (docker service name); not user input.
            infos = await asyncio.to_thread(socket.getaddrinfo, host, port, type=socket.SOCK_STREAM)
            ip = infos[0][4][0]
        else:
            allowed = parse_networks(settings.device_allowed_networks)
            ip = str(await asyncio.to_thread(resolve_allowed, host, allowed))
    except AddressNotAllowed as exc:
        return {name: StreamTest(NOT_ALLOWED, str(exc)) for name in streams}
    except OSError:
        return {name: StreamTest(UNREACHABLE, f"Host {host!r} could not be resolved") for name in streams}

    if not await _tcp_open(ip, port):
        return {name: StreamTest(UNREACHABLE, f"No answer on {_netloc(host, port)} (TCP connect failed)")
                for name in streams}

    if not ffprobe_available():
        detail = (f"Port {port} is open, but ffprobe is not installed on the server, "
                  "so the stream and credentials were not checked")
        return {name: StreamTest(REACHABLE_UNVERIFIED, detail) for name in streams}

    # Connect to the checked IP literal, not the name (no DNS rebinding).
    results = {}
    for name, path in streams.items():
        results[name] = await _probe_stream(build_url(ip, port, path, username, password),
                                            settings.device_test_timeout_seconds)
    return results


def results_to_json(results: dict[str, StreamTest]) -> str:
    return json.dumps({name: asdict(r) for name, r in results.items()})
