"""Static consistency checks of deploy/ (compose, Frigate config, Caddy, env).

These parse the files and cross-check names, ports and variables. They do
not start Docker or Frigate: a file that passes here can still be rejected
by Frigate 0.18 at startup (that is checked on the Linux host).
"""

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
DEPLOY = ROOT / "deploy"


@pytest.fixture(scope="module")
def compose():
    return yaml.safe_load((DEPLOY / "compose.yaml").read_text())


@pytest.fixture(scope="module")
def frigate():
    return yaml.safe_load((DEPLOY / "frigate" / "config.yml").read_text())


def _env_example() -> dict[str, str]:
    values = {}
    for line in (DEPLOY / ".env.example").read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            values[key] = value
    return values


def test_yaml_files_parse():
    for path in ["compose.yaml", "compose.nvidia.yaml", "frigate/config.yml", "test-stream/mediamtx.yml"]:
        assert yaml.safe_load((DEPLOY / path).read_text()), path


def test_build_contexts_have_dockerfiles(compose):
    for name, svc in compose["services"].items():
        if "build" in svc:
            context = (DEPLOY / svc["build"]).resolve()
            assert (context / "Dockerfile").is_file(), f"{name}: no Dockerfile in {context}"


def test_bind_mount_sources_exist(compose):
    for name, svc in compose["services"].items():
        for volume in svc.get("volumes", []):
            if isinstance(volume, str) and volume.startswith("./"):
                source = volume.split(":")[0]
                if source.startswith("./storage"):
                    continue  # created by Docker at first start (gitignored)
                assert (DEPLOY / source).exists(), f"{name}: missing {source}"


def test_required_variables_are_in_env_example(compose):
    text = (DEPLOY / "compose.yaml").read_text()
    required = set(re.findall(r"\$\{([A-Z_]+):\?", text))
    example = _env_example()
    assert required, "expected at least one required variable"
    missing = required - example.keys()
    assert not missing, f"required by compose but absent from .env.example: {missing}"


def test_env_example_contains_no_real_credentials():
    for key, value in _env_example().items():
        assert "@" not in value or not value.startswith("rtsp://"), f"{key} has credentials"
        if "PASSWORD" in key or key.endswith("_KEY"):
            assert value == "", f"{key} must be empty in the example"


def test_example_defaults_start_the_test_stream():
    example = _env_example()
    assert "test-stream" in example.get("COMPOSE_PROFILES", "")
    assert example["FRIGATE_CAMERA_MAIN_URL"].startswith("rtsp://test-stream:8554/")


def test_only_web_is_published_on_all_interfaces(compose):
    for name, svc in compose["services"].items():
        for port in svc.get("ports", []):
            if name != "web":
                assert str(port).startswith("127.0.0.1:"), f"{name} publishes {port} beyond loopback"
    frigate_ports = " ".join(map(str, compose["services"]["frigate"].get("ports", [])))
    assert "5000" not in frigate_ports, "Frigate's unauthenticated port must not be published"


def test_api_talks_to_frigate_internal_port(compose):
    env = compose["services"]["api"]["environment"]
    assert env["FRIGATE_URL"].endswith("http://frigate:5000}")
    assert "env_file" not in compose["services"]["api"], "camera credentials must not reach the api container"


def test_capture_storage_settings_reach_the_api(compose):
    env = compose["services"]["api"]["environment"]
    example = _env_example()
    assert env["CAPTURE_MIN_FREE_MB"] == "${CAPTURE_MIN_FREE_MB:-1024}"
    assert env["CAPTURE_RETENTION_DAYS"] == "${CAPTURE_RETENTION_DAYS:-0}"  # never deletes unless asked
    assert example["CAPTURE_MIN_FREE_MB"] == "1024" and example["CAPTURE_RETENTION_DAYS"] == "0"


def test_frigate_export_cleanup_setting_reaches_the_api(compose):
    assert compose["services"]["api"]["environment"]["CLIP_KEEP_FRIGATE_EXPORT"] == "${CLIP_KEEP_FRIGATE_EXPORT:-false}"
    assert _env_example()["CLIP_KEEP_FRIGATE_EXPORT"] == "false"


