# Face recognition

Recognition compares faces in Frigate person events with the reference faces of the **authorized people** in the gallery ([GALLERY.md](GALLERY.md)). It is **off by default**. Design and phase plan: [ADR-0004](adr/0004-face-events-and-recognition.md).

Status: implemented (Phase 6). Tested with mocked services and, end to end, with the real YuNet and SFace models on public-domain NASA portraits. **Not yet run on the real camera; the thresholds are starting points, not measured values.** The switch is on the **People** page (Phase 7): it shows on/off, whether the embedder is ready, how many people and reference images can be matched, pending and unreadable embeddings, the thresholds and who changed it last; switching asks for the password and explains a refusal.

## What a result means
A model turns each face into 128 numbers (an *embedding*, SFace from OpenCV Zoo, Apache-2.0). Two embeddings are compared by **cosine similarity**: about 1.0 for the same photo, lower the less alike the faces look. A similarity is not proof of identity. Look-alikes, twins, photos of photos, masks, poor light and small faces can all mislead it, and **there is no liveness check** (a printed photo of an enrolled person can match).

| Outcome | When |
|---|---|
| `AUTHORIZED` | The best similarity is at least `RECOGNITION_ACCEPT_THRESHOLD` (0.45), and no second person is also above it. The matched person is linked. |
| `UNRECOGNIZED` | The best similarity is at most `RECOGNITION_REJECT_THRESHOLD` (0.30), or nobody is enrolled. |
| `INCONCLUSIVE` | In between; or two people are above the accept threshold; or the face is smaller than `RECOGNITION_MIN_FACE_PX` (40). |
| `SYSTEM_ERROR` | The face had no usable embedding. |
| `NOT_MATCHED` | Recognition was off, or the embedder was unavailable, when the event was analysed. |

Each result also has a `reason`, so the cases inside one outcome are told apart:

| Outcome | `reason` |
|---|---|
| `AUTHORIZED` | `match` |
| `UNRECOGNIZED` | `below_reject_threshold`, or `gallery_empty` |
| `INCONCLUSIVE` | `between_thresholds`, `several_people_match`, or `insufficient_quality` (face below `RECOGNITION_MIN_FACE_PX`, not compared) |
| `SYSTEM_ERROR` | `recognition_error` |
| `NOT_MATCHED` | `recognition_off`, `recognition_unavailable`, or `not_performed` |

