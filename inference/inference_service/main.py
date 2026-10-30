"""Internal HTTP API of the inference service.

Only the api container calls it, over the private Docker network. It keeps
nothing: images are analysed in memory and discarded.

    GET  /health          liveness plus whether detection can actually run
    GET  /v1/health       same (kept for the api's health check)
    GET  /v1/models       registry: name, version, source, checksum, licence, status
    POST /v1/detect       body: raw image bytes (JPEG/PNG/WebP)
    POST /v1/embed        same, plus an SFace embedding per face (for the api's matcher)

With INFERENCE_API_TOKEN set, /v1/models, /v1/detect and /v1/embed require
"Authorization: Bearer <token>", so other containers on the network
cannot use the detector.
"""

import base64
import hmac
import logging
import time
from contextlib import asynccontextmanager

import cv2
from fastapi import FastAPI, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from . import __version__
from .adapters.base import FaceDetector, FaceEmbedder
from .config import Settings, get_settings
from .cropping import make_crop, quality
from .imaging import ImageError, TooLarge, decode
from .registry import LOAD_ERROR, MODELS, READY, SFACE, YUNET, ModelSpec, verify_file

log = logging.getLogger(__name__)


def _error(status: int, code: str, detail: str, **extra) -> JSONResponse:
    return JSONResponse({"error": code, "detail": detail, **extra}, status_code=status)


def _token(settings: Settings) -> str | None:
    value = settings.api_token.get_secret_value() if settings.api_token else ""
    return value or None


