"""AI analysis workspace: face detection and cropping on uploaded images.

The uploaded image is passed to the inference service and analysed in
memory. Neither the image nor the face crops are stored on the server.
"""

import logging

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse

from ..deps import AppState, current_user, get_state
from ..inference_client import InferenceUnavailable

router = APIRouter(prefix="/api", tags=["analysis"], dependencies=[Depends(current_user)])
log = logging.getLogger(__name__)

ACCEPTED_TYPES = {"image/jpeg", "image/png", "image/webp"}
# Error codes the inference service may return and that are safe to pass on.
PASSTHROUGH_ERRORS = {"invalid_image", "unsupported_format", "too_large", "model_unavailable", "inference_failed"}


def error(status: int, code: str, detail: str, **extra) -> JSONResponse:
    return JSONResponse({"error": code, "detail": detail, **extra}, status_code=status)


def _sniff(head: bytes) -> bool:
    return head[:3] == b"\xff\xd8\xff" or head[:8] == b"\x89PNG\r\n\x1a\n" or (head[:4] == b"RIFF" and head[8:12] == b"WEBP")


@router.post("/analysis/faces")
async def analyse_faces(
    request: Request,
    min_score: float | None = Query(default=None, ge=0.0, le=1.0),
    max_faces: int | None = Query(default=None, ge=1, le=100),
    state: AppState = Depends(get_state),
):
    """Detect faces in an uploaded image (raw body, Content-Type image/jpeg|png|webp)."""
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type not in ACCEPTED_TYPES:
        return error(415, "unsupported_format", "Upload a JPEG, PNG or WebP image")

    limit = state.settings.analysis_max_image_bytes
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > limit:
            return error(413, "too_large", f"Image is larger than {limit // (1024 * 1024)} MB")
    if not body:
        return error(400, "invalid_image", "Empty upload")
    if not _sniff(bytes(body[:12])):
        # The declared type is not trusted; the bytes must agree.
        return error(415, "unsupported_format", "The file content is not a JPEG, PNG or WebP image")

    async with state.analysis_slots:
        try:
            resp = await state.inference.detect(bytes(body), min_score, max_faces)
        except InferenceUnavailable as exc:
            return error(503, "inference_unavailable", str(exc))

    try:
        data = resp.json()
    except ValueError:
        return error(502, "inference_failed", "The inference service returned an invalid response")
    if resp.status_code != 200:
        code = data.get("error") if isinstance(data, dict) else None
        if code in PASSTHROUGH_ERRORS:
            extra = {k: data[k] for k in ("model", "model_status") if k in data}
            return error(resp.status_code, code, str(data.get("detail", ""))[:300], **extra)
        return error(502, "inference_failed", f"The inference service returned HTTP {resp.status_code}")

    faces = []
    for face in data.get("faces", []):
        crop = face.get("crop") or {}
        faces.append({
            "index": face["index"],
            "box": face["box"],
            "score": face["score"],
            "keypoints5": face["keypoints5"],
            "crop_box": crop.get("box"),
            # Data URL: shown directly by the browser, never written to disk.
            "crop_data_url": f"data:image/jpeg;base64,{crop['jpeg_base64']}" if crop.get("jpeg_base64") else None,
        })
    log.info("Face analysis: %d face(s), %s ms", len(faces), data.get("processing_ms"))
    return {
        "source": "inference",
        "image": data["image"],
        "faces": faces,
        "face_count": len(faces),
        "message": None if faces else "No face detected",
        "parameters": data.get("parameters"),
        "model": data["model"],
        "processing_ms": data.get("processing_ms"),
        "stored": False,
    }


@router.get("/models")
async def list_models(state: AppState = Depends(get_state)) -> dict:
    """Models known to the inference service and whether each one is usable."""
    try:
        models = await state.inference.models()
    except InferenceUnavailable as exc:
        return {"inference": {"reachable": False, "detail": str(exc)}, "models": []}
    keep = ("name", "task", "version", "status", "detail", "license", "license_url", "source", "notes", "runtime")
    return {"inference": {"reachable": True}, "models": [{k: m.get(k) for k in keep} for m in models]}
