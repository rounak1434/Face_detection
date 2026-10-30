"""Apply to Frigate (ADR-0003, step 4) against a simulated Frigate. Fake URLs and passwords only.

FakeFrigate reproduces the Frigate v0.18.2 behaviour the apply depends on
(checked in its source, see docs/FRIGATE-API.md):
- {FRIGATE_*} values, including /run/secrets files, are loaded only at startup;
- /api/config/save refuses a new go2rtc source whose placeholder is unknown to
  the running process, validates before writing (400 keeps the file), and
  restarts after writing with save_option=restart;
- /api/config/raw returns the file as a JSON string.
Passing these tests does not show that a real Frigate behaves this way.
"""

import json
import re
import sqlite3
from pathlib import Path

import httpx
import pytest
import yaml

from ai_surveillance.frigate_config import secret_name

from . import credential_cases

TEMPLATE_PATH = Path(__file__).resolve().parents[3] / "deploy" / "frigate" / "config.yml"
CONTAINER_ENV = {"FRIGATE_CAMERA_MAIN_URL", "FRIGATE_CAMERA_SUB_URL"}  # from deploy/.env today
PASSWORD = "fake-Pa55!word"
DEVICE = {
    "name": "Gate NVR", "kind": "nvr", "host": "192.168.1.20", "rtsp_port": 554, "username": "viewer",
    "password": PASSWORD, "main_stream": "/cam/realmonitor?channel=1&subtype=0",
    "sub_stream": "/cam/realmonitor?channel=1&subtype=1",
}
PLACEHOLDER = re.compile(r"\{(FRIGATE_[A-Za-z0-9_]+)\}")


class FakeFrigate:
    def __init__(self, secrets_dir: Path):
        self.secrets_dir = secrets_dir
        self.file = TEMPLATE_PATH.read_text()
        self.env = set(CONTAINER_ENV)
        self.restarting = False
        self.events: list[str] = []
        self.reject_next_save: str | None = None
        self.never_back = False
        self.drop_save_response = False
        self.drop_restart_response = False
        self.save_error_status: int | None = None
        self.stay_down_after_save = False
        self.drop_cameras: set[str] = set()

    # --- simulated process lifecycle
    def _start(self):
        """Frigate's startup: env.py reads FRIGATE_* files from /run/secrets once, and go2rtc's
        create_config.py exits on a stream placeholder without a value (go2rtc then does not run)."""
        self.env = set(CONTAINER_ENV) | {p.name for p in self.secrets_dir.iterdir() if p.name.startswith("FRIGATE_")}
        self.restarting = False
        streams = yaml.safe_load(self.file)["go2rtc"]["streams"] or {}
        missing = sorted({var for sources in streams.values() for source in sources
                          for var in PLACEHOLDER.findall(source) if var not in self.env})
        self.events.append(f"go2rtc-failed:{','.join(missing)}" if missing else "started")

    def version(self, request):
        if self.restarting:
            if self.never_back:
                raise httpx.ConnectError("down")
            self.restarting = "down-seen" if self.restarting is True else self.restarting
            if self.restarting == "down-seen":
                self.restarting = "starting"
                raise httpx.ConnectError("down")
            self._start()
        return httpx.Response(200, text="0.18.2-test")

    def restart(self, request):
        self.events.append("restart")
        self.restarting = True
        if self.drop_restart_response:
            raise httpx.RemoteProtocolError("connection closed")  # Frigate stopped before answering
        return httpx.Response(200, json={"success": True, "message": "Restarting (this can take up to one minute)..."})

    # --- config API
    def raw(self, request):
        if self.restarting:
            raise httpx.ConnectError("down")
        return httpx.Response(200, text=json.dumps(self.file), headers={"content-type": "text/plain"})

    def config(self, request):
        if self.restarting:
            raise httpx.ConnectError("down")
        cameras = {name: {} for name in yaml.safe_load(self.file)["cameras"] if name not in self.drop_cameras}
        return httpx.Response(200, json={"cameras": cameras})

    def save(self, request):
        if self.restarting:
            raise httpx.ConnectError("down")
        self.events.append("save")
        if self.save_error_status:
            return httpx.Response(self.save_error_status, json={"success": False, "message": "Internal error"})
        new = request.content.decode()
        if self.reject_next_save:
            message, self.reject_next_save = self.reject_next_save, None
            return httpx.Response(400, json={"success": False, "message": message})
        old_streams = (yaml.safe_load(self.file).get("go2rtc") or {}).get("streams") or {}
        unknown = []
        for name, sources in (yaml.safe_load(new)["go2rtc"]["streams"] or {}).items():
            for source in sources:
                if source in (old_streams.get(name) or []):
                    continue
                if any(var not in self.env for var in PLACEHOLDER.findall(source)):
                    unknown.append(name)
                    break
        if unknown:
            return httpx.Response(400, json={"success": False,
                                             "message": f"Unknown variable in go2rtc stream: {', '.join(unknown)}"})
        self.file = new
        assert request.url.params["save_option"] == "restart"
        self.restarting = True
        if self.stay_down_after_save:
            self.never_back = True
        if self.drop_save_response:
            raise httpx.RemoteProtocolError("connection closed")
        return httpx.Response(200, json={"success": True, "message": "Config successfully saved, restarting..."})


