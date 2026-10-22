"""Camera & NVR setup API. Network access is replaced by fakes: these tests
prove the rules (secrets, SSRF guard, honest statuses), not that a real
device works."""

import ipaddress
import logging
import sqlite3

import pytest

from ai_surveillance import devices as dev
from ai_surveillance import netguard
from ai_surveillance.credentials import CredentialCipher, CredentialError
from ai_surveillance.rtsp import ProbeResult, StreamInfo

SECRET = "S3cret!pass@word"

VALID = {
    "name": "Gate NVR",
    "kind": "nvr",
    "host": "192.168.1.20",
    "rtsp_port": 554,
    "username": "viewer",
    "password": SECRET,
    "main_stream": "/cam/realmonitor?channel=1&subtype=0",
    "sub_stream": "/cam/realmonitor?channel=1&subtype=1",
}


class FakeNet:
    """Stands in for TCP connects, DNS and ffprobe; records what would be contacted."""

    def __init__(self, monkeypatch):
        self.tcp_open = True
        self.ffprobe = True
        self.probe_result = None
        self.dns: dict[str, list[str]] = {}
        self.connected: list[tuple[str, int]] = []
        self.probed: list[str] = []

        async def fake_tcp(ip, port):
            self.connected.append((ip, port))
            return self.tcp_open

        def fake_probe(url, timeout=15.0):
            self.probed.append(url)
            if self.probe_result is not None:
                return self.probe_result
            return ProbeResult(dev.mask_credentials(url), True, [StreamInfo("h264", 1280, 720, 15.0, "Main")], [])

        def fake_resolve(host):
            if host not in self.dns:
                raise OSError("unknown host")
            return self.dns[host]

        monkeypatch.setattr(dev, "_tcp_open", fake_tcp)
        monkeypatch.setattr(dev, "probe", fake_probe)
        monkeypatch.setattr(dev, "ffprobe_available", lambda: self.ffprobe)
        monkeypatch.setattr(netguard, "_system_resolve", fake_resolve)


@pytest.fixture
def net(monkeypatch):
    return FakeNet(monkeypatch)


def _create(client, **overrides):
    return client.post("/api/devices", json={**VALID, **overrides})


# --- secrets -----------------------------------------------------------------

def test_requires_login(client):
    assert client.get("/api/devices").status_code == 401
    assert client.post("/api/devices", json=VALID).status_code == 401


def test_password_is_never_returned(signed_in):
    resp = _create(signed_in)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["has_password"] is True
    assert "password" not in body and "password_enc" not in body
    assert body["main_url_display"] == "rtsp://***@192.168.1.20:554/cam/realmonitor?channel=1&subtype=0"
    for text in (resp.text, signed_in.get("/api/devices").text):
        assert SECRET not in text
        assert "viewer:" not in text


def test_password_is_encrypted_at_rest(signed_in, settings):
    _create(signed_in)
    db_path = settings.database_url.removeprefix("sqlite:///")
    stored = sqlite3.connect(db_path).execute("select password_enc from devices").fetchone()[0]
    assert stored.startswith("v1:")
    assert SECRET not in stored


def test_secrets_never_logged(signed_in, net, caplog):
    caplog.set_level(logging.DEBUG)
    device = _create(signed_in).json()
    net.probe_result = ProbeResult("x", False, [], [], f"rtsp://viewer:{SECRET}@192.168.1.20 401 Unauthorized")
    signed_in.post(f"/api/devices/{device['id']}/test")
    assert SECRET not in caplog.text


def test_cipher_binds_ciphertext_to_device():
    cipher = CredentialCipher(b"k" * 32)
    token = cipher.encrypt("pw", "device-a")
    assert cipher.decrypt(token, "device-a") == "pw"
    with pytest.raises(CredentialError):
        cipher.decrypt(token, "device-b")  # copied to another row
    with pytest.raises(CredentialError):
        CredentialCipher(b"x" * 32).decrypt(token, "device-a")  # wrong key


def test_key_file_created_once(settings):
    from ai_surveillance.credentials import load_key

    first = load_key(settings)
    assert settings.credentials_key_file.exists()
    assert load_key(settings) == first


# --- validation --------------------------------------------------------------