def test_inference_token_reaches_both_services_and_has_no_example_value(compose):
    for service in ("api", "inference"):
        assert compose["services"][service]["environment"]["INFERENCE_API_TOKEN"] == "${INFERENCE_API_TOKEN:-}"
    assert _env_example()["INFERENCE_API_TOKEN"] == "", "each install must generate its own token"


def test_frigate_env_placeholders_are_provided(compose, frigate):
    text = (DEPLOY / "frigate" / "config.yml").read_text()
    used = set(re.findall(r"\{(FRIGATE_[A-Z_]+)\}", text))
    provided = set(compose["services"]["frigate"]["environment"].keys())
    assert used <= provided, f"Frigate config uses {used - provided} which compose does not pass"


def test_frigate_restream_inputs_match_go2rtc_streams(frigate):
    streams = set(frigate["go2rtc"]["streams"])
    for name, cam in frigate["cameras"].items():
        for inp in cam["ffmpeg"]["inputs"]:
            m = re.match(r"rtsp://127\.0\.0\.1:8554/(.+)$", inp["path"])
            assert m and m.group(1) in streams, f"{name}: input {inp['path']} is not a go2rtc stream"
        for stream in cam.get("live", {}).get("streams", {}).values():
            assert stream in streams, f"{name}: live stream {stream} is not defined in go2rtc"
        roles = {r for inp in cam["ffmpeg"]["inputs"] for r in inp["roles"]}
        assert {"record", "detect"} <= roles


def test_frigate_018_record_schema(frigate):
    for name, cam in frigate["cameras"].items():
        record = cam["record"]
        assert "retain" not in record, "record.retain was removed in Frigate 0.18"
        assert record["enabled"] is True and record["continuous"]["days"] > 0


def test_test_stream_resolution_matches_detect(frigate):
    mediamtx = (DEPLOY / "test-stream" / "mediamtx.yml").read_text()
    size = re.search(r"testsrc2=size=(\d+)x(\d+)", mediamtx)
    detect = frigate["cameras"]["entrance"]["detect"]
    assert (int(size.group(1)), int(size.group(2))) == (detect["width"], detect["height"])


def test_caddy_proxies_to_api_port():
    caddy = (DEPLOY / "caddy" / "Caddyfile").read_text()
    dockerfile = (ROOT / "backend" / "Dockerfile").read_text()
    assert "reverse_proxy api:8000" in caddy
    assert '"--port", "8000"' in dockerfile


def test_inference_service_is_internal_and_reachable_by_api(compose):
    inference = compose["services"]["inference"]
    assert "ports" not in inference, "the inference service must not be published"
    assert (ROOT / "inference" / "Dockerfile").is_file()
    dockerfile = (ROOT / "inference" / "Dockerfile").read_text()
    port = re.search(r'"--port", "(\d+)"', dockerfile).group(1)
    assert compose["services"]["api"]["environment"]["INFERENCE_URL"] == f"http://inference:{port}"
    assert "inference_service.download" in dockerfile, "models must be fetched and verified at build time"


def test_inference_has_a_memory_limit(compose):
    assert compose["services"]["inference"]["mem_limit"].startswith("${INFERENCE_MEM_LIMIT:-")


@pytest.mark.parametrize("service", ["backend", "inference"])
def test_images_install_with_pinned_constraints(service):
    dockerfile = (ROOT / service / "Dockerfile").read_text()
    assert "-c constraints.txt" in dockerfile and "constraints.txt" in dockerfile.split("COPY", 2)[1]
    pins = [l for l in (ROOT / service / "constraints.txt").read_text().splitlines() if l and not l.startswith("#")]
    assert pins and all("==" in p for p in pins), "constraints must be exact pins"
    assert not any(p.lower().startswith("ai-surveillance") for p in pins), "the package itself must not be pinned"
    ignored = (ROOT / service / ".dockerignore").read_text().split()
    assert "constraints.txt" not in ignored


def test_nvidia_override_switches_image(compose):
    override = yaml.safe_load((DEPLOY / "compose.nvidia.yaml").read_text())
    image = override["services"]["frigate"]["image"]
    assert image.endswith("-tensorrt")
    assert image.split(":")[1].startswith(compose["services"]["frigate"]["image"].split(":")[1])