@pytest.fixture
def secrets_dir(tmp_path):
    path = tmp_path / "frigate-secrets"
    path.mkdir()
    return path


@pytest.fixture
def settings(settings, tmp_path, secrets_dir):
    settings.frigate_config_template = TEMPLATE_PATH
    settings.frigate_secrets_dir = secrets_dir
    settings.frigate_previous_config_file = tmp_path / "data" / "frigate-config.previous.yml"
    settings.frigate_restart_poll_seconds = 0
    settings.frigate_restart_down_seconds = 2
    settings.frigate_restart_timeout_seconds = 2
    return settings


@pytest.fixture
def fake(frigate, secrets_dir):
    fake = FakeFrigate(secrets_dir)
    frigate.get("/api/version").mock(side_effect=fake.version)
    frigate.post("/api/restart").mock(side_effect=fake.restart)
    frigate.get("/api/config/raw").mock(side_effect=fake.raw)
    frigate.get("/api/config").mock(side_effect=fake.config)
    frigate.post("/api/config/save").mock(side_effect=fake.save)
    return fake


def _db(settings):
    return sqlite3.connect(settings.database_url.removeprefix("sqlite:///"))


def _approved(client, settings, **overrides):
    """A saved device marked connected (as after a passed test) and switched on."""
    device = client.post("/api/devices", json={**DEVICE, **overrides}).json()
    result = json.dumps({"main": {"status": "connected", "width": 2560, "height": 1440},
                         "sub": {"status": "connected", "width": 640, "height": 360}})
    con = _db(settings)
    con.execute("UPDATE devices SET status='connected', test_result=?, use_in_frigate=1 WHERE id=?",
                (result, device["id"]))
    con.commit()
    con.close()
    return device


def _apply(client):
    return client.post("/api/frigate/apply")


def _secret_files(secrets_dir):
    return {p.name: p.read_text() for p in secrets_dir.iterdir()}


def test_apply_requires_login(client):
    assert _apply(client).status_code == 401


def test_apply_is_disabled_until_configured(signed_in, settings):
    settings.frigate_secrets_dir = None
    resp = _apply(signed_in)
    assert resp.status_code == 503
    assert "FRIGATE_SECRETS_DIR" in resp.json()["detail"]


def test_first_apply_restarts_to_load_the_new_secret_then_saves(signed_in, settings, fake, secrets_dir):
    device = _approved(signed_in, settings)
    resp = _apply(signed_in)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == {"cameras": [{"device_id": device["id"], "name": "Gate NVR", "camera": "gate_nvr"}],
                    "skipped": [], "restarts": 2}
    # The restart comes first, so the save sees the new secret (Frigate refuses unknown variables).
    assert fake.events == ["restart", "started", "save", "started"]
    cfg = yaml.safe_load(fake.file)
    assert {"entrance", "gate_nvr"} <= set(cfg["cameras"])
    assert cfg["go2rtc"]["streams"]["gate_nvr"] == [f"{{{secret_name(device['id'], 'main')}}}"]
    assert cfg["cameras"]["gate_nvr"]["detect"]["width"] == 640  # from the sub stream's test
    files = _secret_files(secrets_dir)
    assert files[secret_name(device["id"], "main")] == (
        "rtsp://viewer:fake-Pa55%21word@192.168.1.20:554/cam/realmonitor?channel=1&subtype=0")
    assert set(files) == {secret_name(device["id"], "main"), secret_name(device["id"], "sub")}
    assert PASSWORD not in fake.file and "fake-Pa55" not in resp.text
    assert settings.frigate_previous_config_file.read_text() == TEMPLATE_PATH.read_text()