@pytest.mark.parametrize(
    "overrides, fragment",
    [
        ({"host": "not a host!"}, "IP address or a host name"),
        ({"rtsp_port": 0}, "greater than or equal to 1"),
        ({"rtsp_port": 70000}, "less than or equal to 65535"),
        ({"kind": "webcam"}, "pattern"),
        ({"name": ""}, "at least 1"),
        ({"main_stream": "rtsp://user:pw@192.168.1.20/x"}, "credential fields"),
        ({"main_stream": "rtsp://192.168.1.99/x"}, "same host"),
        ({"main_stream": "rtsp://192.168.1.20:8554/x"}, "same RTSP port"),
        ({"main_stream": "http://192.168.1.20/x"}, "Only rtsp://"),
        ({"main_stream": "file:///etc/passwd"}, "Only rtsp://"),
        ({"main_stream": "/path with space"}, "not allowed"),
        ({"username": "a:b"}, "must not contain"),
        ({"password": "bad\npass"}, "control characters"),
    ],
)
def test_invalid_input_rejected(signed_in, overrides, fragment):
    resp = _create(signed_in, **overrides)
    assert resp.status_code == 422
    assert fragment in resp.text


def test_full_url_for_same_device_is_reduced_to_path(signed_in):
    body = _create(signed_in, main_stream="rtsp://192.168.1.20:554/cam/realmonitor?channel=2&subtype=0").json()
    assert body["main_stream"] == "/cam/realmonitor?channel=2&subtype=0"


def test_path_without_leading_slash_accepted(signed_in):
    body = _create(signed_in, main_stream="Streaming/Channels/101", sub_stream="").json()
    assert body["main_stream"] == "/Streaming/Channels/101"
    assert body["sub_stream"] is None


@pytest.mark.parametrize("host", ["127.0.0.1", "169.254.169.254", "8.8.8.8", "0.0.0.0", "::1", "224.0.0.1"])
def test_disallowed_ip_literal_refused_at_save(signed_in, host):
    resp = _create(signed_in, host=host, main_stream="/x")
    assert resp.status_code == 422


def test_duplicate_name_conflicts(signed_in):
    assert _create(signed_in).status_code == 201
    assert _create(signed_in).status_code == 409


# --- SSRF guard (unit level) ---------------------------------------------------

ALLOWED = netguard.parse_networks("10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,fc00::/7")


@pytest.mark.parametrize("ip", ["192.168.1.20", "10.1.2.3", "172.20.0.5", "fd00::10"])
def test_private_addresses_allowed(ip):
    netguard.check_ip(ipaddress.ip_address(ip), ALLOWED)


@pytest.mark.parametrize("ip", ["127.0.0.1", "169.254.169.254", "8.8.8.8", "::1", "fe80::1", "::ffff:127.0.0.1",
                                "100.64.0.1", "0.0.0.0"])
def test_unsafe_addresses_refused(ip):
    with pytest.raises(netguard.AddressNotAllowed):
        netguard.check_ip(ipaddress.ip_address(ip), ALLOWED)


def test_hostname_must_resolve_only_to_allowed_addresses():
    assert str(netguard.resolve_allowed("nvr.lan", ALLOWED, resolver=lambda h: ["192.168.1.20"])) == "192.168.1.20"
    with pytest.raises(netguard.AddressNotAllowed):
        netguard.resolve_allowed("evil.lan", ALLOWED, resolver=lambda h: ["192.168.1.20", "127.0.0.1"])
    with pytest.raises(netguard.AddressNotAllowed):
        netguard.resolve_allowed("localhost", ALLOWED, resolver=lambda h: ["127.0.0.1"])


def test_no_arbitrary_url_probe_endpoint(signed_in):
    for path in ("/api/devices/test", "/api/devices/probe", "/api/probe"):
        resp = signed_in.post(path, json={"url": "rtsp://192.168.1.20/x"})
        assert resp.status_code in (404, 405)


# --- connection test statuses -------------------------------------------------

def test_new_device_is_not_tested(signed_in):
    assert _create(signed_in).json()["status"] == "not_tested"


def test_connected_only_after_decoded_video(signed_in, net):
    device = _create(signed_in).json()
    body = signed_in.post(f"/api/devices/{device['id']}/test").json()
    assert body["status"] == "connected"
    assert body["streams"]["main"]["codec"] == "h264"
    assert body["streams"]["sub"]["status"] == "connected"
    assert body["tested_at"] is not None
    # Connected to the checked IP literal, with credentials only in the probe URL.
    assert net.connected == [("192.168.1.20", 554)]
    assert net.probed[0] == "rtsp://viewer:S3cret%21pass%40word@192.168.1.20:554/cam/realmonitor?channel=1&subtype=0"
    assert SECRET not in str(body)


def test_unreachable_when_tcp_fails(signed_in, net):
    net.tcp_open = False
    device = _create(signed_in).json()
    body = signed_in.post(f"/api/devices/{device['id']}/test").json()
    assert body["status"] == "unreachable"
    assert net.probed == []


