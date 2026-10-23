"""Unit-test fixtures. Frigate is replaced by a respx mock; nothing leaves the machine.

The mock responses mirror the shapes in the Frigate v0.18.2 source (see
docs/FRIGATE-API.md). Passing these tests does NOT show that a real Frigate
behaves this way; that is what tests/integration is for.
"""

import pytest
import respx
from fastapi.testclient import TestClient

from ai_surveillance.config import Settings
from ai_surveillance.db import AdminUser, Database
from ai_surveillance.main import create_app
from ai_surveillance.security import hash_password

FRIGATE = "http://frigate.test:5000"
INFERENCE = "http://inference.test:8100"
ADMIN_USER = "admin"
ADMIN_PASSWORD = "correct-horse-battery"
JPEG = b"\xff\xd8\xff\xe0fake-jpeg\xff\xd9"

FRIGATE_CONFIG = {
    "cameras": {
        "entrance": {
            "enabled": True,
            "detect": {"width": 1280, "height": 720},
            "record": {"enabled": True},
            "live": {"streams": {"Sub": "entrance_sub", "Main": "entrance"}},
            # Frigate masks credentials here; the app must not forward paths anyway.
            "ffmpeg": {"inputs": [{"path": "rtsp://*:*@10.0.0.5:554/stream"}]},
        },
    }
}

FRIGATE_STATS = {"cameras": {"entrance": {"camera_fps": 15.0, "process_fps": 5.0, "detection_fps": 0.0}}}


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        database_url=f"sqlite:///{tmp_path / 'app.db'}",
        media_root=tmp_path / "media",
        frigate_url=FRIGATE,
        inference_url=INFERENCE,
        cookie_secure=False,  # TestClient talks plain HTTP
        clip_settle_seconds=0,
        clip_poll_interval_seconds=0,
        clip_export_timeout_seconds=5,
        credentials_key_file=tmp_path / "credentials.key",
        device_test_cooldown_seconds=0,
        _env_file=None,
    )


@pytest.fixture
def frigate():
    with respx.mock(base_url=FRIGATE, assert_all_called=False) as mock:
        # The inference service is mocked in the same router so health checks
        # never touch the network; tests override these routes as needed.
        mock.get(f"{INFERENCE}/v1/health").respond(
            200, json={"status": "ok", "ready": True, "models": {"yunet": "ready"}}
        )
        mock.get("/api/version").respond(200, text="0.18.2-test")
        mock.get("/api/config").respond(200, json=FRIGATE_CONFIG)
        mock.get("/api/stats").respond(200, json=FRIGATE_STATS)
        mock.get("/api/entrance/latest.jpg", name="latest").respond(
            200, content=JPEG, headers={"content-type": "image/jpeg"}
        )
        yield mock


@pytest.fixture
def client(settings, frigate):
    db = Database(settings.database_url)
    db.migrate()
    with db.sessionmaker() as session:
        session.add(AdminUser(username=ADMIN_USER, password_hash=hash_password(ADMIN_PASSWORD)))
        session.commit()
    db.engine.dispose()

    # respx patches httpx while the `frigate` fixture is active, so the app's
    # own client is served by the mock routes.
    with TestClient(create_app(settings)) as test_client:
        yield test_client


@pytest.fixture
def signed_in(client):
    resp = client.post("/api/auth/login", json={"username": ADMIN_USER, "password": ADMIN_PASSWORD})
    assert resp.status_code == 200
    return client