def test_without_the_first_restart_frigate_would_refuse_the_new_stream(signed_in, settings, fake, secrets_dir):
    """Guards the fake itself: the behaviour that makes the extra restart necessary."""
    device = _approved(signed_in, settings)
    (secrets_dir / secret_name(device["id"], "main")).write_text("rtsp://192.0.2.1/x")
    text = TEMPLATE_PATH.read_text().replace("streams:", "streams:\n    gate_nvr:\n      - "
                                             f"\"{{{secret_name(device['id'], 'main')}}}\"", 1)
    resp = fake.save(httpx.Request("POST", "http://f/api/config/save?save_option=restart", content=text.encode()))
    assert resp.status_code == 400 and "Unknown variable in go2rtc stream: gate_nvr" in resp.text


def test_replacing_one_device_with_another_keeps_go2rtc_startable(signed_in, settings, fake, secrets_dir):
    """The first restart runs the old file with the new secrets: the old device's secret must still be there."""
    old = _approved(signed_in, settings)
    assert _apply(signed_in).status_code == 200
    signed_in.put(f"/api/devices/{old['id']}/frigate", json={"use_in_frigate": False})
    new = _approved(signed_in, settings, name="Back Door", host="192.168.1.21")
    fake.events.clear()
    resp = _apply(signed_in)
    assert resp.status_code == 200, resp.text
    assert fake.events == ["restart", "started", "save", "started"]  # no go2rtc failure at either start
    assert set(_secret_files(secrets_dir)) == {secret_name(new["id"], "main"), secret_name(new["id"], "sub")}
    assert "gate_nvr" not in yaml.safe_load(fake.file)["cameras"]


def test_reapply_with_the_same_devices_restarts_once(signed_in, settings, fake, secrets_dir):
    device = _approved(signed_in, settings)
    assert _apply(signed_in).status_code == 200
    signed_in.put(f"/api/devices/{device['id']}", json={**DEVICE, "password": "fake-new-pass"})
    con = _db(settings)
    con.execute("UPDATE devices SET status='connected' WHERE id=?", (device["id"],))
    con.commit()
    con.close()
    fake.events.clear()
    body = _apply(signed_in).json()
    assert body["restarts"] == 1
    assert fake.events == ["save", "started"]
    assert "fake-new-pass" in _secret_files(secrets_dir)[secret_name(device["id"], "main")]


def test_switching_a_device_off_removes_its_camera_and_secret(signed_in, settings, fake, secrets_dir):
    device = _approved(signed_in, settings)
    _apply(signed_in)
    signed_in.put(f"/api/devices/{device['id']}/frigate", json={"use_in_frigate": False})
    body = _apply(signed_in).json()
    assert body["cameras"] == [] and body["restarts"] == 1
    assert "gate_nvr" not in yaml.safe_load(fake.file)["cameras"]
    assert _secret_files(secrets_dir) == {}


def test_approved_device_that_is_not_connected_is_skipped(signed_in, settings, fake, secrets_dir):
    device = _approved(signed_in, settings)
    con = _db(settings)
    con.execute("UPDATE devices SET status='not_tested' WHERE id=?", (device["id"],))
    con.commit()
    con.close()
    body = _apply(signed_in).json()
    assert body["cameras"] == []
    assert body["skipped"] == [{"device_id": device["id"], "name": "Gate NVR",
                                "reason": "connection test has not passed"}]
    assert _secret_files(secrets_dir) == {}


