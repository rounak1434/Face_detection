"""Rendering Frigate's config from the template plus approved saved devices (ADR-0003, step 1)."""

import re
from pathlib import Path

import pytest
import yaml

from ai_surveillance.frigate_config import (
    GENERATED_HEADER,
    FrigateDevice,
    render_config,
    secret_name,
)

TEMPLATE = (Path(__file__).resolve().parents[3] / "deploy" / "frigate" / "config.yml").read_text()
ID_A = "a" * 32
ID_B = "0123456789abcdef0123456789abcdef"
PLACEHOLDER = re.compile(r"^\{FRIGATE_[A-Z0-9_]+\}$")


def _render(*devices):
    text, names = render_config(TEMPLATE, list(devices))
    return text, yaml.safe_load(text), names


def _assert_no_url_can_carry_credentials(cfg):
    """Every source URL is a placeholder; cameras only read go2rtc's local restream."""
    for name, sources in cfg["go2rtc"]["streams"].items():
        for src in sources:
            assert PLACEHOLDER.match(src), f"go2rtc stream {name} has a literal source: {src!r}"
    for cam, conf in cfg["cameras"].items():
        for inp in conf["ffmpeg"]["inputs"]:
            assert inp["path"].startswith("rtsp://127.0.0.1:8554/"), f"{cam} reads {inp['path']!r}"
            assert inp["path"].removeprefix("rtsp://127.0.0.1:8554/") in cfg["go2rtc"]["streams"]


def test_no_devices_keeps_the_template_unchanged():
    text, cfg, names = _render()
    assert text.startswith(GENERATED_HEADER)
    assert cfg == yaml.safe_load(TEMPLATE)
    assert names == {}


def test_device_with_main_and_sub_stream():
    text, cfg, names = _render(FrigateDevice(ID_A, "Gate Camera", has_sub=True, detect_width=640, detect_height=360))
    cam = names[ID_A]
    assert cam == "gate_camera"
    assert cfg["go2rtc"]["streams"][cam] == [f"{{{secret_name(ID_A, 'main')}}}"]
    assert cfg["go2rtc"]["streams"][f"{cam}_sub"] == [f"{{{secret_name(ID_A, 'sub')}}}"]
    camera = cfg["cameras"][cam]
    inputs = camera["ffmpeg"]["inputs"]
    assert [(i["path"], i["roles"]) for i in inputs] == [
        (f"rtsp://127.0.0.1:8554/{cam}", ["record"]),
        (f"rtsp://127.0.0.1:8554/{cam}_sub", ["detect"]),
    ]
    assert all(i["input_args"] == "preset-rtsp-restream" for i in inputs)
    assert camera["detect"] == {"enabled": True, "width": 640, "height": 360, "fps": 5}
    assert camera["live"]["streams"] == {"Sub": f"{cam}_sub", "Main": cam}
    # Recording, snapshots and tracked objects follow the template's camera.
    reference = yaml.safe_load(TEMPLATE)["cameras"]["entrance"]
    for key in ("objects", "record", "snapshots"):
        assert camera[key] == reference[key]
    assert cfg["cameras"]["entrance"] == reference  # the .env camera stays as it was
    _assert_no_url_can_carry_credentials(cfg)


def test_device_without_sub_stream_detects_on_main():
    _, cfg, names = _render(FrigateDevice(ID_A, "Yard", has_sub=False, detect_width=None, detect_height=None))
    cam = names[ID_A]
    camera = cfg["cameras"][cam]
    assert camera["ffmpeg"]["inputs"] == [
        {"path": f"rtsp://127.0.0.1:8554/{cam}", "input_args": "preset-rtsp-restream", "roles": ["record", "detect"]}
    ]
    assert f"{cam}_sub" not in cfg["go2rtc"]["streams"]
    assert camera["detect"] == {"enabled": True, "fps": 5}  # Frigate detects the size itself
    assert camera["live"]["streams"] == {"Main": cam}
    _assert_no_url_can_carry_credentials(cfg)


@pytest.mark.parametrize("name,expected", [
    ("Front Door #1", "front_door_1"),
    ("  Läden/Hof  ", "l_den_hof"),
    ("UPPER-case", "upper_case"),
    ("***", "camera"),
    ("x" * 64, "x" * 40),
])
def test_camera_names_are_safe_for_frigate(name, expected):
    _, cfg, names = _render(FrigateDevice(ID_A, name, has_sub=False, detect_width=None, detect_height=None))
    assert names[ID_A] == expected
    assert re.fullmatch(r"[a-z0-9_]+", names[ID_A])


def test_name_collisions_get_a_stable_suffix():
    a = FrigateDevice(ID_A, "Entrance", has_sub=False, detect_width=None, detect_height=None)  # clashes with the template
    b = FrigateDevice(ID_B, "entrance", has_sub=True, detect_width=None, detect_height=None)
    _, cfg, names = _render(a, b)
    assert names == {ID_A: "entrance_aaaaaa", ID_B: "entrance_012345"}
    assert set(cfg["cameras"]) == {"entrance", "entrance_aaaaaa", "entrance_012345"}
    assert _render(a, b)[2] == names  # same input, same names
    _assert_no_url_can_carry_credentials(cfg)


def test_a_device_name_cannot_take_over_a_sub_stream_name():
    # "entrance_sub" is the template's sub stream in go2rtc.
    _, cfg, names = _render(FrigateDevice(ID_A, "entrance sub", has_sub=False, detect_width=None, detect_height=None))
    assert names[ID_A] == "entrance_sub_aaaaaa"
    assert cfg["go2rtc"]["streams"]["entrance_sub"] == yaml.safe_load(TEMPLATE)["go2rtc"]["streams"]["entrance_sub"]


@pytest.mark.parametrize("device_id", ["", "abc", "A" * 32, "g" * 32, "../" + "a" * 29, "a" * 33])
def test_invalid_device_ids_are_refused(device_id):
    with pytest.raises(ValueError):
        render_config(TEMPLATE, [FrigateDevice(device_id, "Cam", has_sub=False, detect_width=None, detect_height=None)])


def test_duplicate_devices_are_refused():
    dev = FrigateDevice(ID_A, "Cam", has_sub=False, detect_width=None, detect_height=None)
    with pytest.raises(ValueError):
        render_config(TEMPLATE, [dev, dev])


@pytest.mark.parametrize("width,height", [(0, 360), (640, 0), (640, None), (100000, 360)])
def test_implausible_detect_sizes_are_left_to_frigate(width, height):
    _, cfg, names = _render(FrigateDevice(ID_A, "Cam", has_sub=False, detect_width=width, detect_height=height))
    assert cfg["cameras"][names[ID_A]]["detect"] == {"enabled": True, "fps": 5}


@pytest.mark.parametrize("template", [
    "", "- a list\n", "cameras: []\n", "go2rtc: {streams: {}}\n", "cameras: {}\ngo2rtc: {streams: []}\n", "a: [unclosed\n",
])
def test_malformed_templates_are_refused(template):
    with pytest.raises(ValueError):
        render_config(template, [])


def test_secret_names_follow_frigate_rules():
    assert secret_name(ID_B, "main") == "FRIGATE_DEV_0123456789ABCDEF0123456789ABCDEF_MAIN"
    assert secret_name(ID_B, "sub").startswith("FRIGATE_")  # Frigate only substitutes FRIGATE_* names
    with pytest.raises(ValueError):
        secret_name(ID_B, "other")
