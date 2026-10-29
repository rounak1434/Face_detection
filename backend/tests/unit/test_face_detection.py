"""Face detection on Frigate person events (ADR-0004, Phase 2).

Mocked: Frigate (respx, shapes from the v0.18.2 source) and, except in the last
test, the inference service. The last test runs the real YuNet model in-process
when the inference package and its verified model are installed alongside (WSL
check); it is skipped otherwise, and CI's backend job skips it.
"""

import asyncio
import base64
import json
from pathlib import Path

import httpx
import pytest
import respx

from ai_surveillance import face_detection as fd
from ai_surveillance.frigate_client import FrigateClient, FrigateError, FrigateUnreachable, PersonEvent
from ai_surveillance.inference_client import InferenceClient, InferenceUnavailable

from .conftest import FRIGATE, INFERENCE, JPEG

EVENT = PersonEvent("1761730000.123456-abc123", "entrance", 1761730000.12, 1761730009.5)
MAIN_JPEG = b"\xff\xd8\xff\xe0main-stream-frame\xff\xd9"
CROP = b"\xff\xd8\xff\xe0crop\xff\xd9"
FACE = {"index": 0, "box": {"x": 10, "y": 20, "w": 80, "h": 100}, "score": 0.93,
        "keypoints5": [[30, 50], [60, 50], [45, 70], [35, 90], [55, 90]],
        "quality": {"face_px": 80, "small_face": False, "touches_edge": False, "sharpness": 120.0},
        "crop": {"box": {"x": 0, "y": 0, "w": 96, "h": 120}, "width": 96, "height": 120,
                 "jpeg_base64": base64.b64encode(CROP).decode()}}
SNAPSHOT_PATH = f"/api/events/{EVENT.id}/snapshot.jpg"
RECORDING_PATH = "/api/entrance/recordings/1761730004.250000/snapshot.jpg"


def detect_result(faces):
    return {"status": "faces_detected" if faces else "no_faces_detected",
            "image": {"width": 1920, "height": 1080, "format": "image/jpeg"}, "faces": faces,
            "face_count": len(faces), "model": {"name": "yunet", "version": "2023mar"}, "processing_ms": 12.0}


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def mock():
    with respx.mock(assert_all_called=False) as router:
        router.get(f"{FRIGATE}{SNAPSHOT_PATH}", name="snapshot").respond(
            200, content=JPEG, headers={"content-type": "image/jpeg", "x-frame-time": "1761730004.25"})
        router.get(f"{FRIGATE}{RECORDING_PATH}", name="recording").respond(
            200, content=MAIN_JPEG, headers={"content-type": "image/jpeg"})
        router.post(f"{INFERENCE}/v1/detect", name="detect").respond(200, json=detect_result([FACE]))
        yield router


def detect(settings):
    async def go():
        frigate, inference = FrigateClient(settings), InferenceClient(settings)
        try:
            return await fd.detect_event_faces(EVENT, frigate, inference, settings)
        finally:
            await frigate.aclose()
            await inference.aclose()
    return run(go())


# --- Frigate client -----------------------------------------------------------

def test_person_events_query_matches_frigate_and_skips_malformed_entries(settings):
    with respx.mock() as router:
        route = router.get(f"{FRIGATE}/api/events").respond(200, json=[
            {"id": "1761730000.1-a1", "camera": "entrance", "start_time": 1761730000.1, "end_time": 1761730005.0,
             "label": "person", "has_snapshot": True, "data": {}},
            {"id": "../etc", "camera": "entrance", "start_time": 1.0, "end_time": 2.0},  # unsafe id
            {"id": "1761730001.1-b2", "camera": "entrance", "start_time": 1761730001.1, "end_time": None},  # in progress
            "not an object",
        ])
        events = run(FrigateClient(settings).person_events(after=1761729000.0, limit=25))
    assert events == [PersonEvent("1761730000.1-a1", "entrance", 1761730000.1, 1761730005.0)]
    params = route.calls.last.request.url.params
    assert dict(params) == {"labels": "person", "has_snapshot": "1", "in_progress": "0",
                            "after": "1761729000.0", "sort": "date_asc", "limit": "25"}


