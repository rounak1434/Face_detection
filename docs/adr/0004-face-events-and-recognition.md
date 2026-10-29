# ADR-0004: Face events, authorized-person gallery and recognition (audit and plan)

- Status: **accepted: option B** (our matcher), owner's decision 2026-10-29. Progress: Phase 1 (audit) done; Phase 2 (face detection on events) done, see section 8; Phase 3 (storage, deduplication, retention) done, see section 9. Decisions 3–5 in section 7 are still open.
- Date: 2026-10-29, base commit `e8e0755`
- Supersedes the open "face recognition: decided in milestone 2" point of [ADR-0001](0001-frigate-engine.md).

Every fact below names its source: a file in this repository, or a file read in the upstream source on 2026-10-29 (Frigate tag `v0.18.2`, OpenCV Zoo `main`). Nothing here has run on the real stack.

## 1. What exists today (reusable)
| Component | Evidence | Reuse for |
|---|---|---|
| **Inference service** (FastAPI, internal only, bearer token, 1 worker, `mem_limit` 1536 MB) | `inference/inference_service/main.py`, `deploy/compose.yaml` | Face detection on event snapshots; host the embedding model (no new service) |
| **YuNet** face detector, MIT, SHA-256 pinned, downloaded at build | `registry.py`, `adapters/yunet.py`; real-model tests in `inference/tests/test_model_yunet.py` | Boxes, score and the **5 keypoints SFace's alignment needs** |
| Model registry with size + SHA-256 verification, `ready/missing/checksum_mismatch/load_error` | `registry.py`, `download.py` | Add SFace the same way |
| OpenCV 4.14 in the inference image, which has `cv2.FaceRecognizerSF` (checked: `hasattr` is `True`) | `inference/constraints.txt` | Embeddings and matching without a new runtime |
| api → inference client, timeouts, error mapping, concurrency slots | `backend/ai_surveillance/inference_client.py`, `routes/analysis.py` | Same client for detect/embed |
| Frigate 0.18.2: `objects.track: [person]`, snapshots on (retain 14 days); applied device cameras inherit both | `deploy/frigate/config.yml`, `frigate_config._INHERITED` | **Event source**: person events with a snapshot |
| Frigate client (auth on 8971, 5000 internal) | `frigate_client.py` | Add `events()` and `event_snapshot()` |
| Media store: server-generated names, path checks, atomic writes, free-space guard, retention loop | `media.py`, `captures.py` | Face crops, snapshots, reference images |
| SQLite + Alembic (revisions `0001`, `0002`), backup before migrating | `migrations/`, `docs/DATABASE.md` | New tables via `0003`+ |
| Sign-in: single admin account, argon2id, `SameSite=Strict` session cookie | `security.py`, `routes/auth.py` | All new routes |
| AES-256-GCM cipher with per-record binding and clean error handling | `credentials.py` | Optional encryption of reference images and embeddings at rest |
| Frontend: React + router; Live, AI analysis, Setup pages; 47 vitest tests | `frontend/src/App.tsx`, `src/test/` | New Events and People pages |

## 2. Missing capabilities
1. No event ingestion: nothing reads Frigate's events; there is no MQTT broker in Compose.
2. No persistent face events, crops or event history (the AI analysis page stores nothing by design).
3. No gallery, no enrollment, no consent records.
4. No embedding model, no matching, no thresholds, no evaluation data.
5. No persistent application settings (all settings are environment variables), so no recognition switch.
6. No roles beyond the single admin.
7. No test images of **two different identities** or of **one identity twice**: the repository has exactly one public-domain portrait (`inference/tests/data/public_domain/`).

## 3. Engine decision needed: Frigate's built-in recognition (A) or our matcher (B)
Facts from the Frigate v0.18.2 source:
- `frigate/config/classification.py` `FaceRecognitionConfig`: `enabled` (default false), `model_size` small/large, `unknown_score` 0.8, `recognition_threshold` 0.9, `min_area` 750, `min_faces` 1, `save_attempts` 200.
- `frigate/data_processing/real_time/face.py`: a name is written to the event's `sub_label` only when the weighted score ≥ `recognition_threshold`; below `unknown_score` the face is "unknown". Per-attempt names and scores are published as tracked-object updates (MQTT); the HTTP event keeps only the final `sub_label`.
- Enrollment API: `POST /api/faces/{name}/create`, `/register`, `/faces/{name}/delete`, `GET /api/faces` (`frigate/api/classification.py`); images live in Frigate's media volume.