def test_refused_config_keeps_frigate_and_restores_the_secrets(signed_in, settings, fake, secrets_dir):
    old = _approved(signed_in, settings)
    _apply(signed_in)
    before_file, before_secrets = fake.file, _secret_files(secrets_dir)
    _approved(signed_in, settings, name="Back Door", host="192.168.1.21")
    fake.reject_next_save = (f"Your configuration is invalid. Line 3: rtsp://viewer:{PASSWORD}@192.168.1.21 "
                             "- bad value")
    resp = _apply(signed_in)
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert "Frigate refused the configuration" in detail and "previous configuration is unchanged" in detail
    assert PASSWORD not in detail and "fake-Pa55%21word" not in detail
    assert fake.file == before_file
    assert _secret_files(secrets_dir) == before_secrets
    assert secret_name(old["id"], "main") in before_secrets


def test_password_in_a_cut_off_frigate_message_is_still_removed(signed_in, settings, fake):
    """Frigate messages are cut at 300 characters, so a URL can arrive incomplete; its encoded password must still go."""
    _approved(signed_in, settings)
    fake.reject_next_save = "x" * 230 + "rtsp://viewer:fake-Pa55%21word@192.168.1"  # cut inside the URL
    resp = _apply(signed_in)
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert "fake-Pa55%21word" not in detail and PASSWORD not in detail and "***" in detail


def test_lost_save_response_is_judged_by_what_frigate_runs(signed_in, settings, fake):
    _approved(signed_in, settings)
    fake.drop_save_response = True
    resp = _apply(signed_in)
    assert resp.status_code == 200, resp.text
    assert "gate_nvr" in yaml.safe_load(fake.file)["cameras"]


def test_missing_camera_after_restart_restores_previous_config_and_secrets(signed_in, settings, fake, secrets_dir):
    _approved(signed_in, settings)
    fake.drop_cameras = {"gate_nvr"}
    resp = _apply(signed_in)
    assert resp.status_code == 502
    detail = resp.json()["detail"]
    assert "did not load the cameras ['gate_nvr']" in detail and "previous configuration was restored" in detail
    assert fake.file == TEMPLATE_PATH.read_text()
    assert _secret_files(secrets_dir) == {}
    # restart (load secret), save+restart, then restore: restart (old secrets), save+restart
    assert fake.events.count("restart") == 2 and fake.events.count("save") == 2
    assert not [e for e in fake.events if e.startswith("go2rtc-failed")]


def test_restore_after_replacing_a_device_keeps_go2rtc_startable(signed_in, settings, fake, secrets_dir):
    old = _approved(signed_in, settings)
    _apply(signed_in)
    before_file, before_secrets = fake.file, _secret_files(secrets_dir)
    signed_in.put(f"/api/devices/{old['id']}/frigate", json={"use_in_frigate": False})
    _approved(signed_in, settings, name="Back Door", host="192.168.1.21")
    fake.drop_cameras = {"back_door"}
    fake.events.clear()
    resp = _apply(signed_in)
    assert resp.status_code == 502 and "previous configuration was restored" in resp.json()["detail"]
    assert not [e for e in fake.events if e.startswith("go2rtc-failed")], fake.events
    assert fake.file == before_file
    assert _secret_files(secrets_dir) == before_secrets


def test_frigate_not_coming_back_reports_where_the_previous_config_is(signed_in, settings, fake, secrets_dir):
    device = _approved(signed_in, settings)
    fake.never_back = True
    resp = _apply(signed_in)
    assert resp.status_code == 502
    detail = resp.json()["detail"]
    assert "did not come back" in detail and "Restoring the previous configuration also failed" in detail
    assert "outcome is UNKNOWN" in detail  # D4: never reported as done or as restored
    assert "frigate-config.previous.yml" in detail and str(settings.frigate_previous_config_file.parent) not in detail
    assert settings.frigate_previous_config_file.read_text() == TEMPLATE_PATH.read_text()
    # Which file Frigate will start with is unknown, so the secrets for both stay: go2rtc can start either way.
    assert set(_secret_files(secrets_dir)) == {secret_name(device["id"], "main"), secret_name(device["id"], "sub")}


def test_missing_template_changes_nothing(signed_in, settings, fake, secrets_dir, tmp_path):
    _approved(signed_in, settings)
    settings.frigate_config_template = tmp_path / "no-such-template.yml"
    resp = _apply(signed_in)
    assert resp.status_code == 503 and "Nothing was changed" in resp.json()["detail"]
    assert "no-such-template" not in resp.json()["detail"]
    assert fake.events == [] and _secret_files(secrets_dir) == {}


