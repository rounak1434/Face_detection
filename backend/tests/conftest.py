"""Test fixtures. Frigate is replaced by a respx mock; nothing leaves the machine."""

import pytest
import respx
from fastapi.testclient import TestClient

from ai_surveillance.config import Settings
from ai_surveillance.db import AdminUser, Database
from ai_surveillance.main import create_app
from ai_surveillance.security import hash_password

FRIGATE = "http://frigate.test:5000"
ADMIN_USER = "admin"
ADMIN_PASSWORD = "correct-horse-battery"
JPEG = b"\xff\xd8\xff\xe0fake-jpeg\xff\xd9"

FRIGATE_CONFIG = {
    "cameras": {
        "entrance": {"enabled": True, "detect": {"width": 1280, "height": 720}, "record": {"enabled": True},
                     "ffmpeg": {"inputs": [{"path": "rtsp://user:secret@10.0.0.5:554/stream"}]}},
    }
}


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        database_url=f"sqlite:///{tmp_path / 'app.db'}",
        media_root=tmp_path / "media",
        frigate_url=FRIGATE,
        cookie_secure=False,  # TestClient talks plain HTTP
        clip_settle_seconds=0,
        clip_poll_interval_seconds=0,
        clip_export_timeout_seconds=5,
        _env_file=None,
    )


@pytest.fixture
def frigate():
    with respx.mock(base_url=FRIGATE, assert_all_called=False) as mock:
        mock.get("/api/version").respond(200, text="0.18.2-test")
        mock.get("/api/config").respond(200, json=FRIGATE_CONFIG)
        mock.get("/api/entrance/latest.jpg").respond(200, content=JPEG, headers={"content-type": "image/jpeg"})
        yield mock


@pytest.fixture
def client(settings, frigate):
    db = Database(settings.database_url)
    db.create_all()
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