| | **A: Frigate built-in** | **B: our matcher (recommended)** |
|---|---|---|
| Pipeline | Frigate's own face detector + embedding inside Frigate | Frigate person event → snapshot → our YuNet (existing) → SFace embedding in the existing inference service |
| Scores for `INCONCLUSIVE` | Final `sub_label` only over HTTP; scores need MQTT (not deployed) | Every match has a cosine score; three-way decision is ours |
| Recognition OFF while detection continues | Frigate's face detection is part of its face recognition; turning it off stops face work in Frigate; switching needs a config change and restart (our Apply flow) | A database flag checked per event: detection and events continue, embedding/matching skipped, instantly, no restart |
| Gallery storage | Frigate's media volume, managed through Frigate's API | Our database and media store, our retention and deletion |
| Model and licence | Frigate downloads its face models when enabled (not inspected) | SFace, **Apache-2.0** (OpenCV Zoo `models/face_recognition_sface/README.md`), 38,696,353 bytes, SHA-256 `0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79` (Git LFS pointer, file unchanged since 2022-01-06) |
| GPU memory (GTX 1650, 4 GB) | Large model needs the GPU | CPU only; no GPU memory |
| Duplicate pipelines | None in our code, but our YuNet stays unused for cameras | None: YuNet is reused, no new service |

**Recommendation: B.** It is the only option that meets "recognition OFF keeps detection", gives honest `INCONCLUSIVE` without adding MQTT, and reuses the existing inference service. **Your decision is needed before Phase 2.**

Known SFace facts and limits: published LFW accuracy 0.9940 (OpenCV Zoo README, not our cameras); reference threshold cosine ≥ 0.363 (`sface.py`), which is a starting point only and must be calibrated on site; input is a 112×112 face aligned from YuNet's 5 keypoints (`FaceRecognizerSF.alignCrop`); no liveness or anti-spoofing; accuracy on infrared/night images unknown.