@pytest.mark.parametrize("text", ["cameras: [unclosed", "go2rtc: {streams: {}}\n", "- just a list\n"])
def test_malformed_template_changes_nothing(signed_in, settings, fake, secrets_dir, tmp_path, text):
    _approved(signed_in, settings)
    template = tmp_path / "bad-template.yml"
    template.write_text(text)
    settings.frigate_config_template = template
    resp = _apply(signed_in)
    assert resp.status_code == 422
    assert resp.json()["detail"].startswith("Frigate configuration could not be built")
    assert fake.events == [] and _secret_files(secrets_dir) == {}
    assert fake.file == TEMPLATE_PATH.read_text()


def test_restart_answer_lost_while_frigate_stops_is_not_a_failure(signed_in, settings, fake):
    """D3: POST /api/restart often gets no answer because Frigate stops first; waiting decides."""
    _approved(signed_in, settings)
    fake.drop_restart_response = True
    resp = _apply(signed_in)
    assert resp.status_code == 200, resp.text
    assert resp.json()["restarts"] == 2 and "gate_nvr" in yaml.safe_load(fake.file)["cameras"]


def test_save_http_error_is_reported_as_such_and_rolled_back(signed_in, settings, fake, secrets_dir):
    """D3: an HTTP error from config/save (not a lost answer) fails at once, not as 'did not restart'."""
    _approved(signed_in, settings)
    fake.save_error_status = 500
    resp = _apply(signed_in)
    assert resp.status_code == 502
    detail = resp.json()["detail"]
    assert "config/save returned HTTP 500" in detail and "previous configuration was restored" in detail
    assert "did not restart" not in detail
    assert fake.file == TEMPLATE_PATH.read_text() and _secret_files(secrets_dir) == {}
    assert not [e for e in fake.events if e.startswith("go2rtc-failed")]


def test_unusable_stored_stream_address_stops_apply_before_anything_changes(signed_in, settings, fake, secrets_dir):
    """D2: a record that bypassed API validation (here a space in the path) gives 422, not a 500."""
    device = _approved(signed_in, settings)
    con = _db(settings)
    con.execute("UPDATE devices SET main_path='/live stream' WHERE id=?", (device["id"],))
    con.commit()
    con.close()
    resp = _apply(signed_in)
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail.startswith("Gate NVR: its stream address cannot be used") and "Nothing was changed" in detail
    assert PASSWORD not in detail and "live stream" not in detail
    assert fake.events == [] and _secret_files(secrets_dir) == {}


def test_unreadable_secrets_directory_stops_apply_before_anything_changes(signed_in, settings, fake, monkeypatch):
    """D1: no raw OSError: a clean 500 'Nothing was changed', no restart, no path in the message."""
    from ai_surveillance import frigate_secrets

    _approved(signed_in, settings)

    def refuse(self):
        raise PermissionError(13, "Permission denied", str(self))

    monkeypatch.setattr(frigate_secrets.FrigateSecrets, "_managed_paths", refuse)
    resp = _apply(signed_in)
    assert resp.status_code == 500
    detail = resp.json()["detail"]
    assert "Permission denied" in detail and "Nothing was changed" in detail
    assert str(settings.frigate_secrets_dir) not in detail
    assert fake.events == [] and fake.file == TEMPLATE_PATH.read_text()


def test_retry_after_a_failed_restore_keeps_the_config_from_before_the_failure(signed_in, settings, fake):
    """D5: the kept copy is not lost when the operator applies again while Frigate runs an unconfirmed config."""
    from ai_surveillance.frigate_apply import older_copy

    _approved(signed_in, settings)
    assert _apply(signed_in).status_code == 200
    good_config = fake.file
    _approved(signed_in, settings, name="Back Door", host="192.168.1.21")
    fake.stay_down_after_save = True  # the new config is saved, then Frigate does not come back
    resp = _apply(signed_in)
    assert resp.status_code == 502 and "outcome is UNKNOWN" in resp.json()["detail"]
    kept = settings.frigate_previous_config_file
    assert kept.read_text() == good_config
    fake.stay_down_after_save = fake.never_back = False
    fake._start()  # Frigate comes back later, running the unconfirmed new config
    assert fake.events[-1] == "started" and fake.file != good_config  # go2rtc could start: both secrets kept
    assert _apply(signed_in).status_code == 200  # the operator applies again
    assert older_copy(kept).read_text() == good_config  # still there after the retry
    assert sorted(p.name for p in kept.parent.iterdir()) == [kept.name, older_copy(kept).name]  # bounded