@pytest.mark.parametrize(
    "error, expected",
    [
        ("method DESCRIBE failed: 401 Unauthorized", "invalid_credentials"),
        ("method DESCRIBE failed: 404 Not Found", "stream_not_found"),
        ("Connection refused", "unreachable"),
        ("Timed out after 10.0s", "unreachable"),
        ("Invalid data found when processing input", "error"),
    ],
)
def test_probe_failures_are_classified(signed_in, net, error, expected):
    net.probe_result = ProbeResult("x", False, [], [], error)
    device = _create(signed_in).json()
    assert signed_in.post(f"/api/devices/{device['id']}/test").json()["status"] == expected


def test_without_ffprobe_never_reports_connected(signed_in, net):
    net.ffprobe = False
    device = _create(signed_in).json()
    body = signed_in.post(f"/api/devices/{device['id']}/test").json()
    assert body["status"] == "reachable_unverified"
    assert "not checked" in body["status_detail"]


def test_hostname_resolving_to_loopback_is_blocked_at_test_time(signed_in, net):
    net.dns["sneaky.lan"] = ["127.0.0.1"]
    device = _create(signed_in, host="sneaky.lan").json()
    body = signed_in.post(f"/api/devices/{device['id']}/test").json()
    assert body["status"] == "address_not_allowed"
    assert net.connected == [] and net.probed == []


def test_hostname_probe_uses_resolved_ip(signed_in, net):
    net.dns["nvr.lan"] = ["192.168.1.30"]
    device = _create(signed_in, host="nvr.lan", main_stream="/live", sub_stream=None).json()
    signed_in.post(f"/api/devices/{device['id']}/test")
    assert net.connected == [("192.168.1.30", 554)]
    assert "@192.168.1.30:554/live" in net.probed[0]


def test_test_cooldown(signed_in, net, settings):
    settings.device_test_cooldown_seconds = 60
    device = _create(signed_in).json()
    assert signed_in.post(f"/api/devices/{device['id']}/test").status_code == 200
    assert signed_in.post(f"/api/devices/{device['id']}/test").status_code == 429


# --- edit / remove -----------------------------------------------------------

def test_edit_keeps_password_and_resets_status(signed_in, net):
    device = _create(signed_in).json()
    signed_in.post(f"/api/devices/{device['id']}/test")
    update = {**VALID, "password": None, "main_stream": "/cam/realmonitor?channel=2&subtype=0"}
    body = signed_in.put(f"/api/devices/{device['id']}", json=update).json()
    assert body["has_password"] is True
    assert body["status"] == "not_tested"
    signed_in.post(f"/api/devices/{device['id']}/test")
    assert f"viewer:{SECRET.replace('!', '%21').replace('@', '%40')}@" in net.probed[-1]


def test_rename_only_keeps_test_result(signed_in, net):
    device = _create(signed_in).json()
    signed_in.post(f"/api/devices/{device['id']}/test")
    body = signed_in.put(f"/api/devices/{device['id']}", json={**VALID, "name": "Renamed", "password": None}).json()
    assert body["name"] == "Renamed"
    assert body["status"] == "connected"


def test_clear_password(signed_in):
    device = _create(signed_in).json()
    body = signed_in.put(f"/api/devices/{device['id']}", json={**VALID, "password": None, "clear_password": True}).json()
    assert body["has_password"] is False


def test_remove(signed_in):
    device = _create(signed_in).json()
    assert signed_in.delete(f"/api/devices/{device['id']}").status_code == 204
    assert signed_in.get("/api/devices").json() == []
    assert signed_in.delete(f"/api/devices/{device['id']}").status_code == 404


# --- synthetic source ----------------------------------------------------------

def test_synthetic_source_is_server_defined(signed_in):
    body = signed_in.post("/api/devices/synthetic", json={"host": "127.0.0.1"}).json()
    assert body["kind"] == "synthetic"
    assert body["source_label"] == "Synthetic RTSP test source"
    assert body["main_url_display"] == "rtsp://test-stream:8554/test"
    assert body["editable"] is False
    again = signed_in.post("/api/devices/synthetic").json()
    assert again["id"] == body["id"]
    resp = signed_in.put(f"/api/devices/{body['id']}", json=VALID)
    assert resp.status_code == 400


def test_synthetic_source_unreachable_without_stack(signed_in, monkeypatch):
    import socket

    def no_dns(*args, **kwargs):
        raise socket.gaierror("no such host")

    monkeypatch.setattr(socket, "getaddrinfo", no_dns)
    body = signed_in.post("/api/devices/synthetic").json()
    result = signed_in.post(f"/api/devices/{body['id']}/test").json()
    assert result["status"] == "unreachable"