def test_person_events_rejects_a_non_list(settings):
    with respx.mock() as router:
        router.get(f"{FRIGATE}/api/events").respond(200, json={"success": False})
        with pytest.raises(FrigateError, match="not a list"):
            run(FrigateClient(settings).person_events(after=0))


def test_event_snapshot_asks_for_a_clean_frame_and_reads_the_frame_time(settings, mock):
    jpeg, frame_time = run(FrigateClient(settings).event_snapshot(EVENT.id))
    assert jpeg == JPEG and frame_time == 1761730004.25
    params = mock["snapshot"].calls.last.request.url.params
    assert (params["bbox"], params["timestamp"], params["crop"]) == ("0", "0", "0")  # no overlay on the face


@pytest.mark.parametrize("headers, expected", [({}, None), ({"x-frame-time": "nan-ish"}, None),
                                                ({"x-frame-time": "0"}, None)])
def test_event_snapshot_without_a_usable_frame_time(settings, headers, expected):
    with respx.mock() as router:
        router.get(f"{FRIGATE}{SNAPSHOT_PATH}").respond(200, content=JPEG, headers={"content-type": "image/jpeg", **headers})
        assert run(FrigateClient(settings).event_snapshot(EVENT.id))[1] is expected


def test_event_snapshot_errors(settings):
    from ai_surveillance.frigate_client import FrigateNotFound

    client = FrigateClient(settings)
    with respx.mock() as router:
        route = router.get(f"{FRIGATE}{SNAPSHOT_PATH}")
        route.respond(404, json={"success": False, "message": "Snapshot not available"})
        with pytest.raises(FrigateNotFound):
            run(client.event_snapshot(EVENT.id))
        route.respond(200, json={"not": "an image"})
        with pytest.raises(FrigateError, match="HTTP 200"):
            run(client.event_snapshot(EVENT.id))


# --- detect_event_faces -------------------------------------------------------

def test_uses_the_recording_frame_and_returns_faces(settings, mock):
    result = detect(settings)
    assert result.status == fd.FACES_DETECTED and result.frame_source == "recording"
    assert result.frame_jpeg == MAIN_JPEG and result.frame_time == 1761730004.25
    assert mock["detect"].calls.last.request.content == MAIN_JPEG
    assert dict(mock["detect"].calls.last.request.url.params) == {"min_score": "0.7", "max_faces": "10"}
    (face,) = result.faces
    assert face.box == FACE["box"] and face.score == 0.93 and face.crop_jpeg == CROP
    assert (result.frame_width, result.frame_height) == (1920, 1080)
    assert result.model == {"name": "yunet", "version": "2023mar"}


def test_falls_back_to_the_snapshot_when_no_recording_covers_the_frame(settings, mock):
    mock["recording"].respond(404, json={"success": False, "message": "Recording not found at 1761730004.25"})
    result = detect(settings)
    assert result.status == fd.FACES_DETECTED and result.frame_source == "snapshot"
    assert mock["detect"].calls.last.request.content == JPEG
    assert result.detail == "no recording covers the frame time"


def test_snapshot_source_never_asks_for_the_recording(settings, mock):
    settings.face_frame_source = "snapshot"
    result = detect(settings)
    assert result.frame_source == "snapshot" and not mock["recording"].called


def test_snapshot_without_frame_time_cannot_use_the_recording(settings, mock):
    mock["snapshot"].respond(200, content=JPEG, headers={"content-type": "image/jpeg"})
    result = detect(settings)
    assert result.frame_source == "snapshot" and not mock["recording"].called
    assert "no frame time" in result.detail


def test_no_faces_is_a_normal_result(settings, mock):
    mock["detect"].respond(200, json=detect_result([]))
    result = detect(settings)
    assert result.status == fd.NO_FACES_DETECTED and result.faces == [] and result.frame_jpeg == MAIN_JPEG


def test_missing_snapshot_is_final(settings, mock):
    mock["snapshot"].respond(404, json={"success": False, "message": "Snapshot not available"})
    result = detect(settings)
    assert result.status == fd.FRAME_UNAVAILABLE and result.frame_jpeg is None
    assert not mock["detect"].called