The reason is derived from what is stored (outcome, similarity, the accept threshold used, the event's recognition state), so events stored before it existed have one too.

For each person the best of their reference faces counts. Inactive people are never matched. Events analysed before the switch was turned on are not matched afterwards.

## Thresholds: where the defaults come from
| Evidence | Similarity |
|---|---|
| OpenCV's published SFace threshold (from its sample, LFW) | 0.363 |
| The same photo re-encoded, scaled to 70 %, brightened, mirrored | 0.94 – 1.0 |
| Neil Armstrong, two NASA portraits five years apart (1964, 1969) | **0.617** |
| Different people (Armstrong, Aldrin, Collins; 6 pairs) | **0.118 – 0.194** |

Four photos of three people is a sanity check, not an accuracy measurement. Accept 0.45 and reject 0.30 put OpenCV's 0.363 inside the "inconclusive" band, so borderline cases are not decided automatically. Before relying on the results, measure on the real door camera with consenting people (ADR-0004, Phase 10) and set the thresholds from that.

## The switch
- `GET /api/recognition`: whether it is on, who changed it and when, whether the embedder is ready, how many active people and reference faces have an embedding, how many are still pending, and the thresholds. While the embedder is unavailable those three counts are `null` (`counts_known: false`): which embeddings are usable depends on the model the embedder reports, so the page shows only the total number of stored reference images and says the rest is unknown. Nothing in the gallery changes.
- `PUT /api/recognition` with `{"enabled": true|false, "password": "…"}`: needs the signed-in user's password again (wrong: 403, audited, counts towards the sign-in lockout). Switching on:
  1. is refused (409 `embedder_unavailable`) while SFace is not loaded in the inference service;
  2. computes embeddings for reference faces that have none yet (for example those enrolled before SFace was installed);
  3. is refused (409 `gallery_empty`) if no active person then has a usable embedding.
- Every change is written to the audit log with the thresholds in force.
- **Default and restarts:** a fresh install has no setting stored, which means **off**. The value lives in the database (`app_settings.recognition_enabled`), so a restart, an update or a rebuild never changes it; only `PUT /api/recognition` does. A damaged stored value counts as off.
- **Only the server decides.** The browser can only ask through `PUT /api/recognition` (signed in, password again, a strict boolean); no other request can turn matching on.
- **Gallery while off:** people, reference images and embeddings stay exactly as they are, byte for byte; switching back on uses them again.
- The switch and the gallery are read before **every** event, so switching off, deactivating or deleting a person applies to the very next event analysed.
- **Off**: face events are analysed by the detector only (`/v1/detect`); no embedding is computed and faces are stored as `NOT_MATCHED`. **On**: each new event goes through `/v1/embed` and every face is matched. If SFace becomes unavailable while on, events are still analysed for faces and marked `recognition_state: unavailable`.

## Privacy
- Reference embeddings are stored **AES-256-GCM encrypted** with the credentials key, like the reference images, and never leave the server.
- **Event embeddings are not stored**: they are matched in memory and discarded; only the outcome, the similarity, the thresholds used and the matched person are kept, under the face-event retention (`FACE_EVENT_RETENTION_DAYS`, default 30).
- Deleted rows are overwritten in the database (`secure_delete`). Deleted files are unlinked, not overwritten, and Frigate keeps its own snapshots and recordings: see the known limits in [VERIFICATION.md](VERIFICATION.md) (Phase 8). Use full-disk encryption on the host.
- Deleting a person removes their images and embeddings and **unlinks past matches**: those faces stay `AUTHORIZED` with their similarity (it was true at the time), but no longer name anyone.

## Settings
In `deploy/.env` (passed to the api by Compose; `docker compose up -d` applies a change). The reject threshold must be below the accept threshold, or the api refuses to start.
| Variable | Default |
|---|---|
| `RECOGNITION_ACCEPT_THRESHOLD` | `0.45` |
| `RECOGNITION_REJECT_THRESHOLD` | `0.30` |
| `RECOGNITION_MIN_FACE_PX` | `40` |

## Inference service
- `POST /v1/embed`: like `/v1/detect`, plus `embedding` per face (base64 of 128 little-endian float32 values, L2-normalised) and `embedder` (`name`, `version`, `dimensions`). Crops are off by default. 503 `model_unavailable` with `model: "sface"` when only the embedder is missing.
- `/health` adds `embed_ready`; `/v1/models` lists `sface` (2021dec, 38,696,353 bytes, SHA-256 `0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79`, Apache-2.0). The model is downloaded and verified at image build (`python -m inference_service.download`), so the inference image grows by about 39 MB; at runtime the service stays offline.

## Model facts and measured speed
| | |
|---|---|
| Model | SFace 2021dec (`face_recognition_sface_2021dec.onnx`), OpenCV Zoo, Apache-2.0; pinned size 38,696,353 bytes and SHA-256 |
| Runtime | OpenCV `FaceRecognizerSF` (OpenCV 4.14, the `opencv-python-headless` wheel), **CPU only**: that wheel has no CUDA (0 CUDA devices reported). The GPU override (`compose.nvidia.yaml`) speeds up Frigate's detector, not these models |
| Input | The face aligned to 112×112 from YuNet's five keypoints (`alignCrop`) — the detector finds the face, SFace only describes it |
| Output | 128 values, L2-normalised by our adapter; compared by cosine similarity (a dot product) |

`python -m inference_service.benchmark` measures load time, latency and memory on the machine it runs on (CPU, 30 runs by default; images optional, the public-domain portraits by default). It measures speed only, not accuracy. On the development laptop (Intel i5-11320H, 4 cores / 8 threads, 2026-10-30; 50 runs per image after warm-up):

| | Windows 11, Python 3.12 | Linux (WSL 2, same laptop), Python 3.14 |
|---|---|---|
| Load YuNet / SFace | 6 ms / 63 ms | 2 ms / 64 ms |
| Detect, 480×600 portrait (median / p95) | 13.5 / 14.7 ms | 7.4 / 10.7 ms |
| Detect, 1920×1080 frame (scaled to 1280 px for detection) | 50.3 / 64.1 ms | 36.7 / 55.6 ms |
| Embed, per face (median / p95) | 9.5 / 12.1 ms | 9.2 / 13.3 ms |
| Process memory, both models loaded, after the 480×600 runs / after the 1080p runs | 159 MB / 195 MB (peak 219 MB) | 224 MB / 283 MB |

So one face event costs roughly 40–65 ms of CPU for a 1080p frame with one face, plus about 10 ms per extra face; the inference container's 1,536 MB memory limit leaves ample room. **These are laptop numbers**; measure the Ubuntu AI PC in Phase 10. The inference image does not contain the test portraits, so copy one in and pass it (run in `deploy/`; these two commands have not been tried yet, as there is no Docker on the development PC):
```bash
docker compose cp ../inference/tests/data/public_domain/nasa_s69-31741_480x600.jpg inference:/tmp/face.jpg
```
```bash
docker compose exec inference python -m inference_service.benchmark --models-dir /models /tmp/face.jpg
```

## Not known yet
- Accuracy at the real door: face size, angle, light, IR at night, motion blur.
- False accepts between look-alike people in your gallery; how many reference images per person help.
- Speed on the Ubuntu host (one extra model pass per face).