## 4. Frame source and its main risk
Frigate's event snapshot (`GET /api/events/{id}/snapshot.jpg`, `frigate/api/media.py`; available once the event has `has_snapshot`) is taken from the **detect stream**: 1280×720 for the test stream, typically 640×360 or 704×576 for an NVR sub stream. Faces at a door can be small there. Mitigations to evaluate in Phase 2, not assumed:
1. The snapshot at the event's best-frame time from the **recording** (main stream): `GET /api/{camera}/recordings/{timestamp}/snapshot.jpg` exists (`get_snapshot_from_recording` in `frigate/api/media.py`); exact parameters to be read before use.
2. A higher-resolution detect stream (more CPU in Frigate).
Faces smaller than a threshold (YuNet's `small_face` hint, 40 px) are stored as detections but never matched.

## 5. Proposed design (option B)
### Event flow
```
Frigate person event (ended, has_snapshot)
  └─ api ingestion loop, every FACE_POLL_SECONDS (default 10):
       GET /api/events?labels=person&has_snapshot=1&in_progress=0&after=<cursor - lookback>
       for each event not yet in face_events (unique frigate_event_id):
         GET snapshot → inference /v1/detect (YuNet) → store snapshot + crops
         if recognition ON and face large enough: inference /v1/embed → match against the gallery
         → face_event (+ face_detection rows) committed once
```
Deduplication: one row per Frigate event (unique constraint); the cursor lags by a lookback window because Frigate's `after` filters by start time, and the unique key makes reprocessing harmless. A restart resumes from the stored cursor.

### Inference API additions (same service)
- `POST /v1/embed`: image + the faces' boxes and keypoints from `/v1/detect` → 128-float SFace features per face. The api does the matching (gallery in its database); the inference service stays stateless.
- `GET /v1/models` lists `sface` with the same verification states. Recognition is unavailable (not "off") when SFace is not `ready`.

### Schemas (migration `0003_face_events_and_gallery`)
- `app_settings(key PK, value JSON, updated_at, updated_by)`: `recognition_enabled` (default **false**).
- `face_events(id, frigate_event_id UNIQUE, camera, started_at, ended_at, snapshot_file, face_count, recognition_state` (`off`/`unavailable`/`done`), `created_at)`.
- `face_detections(id, event_id FK cascade, box, score, keypoints, quality JSON, crop_file, outcome` (`AUTHORIZED`/`UNRECOGNIZED`/`INCONCLUSIVE`/`NOT_MATCHED`/`SYSTEM_ERROR`), `person_id FK set null, similarity, threshold_used, model_version)`.
- `people(id, display_name, notes, consent_recorded_at, consent_note, created_at, updated_at, active)`.
- `reference_faces(id, person_id FK cascade, image_file, embedding BLOB, model_version, quality JSON, created_at, created_by)`.
- `audit_log(id, at, user, action, target)` for enrollment, deletion and recognition switches.
Embeddings and reference images stay in the api volume, never in responses; encryption at rest with the existing key is proposed (decision 3).

### API contracts (all require sign-in; JSON errors `{error, detail}` like `/api/analysis`)
| Method and path | Purpose |
|---|---|
| `GET /api/face-events?camera=&outcome=&person_id=&from=&to=&limit=&cursor=` | History, newest first, keyset pagination |
| `GET /api/face-events/{id}` | One event with its detections (no embeddings) |
| `GET /api/face-events/{id}/snapshot`, `GET /api/face-detections/{id}/crop` | Images, authenticated, `Cache-Control: private, no-store` |
| `DELETE /api/face-events/{id}` | Deletes rows and files |
| `GET /api/recognition` / `PUT /api/recognition {enabled}` | Read and switch; switching on refused (409) while SFace is not ready or the gallery is empty; audit-logged |
| `GET /api/people`, `POST /api/people` (name, consent fields required) | Gallery list and create |
| `POST /api/people/{id}/faces` (multipart image) | Enrollment from an **uploaded** image only: exactly one face of sufficient size, else 422; stores image + embedding |
| `DELETE /api/people/{id}/faces/{face_id}`, `DELETE /api/people/{id}` | Removes images, embeddings, and unlinks past matches (kept as `UNRECOGNIZED`) |
| `GET /api/people/{id}/faces/{face_id}/image` | Reference image, authenticated |
There is deliberately **no** endpoint that enrolls from an event or crop.

### Recognition switch behaviour
- OFF (default): events, snapshots, crops and detections are stored; no embedding is computed; `recognition_state = off`; outcomes `NOT_MATCHED`. Turning OFF takes effect for the next event; nothing is matched retroactively when turned ON.
- ON with SFace not ready: `recognition_state = unavailable`, detection continues.
- Decision per face: best similarity ≥ `accept` → `AUTHORIZED` (person linked); ≤ `reject` → `UNRECOGNIZED`; between → `INCONCLUSIVE`; embedding failure → `SYSTEM_ERROR`. Thresholds are settings with the reference value as a documented start, calibrated on site.

### Security and privacy controls
- Biometric data (embeddings, reference images, face crops) only in the api volume; never in logs, never in API responses except the authenticated image routes; embeddings never leave the server.
- Explicit enrollment from uploads only, with a consent record; deletion removes files and embeddings at once; audit log for enrollment, deletion, switch changes.
- Retention: `FACE_EVENT_RETENTION_DAYS` (default 30) for events, snapshots and crops, through the existing retention loop and free-space guard; gallery data kept until deleted.
- Authorization: all signed-in users are the single admin today. Proposed: confirm the password again for deleting a person and for switching recognition (decision 4).
- The inference service stays internal with the bearer token; `/v1/embed` gets the same size and pixel limits as `/v1/detect`.
- Local law on biometric data (for example India's DPDP Act 2023 or GDPR) may require notice, consent and purpose limits; the product records consent but does not decide legality.

## 6. Phases, tests and acceptance criteria
| Phase | Work | Acceptance | Tests (kind) |
|---|---|---|---|
| 2 Face detection on camera events | Frigate client `events`, `event_snapshot`; ingestion loop calling `/v1/detect`; decide frame source (§4) | Person events with faces produce detections; events without faces are recorded as such; Frigate down → retries, no crash; nothing stored yet beyond memory | respx-mocked Frigate + mocked inference (mocked integration); real YuNet on the fixture through the loop (real model) |
| 3 Snapshots, events, dedup | Migration `0003` (events, detections); media files; cursor; retention | One event per Frigate event across restarts and overlaps; files and rows deleted together; free-space guard respected | Unit + mocked integration; migration up/down tests |
| 4 APIs | Event history, filters, images, delete | Auth required everywhere; pagination stable; no embeddings or paths in responses | In-process API tests |
| 5 Gallery and enrollment | People, consent, upload enrollment, quality gate, audit log | Exactly-one-face rule; no enrollment path from events; deletion complete | API tests with the fixture and synthetic images |
| 6 Recognition + switch | SFace in the registry and Dockerfile; `/v1/embed`; matcher; switch | OFF skips embedding (asserted by call counts); ON matches the enrolled fixture against transformed copies; thresholds documented | **Real-model tests** with SFace; needs identity test images (§7) |
| 7 Frontend | Events page with filters and images; People page with enrollment; recognition switch with states | All states shown; destructive actions confirmed | vitest + build |
| 8 Security/privacy review | Logs, responses, retention, deletion, audit | Checklist in VERIFICATION.md | Regression tests for findings |
| 9 Windows/WSL release prep | Full suites, static Compose, docs | All green on both | — |
| 10 Real stack | Docker, Frigate, test stream, then a consenting test person, then the CP Plus camera | Measured detection rate, match rates, latency, CPU/RAM | Real Docker/Frigate/camera (blocked today) |

## 7. Dependencies and blockers
1. **Decision A or B** (§3). Recommendation B.
2. **Identity test images**: at least two different people, several images each, public-domain or with written consent, for matching tests and threshold checks. Until then Phase 6 can test only "same image, transformed" versus "no match", which proves the plumbing, not accuracy.
3. **SFace download** at image build (38.7 MB, pinned checksum); the inference image grows accordingly.
4. **Real stack** (Docker, Frigate, the Linux AI PC) for Phase 10; no Docker on this PC.
5. **Policy decisions**: encryption at rest of biometric data; password re-confirmation; retention default; who may be enrolled and how consent is recorded.
6. Camera resolution at the door (§4) decides whether recognition is feasible at all; measured in Phase 2/10.

## 8. Phase 2: face detection on Frigate person events (done, 2026-10-29)
- `FrigateClient.person_events(after, limit)`: `GET /api/events?labels=person&has_snapshot=1&in_progress=0&after=…&sort=date_asc&limit=…` (parameters from `frigate/api/defs/query/events_query_parameters.py`; `after` compares the **start** time, `frigate/api/event.py`). Entries without a safe id (`[A-Za-z0-9.-]{1,30}`), a camera or both times are skipped.
- `FrigateClient.event_snapshot(id)`: `GET /api/events/{id}/snapshot.jpg?bbox=0&timestamp=0&crop=0&quality=95`. Without the explicit zeros the camera's snapshot settings apply (`_resolve_snapshot_settings`), and a drawn box would cover the face. Returns the frame time from Frigate's `X-Frame-Time` header.
- `FrigateClient.recording_snapshot(camera, frame_time)`: `GET /api/{camera}/recordings/{frame_time}/snapshot.jpg`, decoded by Frigate from the recording segment (the record stream: the main stream in our template); 404 when no segment covers the time.
- `face_detection.detect_event_faces(event)`: frame from the recording at the snapshot's frame time when `FACE_FRAME_SOURCE=recording` (default), else, or when no recording covers it, the event snapshot; then the existing YuNet via `/v1/detect` (`FACE_MIN_SCORE` 0.7, `FACE_MAX_FACES` 10). Final statuses: `faces_detected`, `no_faces_detected`, `frame_unavailable`, `frame_rejected`, `inference_failed`. Temporary problems (Frigate or inference unreachable, model not loaded, unexpected answers) raise, so the Phase 3 loop retries the event instead of recording a wrong result.
- Not yet: no background loop, nothing stored, settings not passed by Compose (Phase 3). Detection only; nothing identifies anyone.
- Evidence: `backend/tests/unit/test_face_detection.py`, 23 mocked tests (Frigate via respx, inference mocked) and 1 test with the **real YuNet** in-process on the public-domain fixture served as a Frigate snapshot: box (257, 115, 76, 102), score 0.9416, identical to AI-ANALYSIS.md. The real-model test needs the inference package and model alongside the backend: it ran in WSL; it is skipped on Windows and in CI's backend job.
- Open for Phase 10: the size of real faces in the recording versus the snapshot at the door, and the recording's availability right after an event ends (segments are flushed in steps; the fallback covers a missing segment).

## 9. Phase 3: face events stored, deduplicated, retained (done, 2026-10-29)
Operator description: [FACE-EVENTS.md](../FACE-EVENTS.md). Differences from section 5's draft schema, deliberately:
- `face_events` has no `recognition_state` and `face_detections` no `outcome`, `person_id`, `similarity`: those belong to recognition and are added by its own migration in Phase 6, so nothing pretends to be recognition before it exists.
- No cursor table: the cursor is derived from the newest stored `started_at` minus the lookback, which survives restarts without extra state; the unique `frigate_event_id` makes reprocessing harmless. Paging (`offset`, 50 per page, at most 20 pages per poll) keeps a full page of known events from hiding newer ones.
- A frame is kept only for `faces_detected` events (data minimisation); other outcomes store the row only. Statuses gained `gave_up` (temporary failure on the same event `FACE_MAX_ATTEMPTS` times).
- Temporary failures stop the poll instead of skipping the event, so the cursor cannot move past an event that was never analysed.
- Free-space floor shared with captures (`CAPTURE_MIN_FREE_MB`); retention `FACE_EVENT_RETENTION_DAYS` (default 30) runs hourly, files before rows, one commit per event.
- Found while writing the tests: a malformed face entry from the inference service would have raised inside `save()` on every poll and stalled ingestion on that event; `face_detection` now keeps only well-formed faces (box with four numbers, numeric score, five numeric keypoints).
Evidence: `test_face_events.py` (19, mocked Frigate and inference, real SQLite and files), migration model-drift and round-trip tests, `test_deploy_config.py`.
