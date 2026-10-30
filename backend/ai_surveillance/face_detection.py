"""Face detection on one Frigate person event (ADR-0004, Phase 2).

Fetches the event's best frame from Frigate and runs the existing YuNet detector
through the inference service. Nothing is stored here: the caller (the ingestion
loop, Phase 3) decides what to keep.

Outcomes:
- a result with a status: faces_detected, no_faces_detected, frame_unavailable
  (Frigate has no frame for this event), frame_rejected (the detector refused the
  image), inference_failed (the detector failed on this image). These are final
  for the event.
- an exception: FrigateError or InferenceUnavailable when Frigate or the inference
  service is unreachable, refuses the token, or the model is not loaded. These are
  temporary; the event should be tried again later.

Detection says where faces are, never who they are. Only when the caller asks for recognition
(the switch is on, Phase 6) does it also return an embedding per face for the matcher.
"""

import base64
import binascii
import logging
from dataclasses import dataclass, field

from .config import Settings
from .frigate_client import FrigateClient, FrigateNotFound, PersonEvent
from .inference_client import InferenceClient, InferenceUnavailable

log = logging.getLogger(__name__)

FACES_DETECTED = "faces_detected"
NO_FACES_DETECTED = "no_faces_detected"
FRAME_UNAVAILABLE = "frame_unavailable"
FRAME_REJECTED = "frame_rejected"
INFERENCE_FAILED = "inference_failed"
GAVE_UP = "gave_up"  # set by the ingestion loop after repeated temporary failures
FACE_EVENT_STATUSES = frozenset({FACES_DETECTED, NO_FACES_DETECTED, FRAME_UNAVAILABLE, FRAME_REJECTED,
                                 INFERENCE_FAILED, GAVE_UP})

# Inference error codes that are about this image (final), as opposed to the service (temporary).
_IMAGE_ERRORS = {"invalid_image", "unsupported_format", "too_large"}


@dataclass
class DetectedFace:
    box: dict  # {"x", "y", "w", "h"} in frame pixels, clipped to the frame
    score: float  # detection confidence: a face is there, not who it is
    keypoints5: list[list[float]]  # right eye, left eye, nose tip, right/left mouth corner (raw, may lie outside)
    quality: dict  # face_px, small_face, touches_edge, sharpness (heuristics)
    crop_jpeg: bytes | None
    embedding: tuple[float, ...] | None = None  # Phase 6, only while recognition is on; never stored
    match: object | None = None  # recognition.Match, set by the ingestion loop


@dataclass
class EventFaces:
    event: PersonEvent
    status: str
    frame_source: str | None = None  # "recording" or "snapshot"
    frame_time: float | None = None
    frame_jpeg: bytes | None = None
    frame_width: int | None = None
    frame_height: int | None = None
    faces: list[DetectedFace] = field(default_factory=list)
    model: dict | None = None
    detail: str | None = None
    recognition_state: str = "off"  # off | unavailable | done (recognition.STATE_*)
    embedder: str | None = None  # e.g. "sface 2021dec" when recognition_state is done


async def _frame(event: PersonEvent, frigate: FrigateClient, settings: Settings) -> tuple[bytes, str, float | None, str | None]:
    """(jpeg, source, frame_time, note). Raises FrigateNotFound when Frigate has no frame at all."""
    snapshot, frame_time = await frigate.event_snapshot(event.id)
    if settings.face_frame_source == "recording":
        if frame_time is None:
            return snapshot, "snapshot", None, "the snapshot has no frame time, so the recording was not used"
        try:
            return await frigate.recording_snapshot(event.camera, frame_time), "recording", frame_time, None
        except FrigateNotFound:
            return snapshot, "snapshot", frame_time, "no recording covers the frame time"
    return snapshot, "snapshot", frame_time, None


_BOX = ("x", "y", "w", "h")


def _number(value) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _valid_face(face) -> bool:
    """Only well-formed faces are kept: a malformed entry must not stall storage later."""
    if not isinstance(face, dict):
        return False
    box, points = face.get("box"), face.get("keypoints5")
    return (isinstance(box, dict) and all(_number(box.get(k)) for k in _BOX) and _number(face.get("score"))
            and isinstance(points, list) and len(points) == 5
            and all(isinstance(p, list) and len(p) == 2 and all(_number(v) for v in p) for p in points))


def _crop(face: dict) -> bytes | None:
    crop = face.get("crop")
    if not isinstance(crop, dict) or not isinstance(crop.get("jpeg_base64"), str):
        return None
    try:
        return base64.b64decode(crop["jpeg_base64"], validate=True)
    except (binascii.Error, ValueError):
        return None


async def detect_event_faces(event: PersonEvent, frigate: FrigateClient, inference: InferenceClient,
                             settings: Settings, recognize: bool = False) -> EventFaces:
    """`recognize`: also get an embedding per face (/v1/embed). Without it only /v1/detect is called.

    If the embedder is unavailable the event is still analysed for faces (recognition_state
    "unavailable").
    """
    from . import recognition  # recognition imports gallery, which imports this module

    state = recognition.STATE_OFF
    try:
        jpeg, source, frame_time, note = await _frame(event, frigate, settings)
    except FrigateNotFound:
        # Recognition was on; there was simply no face to match.
        return EventFaces(event, FRAME_UNAVAILABLE, detail="Frigate has no snapshot for this event",
                          recognition_state=recognition.STATE_DONE if recognize else state)

    resp = None
    if recognize:
        resp = await inference.embed(jpeg, settings.face_min_score, settings.face_max_faces, crops=True)
        if recognition.is_embedder_missing(resp):
            resp, state = None, recognition.STATE_UNAVAILABLE
        else:
            state = recognition.STATE_DONE
    if resp is None:
        resp = await inference.detect(jpeg, settings.face_min_score, settings.face_max_faces)
    try:
        result = resp.json()
    except ValueError:
        result = {}
    if resp.status_code != 200:
        code = result.get("error") if isinstance(result, dict) else None
        if code in _IMAGE_ERRORS:
            return EventFaces(event, FRAME_REJECTED, source, frame_time, jpeg, detail=f"detector refused the frame: {code}",
                              recognition_state=state)
        if resp.status_code == 500 and code == "inference_failed":
            return EventFaces(event, INFERENCE_FAILED, source, frame_time, jpeg, detail="the detector failed on this frame",
                              recognition_state=state)
        # model_unavailable (503) and anything unexpected: the service, not the frame; try again later.
        raise InferenceUnavailable(f"Face detection unavailable: HTTP {resp.status_code} {code or ''}".strip())

    if not isinstance(result, dict) or not isinstance(result.get("faces"), list):
        raise InferenceUnavailable("Face detection returned an unexpected response")
    done = state == recognition.STATE_DONE
    faces = [
        DetectedFace(box={k: int(f["box"][k]) for k in _BOX}, score=float(f["score"]), keypoints5=f["keypoints5"],
                     quality=f.get("quality") if isinstance(f.get("quality"), dict) else {}, crop_jpeg=_crop(f),
                     embedding=recognition.decode_embedding(f.get("embedding")) if done else None)
        for f in result["faces"] if _valid_face(f)
    ]
    image = result.get("image") or {}
    log.info("Event %s (%s): %d face(s) in the %s frame", event.id, event.camera, len(faces), source)
    return EventFaces(event, FACES_DETECTED if faces else NO_FACES_DETECTED, source, frame_time, jpeg,
                      image.get("width"), image.get("height"), faces, result.get("model"), note,
                      recognition_state=state,
                      embedder=recognition.embedder_id(result.get("embedder")) if done else None)
