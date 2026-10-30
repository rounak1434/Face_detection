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
- `GET /api/recognition`: whether it is on, who changed it and when, whether the embedder is ready, how many active people and reference faces have an embedding, how many are still pending, and the thresholds.
- `PUT /api/recognition` with `{"enabled": true|false, "password": "…"}`: needs the signed-in user's password again (wrong: 403, audited, counts towards the sign-in lockout). Switching on:
  1. is refused (409 `embedder_unavailable`) while SFace is not loaded in the inference service;
  2. computes embeddings for reference faces that have none yet (for example those enrolled before SFace was installed);
  3. is refused (409 `gallery_empty`) if no active person then has a usable embedding.
- Every change is written to the audit log with the thresholds in force.
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

## Not known yet
- Accuracy at the real door: face size, angle, light, IR at night, motion blur.
- False accepts between look-alike people in your gallery; how many reference images per person help.
- Speed on the Ubuntu host (one extra model pass per face).