def test_frigate_unreachable_before_apply_changes_nothing(signed_in, settings, frigate, secrets_dir):
    _approved(signed_in, settings)
    frigate.get("/api/config/raw").mock(side_effect=httpx.ConnectError("refused"))
    resp = _apply(signed_in)
    assert resp.status_code == 502 and "Nothing was changed" in resp.json()["detail"]
    assert _secret_files(secrets_dir) == {}
    assert not [c for c in frigate.calls if c.request.method == "POST"]  # no restart, no save


def test_undecryptable_password_changes_nothing(signed_in, settings, fake, secrets_dir):
    """The ciphertext does not open for this row (here: one bound to another device)."""
    device = _approved(signed_in, settings)
    other = signed_in.post("/api/devices", json={**DEVICE, "name": "Other", "host": "192.168.1.30"}).json()
    con = _db(settings)
    con.execute("UPDATE devices SET password_enc=(SELECT password_enc FROM devices WHERE id=?) WHERE id=?",
                (other["id"], device["id"]))
    con.commit()
    con.close()
    resp = _apply(signed_in)
    assert resp.status_code == 409
    assert "Gate NVR" in resp.json()["detail"] and "Nothing was changed in Frigate" in resp.json()["detail"]
    assert fake.events == [] and _secret_files(secrets_dir) == {}


@pytest.mark.parametrize("kind", credential_cases.KINDS)
def test_damaged_password_stops_apply_before_anything_changes(signed_in, settings, fake, secrets_dir, caplog, kind):
    """Frigate already runs a device; its stored password is then damaged: the next apply touches nothing."""
    from ai_surveillance.credentials import load_key

    device = _approved(signed_in, settings)
    assert _apply(signed_in).status_code == 200
    file_before, secrets_before = fake.file, _secret_files(secrets_dir)
    previous_copy = settings.frigate_previous_config_file.read_text()
    con = _db(settings)
    good = con.execute("SELECT password_enc FROM devices WHERE id=?", (device["id"],)).fetchone()[0]
    bad = credential_cases.damage(kind, good, device["id"], load_key(settings))
    con.execute("UPDATE devices SET password_enc=? WHERE id=?", (bad, device["id"]))
    con.commit()
    fake.events.clear()

    with caplog.at_level("INFO"):
        resp = _apply(signed_in)
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail.startswith("Gate NVR: Stored credential") and "Nothing was changed in Frigate" in detail
    assert fake.events == []  # no restart, no save
    assert fake.file == file_before and _secret_files(secrets_dir) == secrets_before
    assert settings.frigate_previous_config_file.read_text() == previous_copy
    for text in (resp.text, caplog.text):
        assert bad[3:15] not in text and PASSWORD not in text and "Traceback" not in text
    assert con.execute("SELECT password_enc FROM devices WHERE id=?", (device["id"],)).fetchone()[0] == bad
    con.close()


def test_only_one_apply_at_a_time(signed_in, settings, fake):
    import asyncio

    state = signed_in.app.state.app
    signed_in.portal.call(state.frigate_apply_lock.acquire)
    try:
        resp = _apply(signed_in)
    finally:
        state.frigate_apply_lock.release()
    assert resp.status_code == 409 and "already running" in resp.json()["detail"]
    assert fake.events == []
    assert isinstance(state.frigate_apply_lock, asyncio.Lock)


def test_apply_logs_no_secret(signed_in, settings, fake, caplog):
    _approved(signed_in, settings)
    fake.drop_cameras = {"gate_nvr"}
    with caplog.at_level("INFO"):
        _apply(signed_in)
    assert PASSWORD not in caplog.text and "fake-Pa55%21word" not in caplog.text
    assert "restoring the previous configuration" in caplog.text
