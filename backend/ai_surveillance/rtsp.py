"""RTSP helpers: credential masking and an ffprobe-based stream check.

The probe is the evidence used to mark a camera or NVR stream as verified:
it reports what ffprobe actually decoded, nothing is assumed.
"""

import json
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass

# Greedy up to the last "@" before the path, so a password containing a raw
# "@" is still fully masked.
_CREDENTIALS = re.compile(r"(?P<scheme>[a-z][a-z0-9+.-]*://)[^/\s]+@", re.IGNORECASE)


def mask_credentials(text: str) -> str:
    """Replace any user:password@ part of URLs in text with ***@."""
    return _CREDENTIALS.sub(r"\g<scheme>***@", text)


@dataclass
class StreamInfo:
    codec: str
    width: int | None
    height: int | None
    fps: float | None
    profile: str | None


@dataclass
class ProbeResult:
    url: str  # always masked
    ok: bool
    video: list[StreamInfo]
    audio_codecs: list[str]
    error: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def _parse_rate(rate: str | None) -> float | None:
    if not rate or rate == "0/0":
        return None
    num, _, den = rate.partition("/")
    try:
        return round(float(num) / float(den or 1), 2)
    except (ValueError, ZeroDivisionError):
        return None


def parse_ffprobe_output(url: str, raw: str) -> ProbeResult:
    data = json.loads(raw or "{}")
    video, audio = [], []
    for stream in data.get("streams", []):
        if stream.get("codec_type") == "video":
            video.append(
                StreamInfo(
                    codec=stream.get("codec_name", "unknown"),
                    width=stream.get("width"),
                    height=stream.get("height"),
                    fps=_parse_rate(stream.get("avg_frame_rate")) or _parse_rate(stream.get("r_frame_rate")),
                    profile=stream.get("profile"),
                )
            )
        elif stream.get("codec_type") == "audio":
            audio.append(stream.get("codec_name", "unknown"))
    ok = bool(video)
    return ProbeResult(
        url=mask_credentials(url),
        ok=ok,
        video=video,
        audio_codecs=audio,
        error=None if ok else "No video stream found",
    )


def ffprobe_available() -> bool:
    return shutil.which("ffprobe") is not None


def probe(url: str, timeout_seconds: float = 15.0, transport: str = "tcp") -> ProbeResult:
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        return ProbeResult(mask_credentials(url), False, [], [], "ffprobe not found on PATH")
    cmd = [
        ffprobe, "-v", "error",
        # Only network streaming protocols; never local files or other schemes.
        "-protocol_whitelist", "rtsp,rtsps,rtp,srtp,tcp,udp,tls",
        "-rtsp_transport", transport,
        "-show_streams", "-of", "json",
        url,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_seconds, check=False)
    except subprocess.TimeoutExpired:
        return ProbeResult(mask_credentials(url), False, [], [], f"Timed out after {timeout_seconds}s")
    if proc.returncode != 0:
        message = mask_credentials(proc.stderr.strip()) or f"ffprobe exited with {proc.returncode}"
        return ProbeResult(mask_credentials(url), False, [], [], message[:500])
    return parse_ffprobe_output(url, proc.stdout)
