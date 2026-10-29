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


def test_config_raw_decodes_frigates_json_string():
    """Frigate 0.18.2 wraps the file in a JSONResponse with media type text/plain."""
    client = _client()
    with respx.mock(base_url=AUTH_URL) as mock:
        mock.get("/api/config/raw").respond(200, text='"cameras:\\n  a: {}\\n"', headers={"content-type": "text/plain"})
        assert run(client.config_raw()) == "cameras:\n  a: {}\n"


def test_save_config_sends_text_and_the_save_option():
    client = _client(frigate_username="viewer", frigate_password="pw-123456789012")
    with respx.mock(base_url=AUTH_URL) as mock:
        mock.post("/api/login").respond(200, headers={"set-cookie": "frigate_token=jwt-1"})
        save = mock.post("/api/config/save").respond(200, json={"success": True, "message": "Config successfully saved."})
        assert run(client.save_config("cameras: {}\n", restart=False)) == "Config successfully saved."
        run(client.save_config("cameras: {}\n", restart=True))
    first, second = (c.request for c in save.calls)
    assert first.url.params["save_option"] == "saveonly" and second.url.params["save_option"] == "restart"
    assert first.content == b"cameras: {}\n"
    assert first.headers["content-type"] == "text/plain"
    assert first.headers["authorization"] == "Bearer jwt-1"


def test_save_config_refusal_is_its_own_error():
    from ai_surveillance.frigate_client import FrigateConfigRejected

    client = _client()
    with respx.mock(base_url=AUTH_URL) as mock:
        mock.post("/api/config/save").respond(400, json={"success": False, "message": "Your configuration is invalid."})
        with pytest.raises(FrigateConfigRejected, match="Your configuration is invalid"):
            run(client.save_config("x: 1\n", restart=True))
        mock.post("/api/config/save").respond(500, json={"success": False, "message": "boom"})
        with pytest.raises(FrigateError, match="HTTP 500") as caught:
            run(client.save_config("x: 1\n", restart=True))
        assert not isinstance(caught.value, FrigateConfigRejected)


def test_restart_and_is_up():
    client = _client()
    with respx.mock(base_url=AUTH_URL) as mock:
        mock.post("/api/restart").respond(500, json={"success": False, "message": "Unable to restart Frigate."})
        with pytest.raises(FrigateError, match="Unable to restart Frigate"):
            run(client.restart())
        mock.get("/api/version").mock(side_effect=httpx.ConnectError("down"))
        assert run(client.is_up()) is False
        mock.get("/api/version").respond(200, text="0.18.2")
        assert run(client.is_up()) is True


def test_live_ws_url():
    http = FrigateClient(Settings(frigate_url="http://frigate:5000", _env_file=None))
    assert http.live_ws_url("entrance_sub") == "ws://frigate:5000/live/mse/api/ws?src=entrance_sub"
    https = FrigateClient(Settings(frigate_url="https://frigate:8971/", _env_file=None))
    assert https.live_ws_url("front door") == "wss://frigate:8971/live/mse/api/ws?src=front%20door"
