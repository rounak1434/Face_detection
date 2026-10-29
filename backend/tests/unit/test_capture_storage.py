"""Capture retention and the free-space floor that keeps room for the database."""

import time
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from ai_surveillance.captures import purge_old_captures
from ai_surveillance.db import Capture, Database, utcnow
from ai_surveillance.main import create_app
from ai_surveillance.media import MediaStore

from .conftest import JPEG
from .test_cameras_and_captures import _ready_export, _wait_for

MB = 1024 * 1024


def _add(db: Database, store: MediaStore, *, age_days: float, status: str = "ready", kind: str = "snapshot") -> Capture:
    with db.sessionmaker() as s:
        c = Capture(camera="entrance", kind=kind, status=status, created_at=utcnow() - timedelta(days=age_days))
        s.add(c)
        s.flush()
        if status == "ready":
            c.file_name = f"{c.id}.{'mp4' if kind == 'clip' else 'jpg'}"
            store.write_atomic(c.file_name, JPEG)
        s.commit()
        s.refresh(c)
        s.expunge(c)
        return c


def _ids(db: Database) -> set[str]:
    with db.sessionmaker() as s:
        return {c.id for c in s.query(Capture).all()}


@pytest.fixture
def storage(settings):
    db = Database(settings.database_url)
    db.migrate()
    yield db, MediaStore(settings.media_root)
    db.engine.dispose()


def test_purge_removes_only_old_finished_captures(storage):
    db, store = storage
    old_ready = _add(db, store, age_days=10)
    old_failed = _add(db, store, age_days=10, status="failed")
    old_pending = _add(db, store, age_days=10, status="pending", kind="clip")
    recent = _add(db, store, age_days=1)

    assert purge_old_captures(db.sessionmaker, store, days=7) == 2
    assert _ids(db) == {old_pending.id, recent.id}
    assert not store.path_for(old_ready.file_name).exists()
    assert store.path_for(recent.file_name).is_file()
    assert old_failed.id not in _ids(db)


def test_purge_keeps_the_record_when_its_file_cannot_be_removed(storage, monkeypatch):
    db, store = storage
    old = _add(db, store, age_days=10)
    monkeypatch.setattr(MediaStore, "delete", lambda self, name: (_ for _ in ()).throw(PermissionError(13, "denied")))
    assert purge_old_captures(db.sessionmaker, store, days=7) == 0
    assert old.id in _ids(db)


def test_retention_is_off_by_default(settings, frigate):
    assert settings.capture_retention_days == 0
    db = Database(settings.database_url)
    db.migrate()
    old = _add(db, MediaStore(settings.media_root), age_days=3650)
    db.engine.dispose()
    with TestClient(create_app(settings)):
        pass
    db = Database(settings.database_url)
    assert old.id in _ids(db)
    db.engine.dispose()


def test_retention_runs_at_startup_when_enabled(settings, frigate):
    settings.capture_retention_days = 30
    db = Database(settings.database_url)
    db.migrate()
    store = MediaStore(settings.media_root)
    old, recent = _add(db, store, age_days=31), _add(db, store, age_days=29)
    db.engine.dispose()
    db = Database(settings.database_url)
    try:
        with TestClient(create_app(settings)) as client:
            for _ in range(50):
                if old.id not in _ids(db):
                    break
                client.get("/api/health")  # gives the background task a turn
                time.sleep(0.02)
        assert _ids(db) == {recent.id}
        assert not store.path_for(old.file_name).exists()
    finally:
        db.engine.dispose()


def _free(monkeypatch, *values_mb):
    """MediaStore.free_bytes returns these values in turn (the last one repeats)."""
    values = list(values_mb)

    def free_bytes(self):
        return int((values.pop(0) if len(values) > 1 else values[0]) * MB)

    monkeypatch.setattr(MediaStore, "free_bytes", free_bytes)


def test_snapshot_refused_below_the_free_space_floor(signed_in, settings, monkeypatch):
    settings.capture_min_free_mb = 1024
    _free(monkeypatch, 500)
    resp = signed_in.post("/api/cameras/entrance/snapshot")
    assert resp.status_code == 507
    detail = resp.json()["detail"]
    assert "500 MB free" in detail and "1024 MB" in detail
    assert str(settings.media_root) not in detail
    assert signed_in.get("/api/captures").json() == []  # nothing recorded
    assert list((settings.media_root / "captures").iterdir()) == []


def test_clip_refused_below_the_free_space_floor(signed_in, settings, monkeypatch):
    settings.capture_min_free_mb = 1024
    _free(monkeypatch, 10)
    resp = signed_in.post("/api/cameras/entrance/clip", json={})
    assert resp.status_code == 507
    assert signed_in.get("/api/captures").json() == []


def test_clip_download_stops_when_space_runs_out_meanwhile(signed_in, frigate, settings, monkeypatch):
    settings.capture_min_free_mb = 1024
    _ready_export(frigate)
    _free(monkeypatch, 5000, 100)  # enough when requested, too little when the file arrives
    capture = _wait_for(signed_in, signed_in.post("/api/cameras/entrance/clip", json={}).json()["id"])
    assert capture["status"] == "failed"
    assert "free" in capture["error"] and str(settings.media_root) not in capture["error"]
    assert list((settings.media_root / "captures").iterdir()) == []


def test_free_space_floor_zero_disables_the_check(signed_in, settings, monkeypatch):
    settings.capture_min_free_mb = 0
    _free(monkeypatch, 1)
    assert signed_in.post("/api/cameras/entrance/snapshot").status_code == 201


def test_free_bytes_reads_the_media_filesystem(tmp_path):
    assert MediaStore(tmp_path).free_bytes() > 0
