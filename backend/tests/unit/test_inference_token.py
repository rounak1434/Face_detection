"""The api authenticates to the inference service with INFERENCE_API_TOKEN."""

import pytest
from pydantic import SecretStr

from .conftest import INFERENCE
from .test_analysis import DETECT_OK, _post

TOKEN = "api-to-inference-token-abcdef"


@pytest.fixture
def settings(settings):
    settings.inference_api_token = SecretStr(TOKEN)
    return settings


def test_token_is_sent_on_every_inference_call(signed_in, frigate):
    detect = frigate.post(f"{INFERENCE}/v1/detect").respond(200, json=DETECT_OK)
    models = frigate.get(f"{INFERENCE}/v1/models").respond(200, json=[])
    assert _post(signed_in).status_code == 200
    assert signed_in.get("/api/models").status_code == 200
    for route in (detect, models):
        assert route.calls.last.request.headers["authorization"] == f"Bearer {TOKEN}"


def test_rejected_token_is_reported_as_a_configuration_problem(signed_in, frigate):
    frigate.post(f"{INFERENCE}/v1/detect").respond(401, json={"error": "unauthorized", "detail": "x"})
    frigate.get(f"{INFERENCE}/v1/models").respond(401, json={"error": "unauthorized", "detail": "x"})
    resp = _post(signed_in)
    assert resp.status_code == 503
    assert resp.json()["error"] == "inference_unavailable"
    assert "INFERENCE_API_TOKEN" in resp.json()["detail"]
    models = signed_in.get("/api/models").json()
    assert models["inference"]["reachable"] is False and "INFERENCE_API_TOKEN" in models["inference"]["detail"]
    assert TOKEN not in resp.text and TOKEN not in str(models)


def test_settings_never_print_the_token(settings):
    assert TOKEN not in repr(settings) and TOKEN not in str(settings.model_dump())
