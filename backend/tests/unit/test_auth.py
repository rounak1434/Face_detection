import pytest

from ai_surveillance.security import LoginThrottle, hash_password

from .conftest import ADMIN_PASSWORD, ADMIN_USER


def test_protected_routes_require_login(client):
    assert client.get("/api/cameras").status_code == 401
    assert client.get("/api/captures").status_code == 401
    assert client.post("/api/cameras/entrance/snapshot").status_code == 401
    assert client.get("/api/auth/me").status_code == 401


def test_login_sets_hardened_cookie(client):
    resp = client.post("/api/auth/login", json={"username": ADMIN_USER, "password": ADMIN_PASSWORD})
    assert resp.status_code == 200
    cookie = resp.headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert "samesite=strict" in cookie
    assert client.get("/api/auth/me").json() == {"username": ADMIN_USER}


def test_wrong_password_rejected(client):
    resp = client.post("/api/auth/login", json={"username": ADMIN_USER, "password": "wrong-password-123"})
    assert resp.status_code == 401
    resp = client.post("/api/auth/login", json={"username": "nobody", "password": "wrong-password-123"})
    assert resp.status_code == 401


def test_lockout_after_repeated_failures(client):
    for _ in range(5):
        client.post("/api/auth/login", json={"username": ADMIN_USER, "password": "nope-nope-nope"})
    resp = client.post("/api/auth/login", json={"username": ADMIN_USER, "password": ADMIN_PASSWORD})
    assert resp.status_code == 429


def test_logout_revokes_session(signed_in):
    assert signed_in.get("/api/auth/me").status_code == 200
    assert signed_in.post("/api/auth/logout").status_code == 204
    assert signed_in.get("/api/auth/me").status_code == 401


def test_short_passwords_refused():
    with pytest.raises(ValueError):
        hash_password("short")


def test_password_reset_signs_out_everywhere(signed_in, settings, monkeypatch):
    from ai_surveillance import cli, config

    monkeypatch.setattr(config, "get_settings", lambda: settings)
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": "a-brand-new-password")
    assert cli.create_admin(ADMIN_USER) == 0
    assert signed_in.get("/api/auth/me").status_code == 401
    resp = signed_in.post("/api/auth/login", json={"username": ADMIN_USER, "password": "a-brand-new-password"})
    assert resp.status_code == 200


def test_throttle_forgets_old_addresses():
    throttle = LoginThrottle(max_failures=2, lockout_seconds=0)
    throttle.record_failure("a")
    assert not throttle.is_locked("a")
    assert throttle._failures == {}


def test_throttle_reset():
    throttle = LoginThrottle(max_failures=2, lockout_seconds=60)
    throttle.record_failure("a")
    throttle.record_failure("a")
    assert throttle.is_locked("a")
    assert not throttle.is_locked("b")
    throttle.reset("a")
    assert not throttle.is_locked("a")
