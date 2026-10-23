# AI analysis: face detection and cropping

Phase 1 of the AI roadmap. The **AI analysis** page detects faces in an uploaded image using a real model, and shows boxes, five keypoints and face crops.

## Scope: what it does and does not do
| Does | Does not |
|---|---|
| Find faces and their boxes, with a detection score | Identify, recognise or verify anyone (Phase 2) |
| Return 5 keypoints per face: eyes, nose tip, mouth corners | Return 68/70-point landmarks (Phase 3) |
| Return a JPEG crop of each face, with a 20% margin | Estimate age, gender or expression (Phase 3) |
| Report "No face detected" as a normal result | Check liveness or detect spoofing (not planned before Phase 6) |

The **detection score** is the model's confidence that a box contains a face. It is not a statement about who the person is.

## Model
| | |
|---|---|
| Model | **YuNet** `face_detection_yunet_2023mar.onnx`, from [OpenCV Zoo](https://github.com/opencv/opencv_zoo/tree/main/models/face_detection_yunet) |
| Licence | MIT ("All files in this directory are licensed under MIT License", OpenCV Zoo) |
| Integrity | SHA-256 `8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4`, 232,589 bytes. Pinned in `inference/inference_service/registry.py` from the Git LFS pointer. |
| Runtime | OpenCV `FaceDetectorYN` (opencv-python-headless) on the CPU |
| Download | Once, when the inference image is built. A checksum mismatch fails the build. After that the service runs offline. |

## Architecture
```
browser ──HTTPS──► web (Caddy, 12 MB body cap) ──► api  POST /api/analysis/faces
                                                     │  login required, type and size checks,
                                                     │  at most 2 analyses at once
                                                     ▼
                                    inference (internal only)  POST /v1/detect
                                     decode (Pillow), pixel limit, EXIF rotation,
                                     YuNet adapter, crops; nothing stored
```
Models sit behind an adapter interface (`inference_service/adapters/base.py`), so a detector can be swapped without changing the API or UI.

## API
`POST /api/analysis/faces`. The body is the raw image file, with `Content-Type: image/jpeg | image/png | image/webp`. Optional query parameters: `min_score` (0–1, default 0.7) and `max_faces` (1–100).

**Success:**
```json
{
  "source": "inference",
  "image": {"width": 960, "height": 1200, "format": "image/jpeg"},
  "faces": [{
    "index": 0, "box": {"x": 513, "y": 231, "w": 153, "h": 199}, "score": 0.939,
    "keypoints5": [[546.7, 317.9], [614.6, 310.4], [581.8, 353.4], [562.1, 383.8], [618.9, 376.8]],
    "crop_box": {"x": 482, "y": 191, "w": 215, "h": 279},
    "crop_data_url": "data:image/jpeg;base64,…"
  }],
  "face_count": 1, "message": null,
  "parameters": {"min_score": 0.7, "max_faces": 50, "crop_margin": 0.2},
  "model": {"name": "yunet", "version": "2023mar"},
  "processing_ms": 53.5, "stored": false
}
```
- Keypoint order follows OpenCV's `FaceDetectorYN`: right eye, left eye, nose tip, right mouth corner, left mouth corner.
- **No face** gives `200` with `"faces": []` and `"message": "No face detected"`.

**Errors** are returned as `{"error": "<code>", "detail": "…"}`:
| Status | `error` | When |
|---|---|---|
| 400 | `invalid_image` | Empty upload, corrupt or truncated file |
| 401 | — | Not signed in |
| 413 | `too_large` | Over 10 MB, or over 40 megapixels (checked from the header, before decoding) |
| 415 | `unsupported_format` | Not JPEG/PNG/WebP. The file's bytes are checked; the declared type is not trusted. |
| 500 | `inference_failed` | The detector failed on this image. No internal details are returned. |
| 502 | `inference_failed` | The inference service gave an unexpected response |
| 503 | `model_unavailable` | Model file missing, failed verification, or failed to load. `model_status` says which. |
| 503 | `inference_unavailable` | Inference service unreachable or timed out |

`GET /api/models` lists each model's status (`ready`, `missing`, `checksum_mismatch`, `load_error`), licence, source and runtime. `GET /api/health` includes `inference.reachable` and `inference.models_ready`.

## Privacy
- Uploaded images and face crops are processed **in memory only**. They are not written to disk or the database and not logged (`"stored": false`). Crops are sent back to the browser as data URLs.
- The browser shows the original image from its own local copy.
- Only signed-in users can analyse. Per-role limits arrive with roles in Phase 2.

## Robustness notes (found while testing)
- **YuNet on very short images.** On inputs under 32 px in one dimension (for example 20×2000), the unpadded detector intermittently returned garbage faces with score 1.0 and keypoints off the image. This happened in about 5% of calls. The adapter now pads the input to a multiple of 32 (at least 64 px) and drops detections whose keypoints fall outside their own box. A regression test repeats the case.
- **Oversized images.** Pillow's own decompression-bomb guard can fire before our pixel check; both now map to `413 too_large`.

## Tests
| Suite | What | Needs |
|---|---|---|
| `inference/tests/test_imaging.py` | Formats, EXIF rotation, alpha, corrupt and truncated files, byte and pixel limits, magic-byte checks | nothing |
| `inference/tests/test_api.py` | `/v1/detect` contract with a fake detector: crops, margins, parameters, every error code, missing or corrupted model, contained crashes | nothing |
| `inference/tests/test_model_yunet.py` | **Real YuNet:** no faces in synthetic images, degenerate sizes (regression). Plus a real-face check when `FACE_FIXTURE=/path/photo.jpg` is set. | the verified model file |
| `backend/tests/unit/test_analysis.py` | API upload checks, error mapping, unreachable or timed-out inference, `/api/models`, health | nothing (mocked inference) |
| `backend/tests/integration/test_live_stack.py` | Model ready, no-face PNG, non-image rejected. Real face when `IT_FACE_IMAGE` is set. | the running stack |

No photographs of people are stored in the repository. Positive-face tests use a photo **you** provide and are allowed to use.

To run the inference tests locally:
```bash
cd inference && python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m inference_service.download --models-dir models
.venv/bin/pytest
```
On Windows, use `.venv\Scripts\` in place of `.venv/bin/`.

## Not measured yet
- Detection accuracy on CP Plus footage: day, night/infrared, distance, angle.
- Speed on your Ubuntu machine. The 53.5 ms above is one image on this Windows development PC.

Both need the evaluation protocol in the roadmap before any claim is made.
