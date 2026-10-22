import json

import pytest
from starlette.websockets import WebSocketDisconnect

from ai_surveillance.config import Settings
from ai_surveillance.frigate_client import FrigateClient
from ai_surveillance.routes.live import origin_allowed
from ai_surveillance.rtsp import mask_credentials, parse_ffprobe_output


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("rtsp://admin:p4ss@192.168.1.10:554/cam/realmonitor?channel=1&subtype=0",
         "rtsp://***@192.168.1.10:554/cam/realmonitor?channel=1&subtype=0"),
        ("rtsp://user:p@ss@host/stream", "rtsp://***@host/stream"),
        ("error opening rtsp://a:b@h/x and rtsp://c:d@h/y", "error opening rtsp://***@h/x and rtsp://***@h/y"),
        ("rtsp://192.168.1.10/stream", "rtsp://192.168.1.10/stream"),
    ],
)
def test_mask_credentials(raw, expected):
    assert mask_credentials(raw) == expected


def test_parse_ffprobe_output():
    raw = json.dumps({"streams": [
        {"codec_type": "video", "codec_name": "h264", "profile": "Main", "width": 1280, "height": 720,
         "avg_frame_rate": "15/1"},
        {"codec_type": "audio", "codec_name": "pcm_alaw"},
    ]})
    result = parse_ffprobe_output("rtsp://u:pw@cam/s", raw)
    assert result.ok
    assert result.url == "rtsp://***@cam/s"
    assert result.video[0].codec == "h264"
    assert (result.video[0].width, result.video[0].height, result.video[0].fps) == (1280, 720, 15.0)
    assert result.audio_codecs == ["pcm_alaw"]


def test_parse_ffprobe_without_video():
    result = parse_ffprobe_output("rtsp://cam/s", json.dumps({"streams": []}))
    assert not result.ok
    assert result.error


@pytest.mark.parametrize(
    "origin, host, allowed",
    [
        ("https://surveillance.lan", "surveillance.lan", True),
        ("https://evil.example", "surveillance.lan", False),
        (None, "surveillance.lan", False),
        ("https://surveillance.lan:8443", "surveillance.lan:8443", True),
    ],
)
def test_origin_check(origin, host, allowed):
    assert origin_allowed(origin, host) is allowed


def test_live_ws_url():
    http = FrigateClient(Settings(frigate_url="http://frigate:5000", _env_file=None))
    assert http.live_ws_url("entrance") == "ws://frigate:5000/live/mse/api/ws?src=entrance"
    https = FrigateClient(Settings(frigate_url="https://frigate:8971/", _env_file=None))
    assert https.live_ws_url("front door") == "wss://frigate:8971/live/mse/api/ws?src=front%20door"


def test_live_rejects_anonymous(client):
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/api/cameras/entrance/live", headers={"origin": "http://testserver"}) as ws:
            ws.receive_text()
    assert exc.value.code == 1008


def test_live_rejects_foreign_origin(signed_in):
    with pytest.raises(WebSocketDisconnect) as exc:
        with signed_in.websocket_connect("/api/cameras/entrance/live", headers={"origin": "https://evil.example"}) as ws:
            ws.receive_text()
    assert exc.value.code == 1008
