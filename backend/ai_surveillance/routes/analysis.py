"""AI analysis workspace: face detection and cropping on uploaded images.

The uploaded image is passed to the inference service and analysed in
memory. Neither the image nor the face crops are stored on the server; the
crops are returned inside this authenticated response only.
"""

import logging

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from starlette.datastructures import UploadFile  # what Starlette's form parser returns
from starlette.formparsers import MultiPartParser

from ..deps import AppState, current_user, get_state
from ..inference_client import InferenceUnavailable

router = APIRouter(prefix="/api", tags=["analysis"], dependencies=[Depends(current_user)])
log = logging.getLogger(__name__)

# Error codes the inference service may return and that are safe to pass on.
PASSTHROUGH_ERRORS = {"invalid_image", "unsupported_format", "too_large", "model_unavailable", "inference_failed"}
MULTIPART_OVERHEAD = 64 * 1024  # boundaries and part headers on top of the file itself
FILE_FIELD = "file"


def error(status: int, code: str, detail: str, **extra) -> JSONResponse:
    return JSONResponse({"error": code, "detail": detail, **extra}, status_code=status)


def _sniff(head: bytes) -> bool:
    return head[:3] == b"\xff\xd8\xff" or head[:8] == b"\x89PNG\r\n\x1a\n" or (head[:4] == b"RIFF" and head[8:12] == b"WEBP")


async def _read_upload(request: Request, max_bytes: int) -> bytes | JSONResponse:
    """The bytes of the single `file` part, with the size limit enforced while reading."""
    if not request.headers.get("content-type", "").lower().startswith("multipart/form-data"):
        return error(415, "unsupported_format", f"Send the image as multipart/form-data in a field named '{FILE_FIELD}'")
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > max_bytes + MULTIPART_OVERHEAD:
        return error(413, "too_large", f"Image is larger than {max_bytes // (1024 * 1024)} MB")

    # Buffer the raw body with a hard cap, then parse it from memory.
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > max_bytes + MULTIPART_OVERHEAD:
            return error(413, "too_large", f"Image is larger than {max_bytes // (1024 * 1024)} MB")

    async def buffered():
        yield bytes(body)

    parser = MultiPartParser(request.headers, buffered(), max_files=1, max_fields=4)
    # Starlette spools file parts above 1 MB to a temporary file on disk. The
    # body is already capped, so a threshold above the cap keeps every upload
    # in memory: nothing is written to disk.
    parser.spool_max_size = max_bytes + MULTIPART_OVERHEAD
    try:
        form = await parser.parse()
    except Exception:  # malformed multipart
        return error(400, "invalid_image", "The upload could not be read as multipart form data")
    try:
        upload = form.get(FILE_FIELD)
        if not isinstance(upload, UploadFile):
            return error(400, "invalid_image", f"No file in the '{FILE_FIELD}' field")
        # The client's filename and declared type are ignored; only the bytes count.
        data = await upload.read()
    finally:
        await form.close()
    if not data:
        return error(400, "invalid_image", "Empty upload")
    if len(data) > max_bytes:
        return error(413, "too_large", f"Image is larger than {max_bytes // (1024 * 1024)} MB")
    if not _sniff(data[:12]):
        return error(415, "unsupported_format", "The file content is not a JPEG, PNG or WebP image")
    return data


@router.post("/analysis/faces")
async def analyse_faces(
    request: Request,
    min_score: float | None = Query(default=None, ge=0.0, le=1.0),
    max_faces: int | None = Query(default=None, ge=1, le=100),
    state: AppState = Depends(get_state),
):
    """Detect faces in an uploaded image (multipart field `file`: JPEG, PNG or WebP)."""
    data = await _read_upload(request, state.settings.analysis_max_image_bytes)
    if isinstance(data, JSONResponse):
        return data

    async with state.analysis_slots:
        try:
            resp = await state.inference.detect(data, min_score, max_faces)
        except InferenceUnavailable as exc:
            return error(503, "inference_unavailable", str(exc))

    try:
        result = resp.json()
    except ValueError:
        return error(502, "inference_failed", "The inference service returned an invalid response")
    if resp.status_code != 200:
        code = result.get("error") if isinstance(result, dict) else None
        if code in PASSTHROUGH_ERRORS:
            extra = {k: result[k] for k in ("model", "model_status") if k in result}
            return error(resp.status_code, code, str(result.get("detail", ""))[:300], **extra)
        return error(502, "inference_failed", f"The inference service returned HTTP {resp.status_code}")

    faces = []
    for face in result.get("faces", []):
        crop = face.get("crop") or None
        faces.append({
            "index": face["index"],
            "box": face["box"],
            "score": face["score"],
            "keypoints5": face["keypoints5"],
            "quality": face.get("quality"),
            "crop": None if crop is None else {
                "box": crop["box"],
                "width": crop["width"],
                "height": crop["height"],
                # Data URL inside this authenticated response; never written to disk.
                "data_url": f"data:image/jpeg;base64,{crop['jpeg_base64']}",
            },
        })
    log.info("Face analysis: %d face(s), %s ms", len(faces), result.get("processing_ms"))
    return {
        "status": "faces_detected" if faces else "no_faces_detected",
        "source": "inference",
        "image": result["image"],
        "faces": faces,
        "face_count": len(faces),
        "parameters": result.get("parameters"),
        "model": result["model"],
        "processing_ms": result.get("processing_ms"),
        "stored": False,
    }


@router.get("/models")
async def list_models(state: AppState = Depends(get_state)) -> dict:
    """Models known to the inference service and whether each one is usable."""
    try:
        health = await state.inference.health()
        models = await state.inference.models()
    except InferenceUnavailable as exc:
        return {"inference": {"reachable": False, "ready": False, "detail": str(exc)}, "models": []}
    keep = ("name", "task", "version", "status", "detail", "license", "license_url", "source", "sha256", "notes",
            "runtime")
    return {
        "inference": {"reachable": True, "ready": bool(health.get("ready"))},
        "models": [{k: m.get(k) for k in keep} for m in models],
    }
