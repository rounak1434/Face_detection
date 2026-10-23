# AI analysis: face detection and cropping (Phase 1)

The **AI analysis** page detects faces in an uploaded image with a real, loaded model. It draws boxes and the model's five alignment keypoints over the image, and lists a crop and metadata for each face.

**Camera integration is unchanged and deferred.** Frigate, the RTSP configuration, the live view, snapshots, clips and the test stream are not affected by this phase.

## Scope: what it does and does not do
| Does | Does not |
|---|---|
| Find faces: boxes in original-image pixels and a detection score | Identify, recognise or verify anyone (Phase 2) |
| Return 5 **alignment keypoints** per face (eyes, nose tip, mouth corners) | Return 68/70/478-point **facial landmarks**, a separate later feature (Phase 3) |
| Return a JPEG crop of each face (20% margin, clipped to the image), with its size | Estimate age, gender or expression (Phase 3) |
| Give simple quality hints: face size, "small face", "at image edge", a sharpness heuristic | Check liveness or detect spoofing |
| Report `no_faces_detected` as a normal result | Store uploads or crops |

The **detection score** is the model's confidence that a box contains a face. It says nothing about who the person is.

## Model
| | |
|---|---|
| Model | **YuNet** `face_detection_yunet_2023mar.onnx`, from [OpenCV Zoo](https://github.com/opencv/opencv_zoo/tree/main/models/face_detection_yunet) |
| Licence | **MIT** ("All files in this directory are licensed under MIT License", OpenCV Zoo; checked 2026-10-23) |
| Checksum | SHA-256 `8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4`, 232,589 bytes. Pinned from the Git LFS pointer in `inference/inference_service/registry.py`. |
| Runtime | OpenCV `FaceDetectorYN` (`opencv-python-headless`), CPU. No GPU packages. |
| Location | Baked into the inference image at `/models` (`INFERENCE_MODELS_DIR`) |
| Download | **Once, at image build time** (`python -m inference_service.download`). It never happens on requests or at startup. A size or checksum mismatch deletes the file and **fails the build**. After that the service works offline. |

**Why OpenCV `FaceDetectorYN` rather than ONNX Runtime.** YuNet's raw output needs anchor decoding and non-maximum suppression. `FaceDetectorYN` is OpenCV's maintained implementation of exactly that, and OpenCV Zoo's own demo uses it. Re-implementing it on ONNX Runtime would add code and risk with no benefit for this model. The adapter interface (`inference_service/adapters/base.py`) allows an ONNX Runtime adapter for later models without changing the API or UI.

**Model states**, from `GET /v1/models` and the Analysis page:
| Status | Meaning |
|---|---|
| `ready` | File verified and detector loaded; inference can run |
| `missing` | No model file in the models directory |
| `checksum_mismatch` | A file is present but its size or SHA-256 differs; it is **not** loaded |
| `load_error` | Verified, but OpenCV failed to load it |

`/health` reports `"ready": true` only when a detector object is actually loaded. The inference container's Docker health check uses this.

## Architecture and request flow
```
browser ── multipart POST /api/analysis/faces ──► web (Caddy, 12 MB body cap)
   └► api: session check (existing auth) → body read with a 10 MB cap → single "file" part
      → magic-byte check (JPEG/PNG/WebP; client name and type ignored) → max 2 concurrent
      └► inference (internal network only): POST /v1/detect with the raw bytes
         → decode with Pillow: header pixel limit (40 MP) before decoding, EXIF rotation
         → YuNet adapter (padding for tiny inputs, keypoint sanity check)
         → crops + quality metadata, in memory → JSON
      ◄─ api maps errors to stable codes and returns crops as data URLs inside this response
```

| Service | Port | Exposure |
|---|---|---|
| `inference` | 8100 | Docker network only; **not published** on the host |
| `api` | 8000 | Docker network only; reached via `web` |
| `web` (Caddy) | 80/443 | Host / LAN, the only published entry point |

## API
`POST /api/analysis/faces`. It requires a session (the existing login) and takes `multipart/form-data` with one field, **`file`**. Optional query parameters: `min_score` (0–1, default 0.7) and `max_faces` (1–100).
```bash
curl -k -b cookies.txt -F "file=@photo.jpg" https://localhost/api/analysis/faces
```

**Success** (values from a real run on the public-domain test fixture):
```json
{
  "status": "faces_detected",
  "source": "inference",
  "image": {"width": 480, "height": 600, "format": "image/jpeg"},
  "faces": [{
    "index": 0,
    "box": {"x": 257, "y": 115, "w": 76, "h": 102},
    "score": 0.9416,
    "keypoints5": [[283.6, 158.4], [316.2, 154.9], [300.9, 175.7], [289.7, 189.4], [317.5, 186.1]],
    "quality": {"face_px": 76, "small_face": false, "touches_edge": false, "sharpness": 346.9},
    "crop": {"box": {"x": 242, "y": 95, "w": 106, "h": 142}, "width": 106, "height": 142,
             "data_url": "data:image/jpeg;base64,…"}
  }],
  "face_count": 1,
  "parameters": {"min_score": 0.7, "max_faces": 50, "crop_margin": 0.2, "small_face_px": 40},
  "model": {"name": "yunet", "version": "2023mar"},
  "processing_ms": 27.0,
  "stored": false
}
```
- `index` is stable within one response, with faces ordered by score.
- Keypoint order follows OpenCV's `FaceDetectorYN`: right eye, left eye, nose tip, right mouth corner, left mouth corner.
- `crop` is `null` if a crop could not be produced (for example an empty box).
- `quality.sharpness` is the variance of the Laplacian at a fixed scale: a heuristic, not a quality model.
- The keypoint values above are illustrative; the box, score, crop and quality values are real.

**No faces:** `200` with `"status": "no_faces_detected"` and `"faces": []`. It never comes with an `error` field, so the UI can always tell it apart from failures.

**Errors** are returned as `{"error": "<code>", "detail": "…"}`. A failure never carries `faces`.
| Status | `error` | When |
|---|---|---|
| 400 | `invalid_image` | Empty upload, missing `file` field, malformed multipart, or a corrupt or truncated image |
| 401 | — | Not signed in |
| 413 | `too_large` | Over 10 MB (checked from Content-Length and while reading), or over 40 megapixels (from the header, before decoding). Covers decompression bombs. |
| 415 | `unsupported_format` | Not multipart, or the bytes are not JPEG/PNG/WebP (e.g. GIF, TIFF, PDF, HEIC) |
| 500 | `inference_failed` | The detector failed on this image (no internals returned) |
| 502 | `inference_failed` | Unexpected response from the inference service |
| 503 | `model_unavailable` | Model missing, failed verification or failed to load (`model_status` says which) |
| 503 | `inference_unavailable` | Inference service unreachable, or no answer within 30 s |

**Related endpoints:**
- `GET /api/models` returns `{"inference": {"reachable", "ready"}, "models": [{name, task, version, status, detail, license, license_url, source, sha256, notes, runtime}]}`.
- `GET /api/health` includes `inference.reachable` and `inference.models_ready`.

Internal inference endpoints (Docker network only): `GET /health`, `GET /v1/health`, `GET /v1/models`, `POST /v1/detect` (raw image bytes).

## Upload and crop storage
- **Nothing is stored.** The uploaded image and crops exist only in memory during the request. They are not written to disk, the database or logs (`"stored": false`).
- Crops are sent back as JPEG data URLs **inside the authenticated response**, so there are no crop files, URLs or paths that could be fetched later or by anyone else. No retention policy is needed because nothing is retained.
- Client file names are ignored; they never touch the filesystem.
- The browser shows the original image from its own local copy (`blob:` URL) and positions the boxes in percentages, so they stay aligned at any display size.

Persisted crops (with server-generated IDs, an authenticated route and retention) are deferred until a feature needs them, such as enrollment in Phase 2.

## Robustness notes (found while testing)
- **YuNet on very short images.** On inputs under 32 px in one dimension (e.g. 20×2000), the unpadded detector intermittently returned garbage faces with score 1.0 and keypoints off the image: 16 in 300 calls. The adapter now pads the input (bottom/right, to a multiple of 32, at least 64 px) and drops detections whose keypoints fall outside their own box. Afterwards: 0 in 300. A regression test repeats the case.
- **Oversized images.** Pillow's own decompression-bomb guard can fire before our pixel check; both map to `413 too_large`.

## Tests
| Suite | What | Needs |
|---|---|---|
| `inference/tests/test_imaging.py` | Formats, EXIF rotation, alpha, corrupt and truncated files, byte and pixel limits, magic bytes over declared type | nothing |
| `inference/tests/test_cropping.py` | Crop margins, clipping at every border, empty crops rejected, crop pixels, quality flags, sharpness ordering | nothing |
| `inference/tests/test_api.py` | `/v1/detect` contract with a fake detector: crops, parameters, every error code, missing / corrupted / unloadable model, no path leaks, crash containment, `/health` readiness | nothing |
| `inference/tests/test_model_yunet.py` | **Real YuNet** on a public-domain portrait: one face; 2 and 3 faces with correct positions; `max_faces`; no faces in synthetic images; degenerate sizes; 3× upscaled image mapped back; small (~24 px) face detected and flagged; face 4 px from the corner with the crop clipped to 0; face cut by the edge flagged; the HTTP service end to end | verified model file |
| `backend/tests/unit/test_analysis.py` | Multipart upload, auth required, PNG/WebP, client filename and type ignored, every validation path, size limit, error mapping, unreachable/timeout/not-ready inference, no paths or secrets in responses | nothing (mocked inference) |
| `backend/tests/unit/test_migrations.py` | Fresh DB; legacy DB with data verified, backed up and stamped; differing legacy DBs refused and untouched; status read-only; CLI; model drift; downgrade; WAL | nothing |
| `frontend/src/test/analysis-page.test.tsx` | Multipart workflow, loading state, boxes, keypoints, crops and metadata, no-face state, clear, client-side validation, server errors, network error, model unavailable, inference unreachable, routing | nothing |
| `backend/tests/integration/test_live_stack.py` | Model ready; no-face PNG; non-image rejected; login required; real face from the public-domain fixture (mounted by `run-integration-tests.sh`) and optional `IT_FACE_IMAGE` | running stack |

**Test fixture:** one public-domain NASA portrait, downscaled. Its provenance and terms are in `inference/tests/data/public_domain/README.md`. No other photos of people are in the repository.

```bash
cd inference && python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m inference_service.download --models-dir models
.venv/bin/pytest -v
```
On Windows, use `.venv\Scripts\` in place of `.venv/bin/`.

## Not measured yet
- Detection accuracy on CP Plus footage: day, night/infrared, distance and angle.
- Speed on the Ubuntu machine. The 27 ms above is one 480×600 image on the Windows development PC.

No claims are made until the evaluation protocol in the roadmap has been run.