@pytest.mark.parametrize("code, status", [("invalid_image", 400), ("unsupported_format", 415), ("too_large", 413)])
def test_a_frame_the_detector_refuses_is_final(settings, mock, code, status):
    mock["detect"].respond(status, json={"error": code, "detail": "x"})
    result = detect(settings)
    assert result.status == fd.FRAME_REJECTED and code in result.detail


def test_detector_failure_on_the_frame_is_final(settings, mock):
    mock["detect"].respond(500, json={"error": "inference_failed", "detail": "The face detector failed on this image"})
    assert detect(settings).status == fd.INFERENCE_FAILED


@pytest.mark.parametrize("response", [
    httpx.Response(503, json={"error": "model_unavailable", "detail": "missing", "model_status": "missing"}),
    httpx.Response(502, text="bad gateway"),
    httpx.Response(200, json={"unexpected": True}),
])
def test_service_problems_are_temporary(settings, mock, response):
    mock["detect"].mock(return_value=response)
    with pytest.raises(InferenceUnavailable):
        detect(settings)


def test_unreachable_services_are_temporary(settings, mock):
    mock["detect"].mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(InferenceUnavailable):
        detect(settings)
    mock["snapshot"].mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(FrigateUnreachable):
        detect(settings)


def test_frigate_server_error_on_the_recording_is_temporary(settings, mock):
    mock["recording"].respond(500, json={"success": False, "message": "boom"})
    with pytest.raises(FrigateError):
        detect(settings)


def test_damaged_crop_is_dropped_not_fatal(settings, mock):
    damaged = {**FACE, "crop": {**FACE["crop"], "jpeg_base64": "%%%not-base64"}}
    mock["detect"].respond(200, json=detect_result([damaged]))
    (face,) = detect(settings).faces
    assert face.crop_jpeg is None and face.score == 0.93


# --- real model ---------------------------------------------------------------

FIXTURE = Path(__file__).resolve().parents[3] / "inference" / "tests" / "data" / "public_domain" / "nasa_s69-31741_480x600.jpg"
MODELS = Path(__file__).resolve().parents[3] / "inference" / "models"


def test_real_yunet_finds_the_face_in_a_frigate_snapshot(settings):
    """Real detector, in-process: the public-domain fixture served as a Frigate event snapshot."""
    pytest.importorskip("inference_service", reason="inference package not installed in this environment")
    from inference_service.adapters.yunet import YuNetDetector
    from inference_service.config import Settings as InferenceSettings
    from inference_service.main import create_app
    from inference_service.registry import READY, YUNET, verify_file

    if verify_file(YUNET, MODELS)[0] != READY:
        pytest.skip("verified YuNet model not present (python -m inference_service.download)")
    settings.face_frame_source = "snapshot"
    app = create_app(InferenceSettings(models_dir=MODELS), detector=YuNetDetector(MODELS / YUNET.file_name))

    async def go():
        async with app.router.lifespan_context(app):
            frigate = FrigateClient(settings)
            inference = InferenceClient(settings, transport=httpx.ASGITransport(app=app))
            with respx.mock() as router:
                router.get(f"{FRIGATE}{SNAPSHOT_PATH}").respond(
                    200, content=FIXTURE.read_bytes(), headers={"content-type": "image/jpeg", "x-frame-time": "1.5"})
                return await fd.detect_event_faces(EVENT, frigate, inference, settings)

    result = run(go())
    assert result.status == fd.FACES_DETECTED and len(result.faces) == 1
    box = result.faces[0].box
    # Same face as AI-ANALYSIS.md's real run on this fixture: (257, 115, 76, 102).
    assert abs(box["x"] - 257) <= 4 and abs(box["y"] - 115) <= 4 and abs(box["w"] - 76) <= 4
    assert result.faces[0].score > 0.9 and result.faces[0].crop_jpeg.startswith(b"\xff\xd8")
    assert result.model == {"name": "yunet", "version": "2023mar"}
    print(json.dumps({"box": box, "score": result.faces[0].score}))
