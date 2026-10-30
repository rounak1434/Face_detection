"""Forwarded-header trust behind the web proxy, with uvicorn's real ProxyHeadersMiddleware.

The trusted address is read from deploy/compose.yaml, so this tests the configuration that ships:
requests from the web proxy carry the browser's address (and the sign-in lockout is per browser);
the same header from any other container is ignored.
"""

import re
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from ai_surveillance.main import create_app

from .conftest import ADMIN_PASSWORD, ADMIN_USER

COMPOSE = yaml.safe_load((Path(__file__).resolve().parents[3] / "deploy" / "compose.yaml").read_text())
TRUSTED = re.fullmatch(r"\$\{WEB_EDGE_IP:-([^}]+)\}", COMPOSE["services"]["api"]["environment"]["FORWARDED_ALLOW_IPS"]).group(1)
OTHER_CONTAINER = "172.31.250.3"  # e.g. something else that could reach the api


@pytest.fixture
def behind_proxy(client, settings):
    """One app (one lockout counter), wrapped like uvicorn --proxy-headers; one client per source address."""
    wrapped = ProxyHeadersMiddleware(create_app(settings), trusted_hosts=TRUSTED)
    first = None

    def make(source_ip: str) -> TestClient:
        nonlocal first
        tc = TestClient(wrapped, client=(source_ip, 40000))
        if first is None:  # run the app's startup once; later clients share its state
            tc.__enter__()
            first = tc
        return tc

    yield make
    if first is not None:
        first.__exit__(None, None, None)


def _login(tc: TestClient, password: str, browser: str | None):
    headers = {"X-Forwarded-For": browser} if browser else {}
    return tc.post("/api/auth/login", json={"username": ADMIN_USER, "password": password}, headers=headers)


def test_lockout_is_per_browser_behind_the_trusted_proxy(behind_proxy, settings):
    proxy = behind_proxy(TRUSTED)
    for _ in range(settings.login_max_failures):
        assert _login(proxy, "wrong-password-123", "203.0.113.5").status_code == 401
    assert _login(proxy, ADMIN_PASSWORD, "203.0.113.5").status_code == 429  # that browser is locked out
    assert _login(proxy, ADMIN_PASSWORD, "198.51.100.7").status_code == 200  # another browser is not


def test_forwarded_header_from_another_container_is_ignored(behind_proxy, settings):
    proxy = behind_proxy(TRUSTED)
    other = behind_proxy(OTHER_CONTAINER)
    # It claims to be many different browsers to dodge the lockout; the claim is ignored.
    for i in range(settings.login_max_failures):
        assert _login(other, "wrong-password-123", f"203.0.113.{i + 10}").status_code == 401
    assert _login(other, ADMIN_PASSWORD, "203.0.113.99").status_code == 429
    # ...and it did not lock out the real browsers it named (same app, same lockout counter).
    assert _login(proxy, ADMIN_PASSWORD, "203.0.113.10").status_code == 200


def test_control_a_trusted_source_naming_a_browser_does_count_for_that_browser(behind_proxy, settings):
    """Shows the counter really is shared, so the test above is meaningful."""
    proxy = behind_proxy(TRUSTED)
    behind_proxy(OTHER_CONTAINER)
    for _ in range(settings.login_max_failures):
        _login(proxy, "wrong-password-123", "203.0.113.10")
    assert _login(proxy, ADMIN_PASSWORD, "203.0.113.10").status_code == 429


def test_the_proxy_without_a_header_is_treated_as_itself(behind_proxy, settings):
    proxy = behind_proxy(TRUSTED)
    for _ in range(settings.login_max_failures):
        _login(proxy, "wrong-password-123", None)
    assert _login(proxy, ADMIN_PASSWORD, None).status_code == 429
    assert _login(proxy, ADMIN_PASSWORD, "203.0.113.5").status_code == 200