def create_app(settings: Settings | None = None, detector: FaceDetector | None = None,
               embedder: FaceEmbedder | None = None) -> FastAPI:
    settings = settings or get_settings()

    def authorized(request: Request) -> bool:
        expected = _token(settings)
        if expected is None:
            return True
        scheme, _, given = request.headers.get("authorization", "").partition(" ")
        return scheme.lower() == "bearer" and hmac.compare_digest(given.encode(), expected.encode())

    def unauthorized() -> JSONResponse:
        return _error(401, "unauthorized", "Missing or invalid service token")

    def load(spec: ModelSpec, factory):
        """(model or None, status, detail) for a model file; a bad file is reported, never loaded."""
        status, detail = verify_file(spec, settings.models_dir)
        model = None
        if status == READY:
            try:
                model = factory(settings.models_dir / spec.file_name)
            except Exception as exc:  # noqa: BLE001 - reported via /v1/models
                status, detail = LOAD_ERROR, f"{type(exc).__name__}: model could not be loaded"
                log.exception("Loading %s failed", spec.name)
        # The path goes to the server log only, never into API responses.
        log.info("Model %s: %s%s (models directory: %s)", spec.name, status,
                 f" ({detail})" if detail else "", settings.models_dir)
        return model, status, detail

    def yunet(path):
        from .adapters.yunet import YuNetDetector

        return YuNetDetector(path, settings.detection_max_side)

    def sface(path):
        from .adapters.sface import SFaceEmbedder

        return SFaceEmbedder(path)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.status = {}
        app.state.detector = detector
        app.state.embedder = embedder
        if detector is not None:  # injected (tests)
            app.state.status[YUNET.name] = (READY, None)
        else:
            app.state.detector, *status = load(YUNET, yunet)
            app.state.status[YUNET.name] = tuple(status)
        if embedder is not None:
            app.state.status[SFACE.name] = (READY, None)
        else:
            app.state.embedder, *status = load(SFACE, sface)
            app.state.status[SFACE.name] = tuple(status)
        if _token(settings) is None:
            log.warning("INFERENCE_API_TOKEN is not set: any container on the internal network can call /v1/detect")
        yield

    app = FastAPI(title="AI Surveillance inference", version=__version__, lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)

    def health_payload(request: Request) -> dict:
        statuses = {name: s for name, (s, _) in request.app.state.status.items()}
        return {
            "status": "ok",
            "version": __version__,
            # True only when a detector object is loaded, i.e. /v1/detect can run.
            "ready": request.app.state.detector is not None,
            # /v1/embed also needs the embedder; recognition is optional, so it is reported separately.
            "embed_ready": request.app.state.detector is not None and request.app.state.embedder is not None,
            "models": statuses,
        }

    app.get("/health")(health_payload)
    app.get("/v1/health")(health_payload)

    @app.get("/v1/models")
    def models(request: Request):
        if not authorized(request):
            return unauthorized()
        result = []
        for spec in MODELS.values():
            status, detail = request.app.state.status.get(spec.name, ("unknown", None))
            result.append({
                "name": spec.name, "task": spec.task, "version": spec.version, "status": status,
                "detail": detail, "license": spec.license, "license_url": spec.license_url,
                "source": spec.source, "sha256": spec.sha256, "notes": spec.notes,
                "runtime": f"opencv {cv2.__version__}",
            })
        return result

    def unavailable(request: Request, spec: ModelSpec, what: str) -> JSONResponse:
        status, detail = request.app.state.status.get(spec.name, ("unknown", None))
        return _error(503, "model_unavailable", detail or f"{what} is not loaded", model=spec.name, model_status=status)

    @app.post("/v1/detect")
    async def detect(
        request: Request,
        min_score: float | None = Query(default=None, ge=0.0, le=1.0),
        max_faces: int | None = Query(default=None, ge=1, le=500),
        crops: bool = Query(default=True),
    ):
        return await analyse(request, min_score, max_faces, crops, embed=False)

    @app.post("/v1/embed")
    async def embed(
        request: Request,
        min_score: float | None = Query(default=None, ge=0.0, le=1.0),
        max_faces: int | None = Query(default=None, ge=1, le=500),
        crops: bool = Query(default=False),
    ):
        """Same as /v1/detect, plus an SFace embedding per face (base64 of 128 little-endian float32 values)."""
        return await analyse(request, min_score, max_faces, crops, embed=True)

    async def analyse(request: Request, min_score: float | None, max_faces: int | None, crops: bool, embed: bool):
        if not authorized(request):
            return unauthorized()
        det: FaceDetector | None = request.app.state.detector
        if det is None:
            return unavailable(request, YUNET, "Face detector")
        emb: FaceEmbedder | None = request.app.state.embedder
        if embed and emb is None:
            return unavailable(request, SFACE, "Face embedder")

        # Read at most one chunk past the limit, so oversized bodies are not buffered.
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > settings.max_image_bytes:
                return _error(413, TooLarge.code, f"Image is larger than {settings.max_image_bytes // (1024 * 1024)} MB")

        started = time.perf_counter()
        try:
            image, mime = await run_in_threadpool(decode, bytes(body), settings.max_image_bytes,
                                                  settings.max_image_pixels)
        except ImageError as exc:
            return _error(exc.status, exc.code, str(exc))

        threshold = settings.detection_score_threshold if min_score is None else min_score
        limit = min(max_faces or settings.max_faces, settings.max_faces)
        try:
            faces = await run_in_threadpool(det.detect, image, threshold, limit)
        except Exception:  # noqa: BLE001
            log.exception("Detection failed")
            return _error(500, "inference_failed", "The face detector failed on this image")
        try:
            payload = []
            for index, face in enumerate(faces):
                item = {
                    "index": index,
                    "box": face.box.__dict__,
                    "score": face.score,
                    "keypoints5": [list(p) for p in face.keypoints5],
                    "quality": quality(image, face, settings.small_face_px),
                    "crop": None,
                }
                if crops:
                    crop = make_crop(image, face, settings.crop_margin, settings.crop_jpeg_quality)
                    if crop is not None:
                        item["crop"] = {"box": crop.box, "width": crop.width, "height": crop.height,
                                        "jpeg_base64": crop.jpeg_base64}
                payload.append(item)
        except Exception:  # noqa: BLE001
            log.exception("Detection failed")
            return _error(500, "inference_failed", "The face detector failed on this image")
        if embed:
            try:
                for item, face in zip(payload, faces, strict=True):
                    vector = await run_in_threadpool(emb.embed, image, face)
                    item["embedding"] = base64.b64encode(vector.astype("<f4").tobytes()).decode()
            except Exception:  # noqa: BLE001
                log.exception("Embedding failed")
                return _error(500, "inference_failed", "The face embedder failed on this image")

        height, width = image.shape[:2]
        extra = {"embedder": {"name": emb.name, "version": emb.version, "dimensions": emb.dimensions}} if embed else {}
        return {
            **extra,
            "status": "faces_detected" if payload else "no_faces_detected",
            "image": {"width": width, "height": height, "format": mime},
            "faces": payload,
            "face_count": len(payload),
            "parameters": {"min_score": threshold, "max_faces": limit, "crop_margin": settings.crop_margin,
                           "small_face_px": settings.small_face_px},
            "model": {"name": det.name, "version": det.version},
            "processing_ms": round((time.perf_counter() - started) * 1000, 1),
        }

    return app


def app() -> FastAPI:
    """Entry point for `uvicorn --factory inference_service.main:app`."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return create_app()
