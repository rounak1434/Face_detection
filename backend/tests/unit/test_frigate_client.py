"""FrigateClient against a mocked Frigate: authenticated port, stream choice, error messages."""

import asyncio

import httpx
import pytest
import respx

from ai_surveillance.config import Settings
from ai_surveillance.frigate_client import FrigateClient, FrigateError, live_stream_name

AUTH_URL = "https://frigate.test:8971"


def _client(**overrides) -> FrigateClient:
    return FrigateClient(Settings(frigate_url=AUTH_URL, _env_file=None, **overrides))


def run(coro):
    return asyncio.run(coro)


def test_login_on_authenticated_port_uses_bearer_token():
    client = _client(frigate_username="viewer", frigate_password="pw-123456789012")
    with respx.mock(base_url=AUTH_URL) as mock:
        login = mock.post("/api/login").respond(200, headers={"set-cookie": "frigate_token=jwt-1; HttpOnly"})
        config = mock.get("/api/config").respond(200, json={"cameras": {"entrance": {}}})
        assert run(client.cameras()) == {"entrance": {}}
    assert login.calls.last.request.read() == b'{"user":"viewer","password":"pw-123456789012"}'
    assert config.calls.last.request.headers["authorization"] == "Bearer jwt-1"


def test_expired_token_triggers_one_relogin():
    client = _client(frigate_username="viewer", frigate_password="pw-123456789012")
    with respx.mock(base_url=AUTH_URL) as mock:
        login = mock.post("/api/login")
        login.side_effect = [
            httpx.Response(200, headers={"set-cookie": "frigate_token=old"}),
            httpx.Response(200, headers={"set-cookie": "frigate_token=new"}),
        ]
        config = mock.get("/api/config")
        config.side_effect = [httpx.Response(401), httpx.Response(200, json={"cameras": {}})]
        assert run(client.cameras()) == {}
    assert login.call_count == 2
    assert config.calls.last.request.headers["authorization"] == "Bearer new"


def test_login_failure_is_reported():
    client = _client(frigate_username="viewer", frigate_password="wrong-password-1")
    with respx.mock(base_url=AUTH_URL) as mock:
        mock.post("/api/login").respond(401, json={"message": "Login failed"})
        with pytest.raises(FrigateError, match="login failed with HTTP 401"):
            run(client.cameras())


def test_internal_port_sends_no_credentials():
    client = FrigateClient(Settings(frigate_url="http://frigate:5000", _env_file=None))
    with respx.mock(base_url="http://frigate:5000") as mock:
        route = mock.get("/api/config").respond(200, json={"cameras": {}})
        run(client.cameras())
    assert "authorization" not in route.calls.last.request.headers


def test_frigate_error_message_is_surfaced():
    client = FrigateClient(Settings(frigate_url="http://frigate:5000", _env_file=None))
    with respx.mock(base_url="http://frigate:5000") as mock:
        mock.post(url__regex=r"/api/export/.*").respond(
            503, json={"success": False, "message": "Export queue is full. Try again once current exports finish."}
        )
        with pytest.raises(FrigateError, match="HTTP 503: Export queue is full"):
            run(client.start_export("entrance", 1, 11, "x"))


@pytest.mark.parametrize(
    "config, expected",
    [
        ({"live": {"streams": {"Sub": "entrance_sub", "Main": "entrance"}}}, "entrance_sub"),
        ({"live": {"streams": {}}}, "entrance"),
        ({"live": {"streams": []}}, "entrance"),  # Frigate's default_factory is a list
        ({}, "entrance"),
    ],
)
def test_live_stream_choice(config, expected):
    assert live_stream_name("entrance", config) == expected


def test_live_ws_url():
    http = FrigateClient(Settings(frigate_url="http://frigate:5000", _env_file=None))
    assert http.live_ws_url("entrance_sub") == "ws://frigate:5000/live/mse/api/ws?src=entrance_sub"
    https = FrigateClient(Settings(frigate_url="https://frigate:8971/", _env_file=None))
    assert https.live_ws_url("front door") == "wss://frigate:8971/live/mse/api/ws?src=front%20door"
