# ADR-0004: Face events, authorized-person gallery and recognition (audit and plan)

- Status: **accepted: option B** (our matcher), owner's decision 2026-10-29. Progress: Phase 1 (audit) done; Phase 2 (face detection on events) done, see section 8; Phase 3 (storage, deduplication, retention) done, see section 9; Phase 4 (event API) done, see section 10; Phase 5 (gallery and enrollment) done, see section 11; Phase 6 (recognition and switch) done, see section 12; Phase 7 (frontend) done, see section 13; Phase 8 (security and privacy review) done, see section 14; Phase 9 (release preparation) done, see section 15. Phase 10 (real stack) is blocked on the Ubuntu host. The policy decisions (section 7, item 5) were taken on 2026-10-30.
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
2. **Identity test images**: resolved for Phase 6 with four public-domain NASA portraits of three people (section 12). That is a sanity check; real accuracy needs consenting people on the real camera (Phase 10).
3. **SFace download** at image build (38.7 MB, pinned checksum); the inference image grows accordingly.
4. **Real stack** (Docker, Frigate, the Linux AI PC) for Phase 10; no Docker on this PC.
5. **Policy decisions** (owner's decisions, 2026-10-30): reference images (and later embeddings) **encrypted at rest** with the existing AES-256-GCM key; **password re-confirmation** for deleting a person and for switching recognition; consent recorded as a **date plus a note** on where the evidence is kept (no document upload); gallery data **kept until deleted**.
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

## 10. Phase 4: face event API (done, 2026-10-29)
`backend/ai_surveillance/routes/face_events.py`; contract in [FACE-EVENTS.md](../FACE-EVENTS.md#api). Differences from section 5's table: the list filter is `status` (detection statuses) plus `has_faces`, because recognition outcomes and `person_id` do not exist until Phase 6; the crop route is nested, `/api/face-events/{id}/detections/{detection_id}/crop`, so a crop is reachable only through its own event; the list answers `{items, next_cursor}` with an opaque keyset cursor on (start time, id). Ids are checked against the server's 32-hex format before any lookup; images are served only from `media/faces` through the media store's name and path checks. Evidence: `test_face_events_api.py` (17).

## 11. Phase 5: gallery and enrollment (done, 2026-10-30)
`gallery.py`, `routes/people.py`, `routes/audit.py`, migration `0004_gallery`; operator description and contract in [GALLERY.md](../GALLERY.md). Differences from section 5's draft, deliberately:
- `reference_faces` has no `embedding` or `model_version` yet: they are added with SFace in Phase 6, so nothing pretends to be recognition before it exists. The original upload is kept (encrypted), so embeddings can be computed from it later without asking for the image again.
- Consent is `consent_date` (a date) plus a required `consent_note`, per the policy decision; `notes` and `created_by` were added.
- Encryption: `CredentialCipher.encrypt_bytes` (AES-256-GCM, the device-password key, fresh 12-byte nonce, associated data `reference_face:<id>`), files `<uuid>.enc` under `media/gallery`. The same key now protects two kinds of data, so losing it means re-entering device passwords **and** re-enrolling everyone.
- Exactly-one-face is decided with a lower counting floor (`GALLERY_OTHER_FACE_SCORE` 0.5) than the acceptance score (`GALLERY_MIN_SCORE` 0.9), so a weak second face still refuses the image; the quality gate also needs 80 px and a face clear of the edge. With the real YuNet the public-domain fixture's 76 px face is refused at the default and accepted at 60 px.
- Password re-confirmation shares the sign-in lockout and answers 403 (not 401) so the session is not dropped; failures are audited. It covers deleting a person now and will cover the recognition switch in Phase 6. Removing one reference image needs a session only.
- `audit_log` has no foreign keys and stores ids and field names only, so it survives deletions without keeping names or consent text.
- "Unlinks past matches" (section 5's delete row) has nothing to unlink until Phase 6 adds match results.
- No route enrolls from an event: checked by a test over the OpenAPI schema (the only write routes under `/api/people` and `/api/face-events` are create person, update person and upload a face) and by a JSON body with a face event id being refused (415).
Evidence: `test_gallery_api.py` (40 in-process tests with a mocked detector, 1 with the real YuNet in WSL), `test_migrations.py` (0004 downgrades alone and keeps face events).

## 12. Phase 6: recognition and the switch (done, 2026-10-30)
`inference_service/adapters/sface.py`, `POST /v1/embed`; `backend/ai_surveillance/recognition.py`, `routes/recognition.py`, migration `0005_recognition`; operator description in [RECOGNITION.md](../RECOGNITION.md). Decisions and differences from section 5:
- **Identity test images** (§7 item 2): three further official NASA portraits from Wikimedia Commons (public domain), downloaded with the owner's approval on 2026-10-30 and recorded in `inference/tests/data/public_domain/README.md`: a second Armstrong photo (1964) plus Aldrin and Collins. Real SFace: same person 0.617; different people 0.118–0.194; the same photo under re-encoding, scaling, brightening and mirroring 0.94–1.0.
- **Thresholds**: accept 0.45, reject 0.30 (settings). They bracket OpenCV's 0.363 so borderline faces are `INCONCLUSIVE`, and separate the fixtures with margin. Also `INCONCLUSIVE` when two people are above accept, or when the face is under 40 px. Starting points only; Phase 10 measures on the real camera.
- `/v1/embed` returns detection plus embedding in one pass (the api does not send boxes back); the detector stays separately available, so the OFF path calls `/v1/detect` only, asserted by call counts.
- Reference embeddings are stored **encrypted** (`embedding_enc`, AES-256-GCM, associated data `reference_embedding:<id>`) per the policy decision; section 5's plain `embedding BLOB` was not used. Enrollment embeds in the same pass when SFace is loaded; otherwise it falls back to detection and the face is embedded later from its encrypted image (re-detected, matched to the enrolled box by overlap ≥ 0.5). Faces that cannot be embedded are skipped for the rest of the process's life instead of being retried every poll.
- Event embeddings are **not stored**; only outcome, similarity, thresholds used and the matched person. The draft's `model_version` per detection became `face_events.recognizer`.
- Section 5 said deleted people's matches are "kept as `UNRECOGNIZED`". Changed: they stay `AUTHORIZED` (true at the time) with `person_id` set to null, so the record no longer names the person and does not claim the face was unknown.
- The switch is `app_settings.recognition_enabled`, read at the start of each poll; switching needs the password (decision 4); switching on embeds pending references first and is refused without a ready embedder or a usable gallery.
- Migration safety: SQLite foreign keys stay enforced during migrations, so rebuilding a parent table (`face_events`) would cascade-delete its detections. Only the child table `face_detections` is rebuilt; downgrade drops the other columns with native `DROP COLUMN`. Tested with data both ways.
Evidence: `inference/tests/test_api.py` (6 new `/v1/embed` contract tests), `inference/tests/test_model_sface.py` (4, real SFace), `backend/tests/unit/test_recognition.py` (29 mocked plus 1 end-to-end with real YuNet and SFace: enroll Armstrong 1964 through the API, switch on, ingest three Frigate events: Armstrong 1969 `AUTHORIZED` 0.617, Aldrin 0.178 and Collins 0.118 `UNRECOGNIZED`).

## 13. Phase 7: frontend (done, 2026-10-30)
`frontend/src/components/faces/`: `FaceEventsPage` (route `/events`), `PeoplePage` with `RecognitionPanel`, `PasswordDialog` and the audit log (route `/people`), shared wording in `labels.ts`; API calls and types in `api.ts`. Decisions:
- Every outcome and recognition state has its own label and colour, with a tooltip saying what it means; a match on a deleted person reads "Authorized: deleted person"; rows from before recognition read "No result".
- Destructive actions are confirmed: deleting an event or a reference image asks first; deleting a person and switching recognition either way open a password dialog (403 and 429 explained in it). A 409 refusal of switching on closes the dialog and says why (embedder not ready, no usable gallery). The switch button is disabled while the embedder is not ready.
- Enrollment shows the server's rejection reasons one per line; file type and size are checked in the browser first. There is no control that sends a face event or crop to the gallery.
- Images are plain `<img>` requests to the signed-in routes (same-origin cookie); nothing is cached by the app.
- Found while checking in a real browser: the consent date defaulted to the UTC date and also capped the date picker, so before 05:30 in India today's date could not be chosen; it now uses the local date. The audit log's 32-character ids widened the page on a phone; table cells now wrap.
Evidence: `frontend/src/test/faces-pages.test.tsx` (13), and a manual run of the real api, the real inference service (YuNet + SFace) and the Vite dev server with a throwaway database.

## 14. Phase 8: security and privacy review (done, 2026-10-30)
Checklist, findings and known limits in [VERIFICATION.md](../VERIFICATION.md) (Phase 8 section). Three findings fixed, each with a regression test that failed before the fix: (1) the switch and gallery were read once per poll, so switching off or deactivating someone did not apply until the next poll; now read before every event; (2) deleted rows stayed readable in SQLite free pages; `secure_delete=ON`; (3) deleting a face event was not audited; now `face_event_deleted`. Not fixed, documented: unlinked files are not overwritten (full-disk encryption recommended), Frigate's own snapshots and recordings, backups keep deleted data, no audit-log retention, image views not audited, single administrator, no liveness.

## 15. Phase 9: release preparation (done, 2026-10-30)
All suites green on Windows and Linux (WSL), static Compose validation without Docker (schema and references, both file sets), results in [VERIFICATION.md](../VERIFICATION.md) (Phase 9). One deployment change: the recognition thresholds and the reference-image size floor are passed by Compose from `deploy/.env`, so Phase 10 can calibrate them on site without rebuilding; a test keeps the Compose default, the example value and the code default equal. Operator docs (README, SETUP, TROUBLESHOOTING) now cover the face features. Phase 10 needs the Ubuntu host: Docker, Frigate, the synthetic stream, then a consenting test person, then the CP Plus camera.

## 16. Re-check against the backend-API brief (2026-10-30)
The owner re-issued the backend/persistence brief. Everything it lists already existed (sections 9–12, 14), except one point: it asks for explicit statuses including "insufficient quality". A face too small to compare was stored as `INCONCLUSIVE`, indistinguishable from an in-between score or two people matching except by its missing similarity. Each recognition result in the event API now carries a `reason` (`match`, `below_reject_threshold`, `gallery_empty`, `between_thresholds`, `several_people_match`, `insufficient_quality`, `recognition_error`, `recognition_off`, `recognition_unavailable`, `not_performed`), derived in `recognition.outcome_reason` from stored values, so no migration and older rows included. The outcome set itself is unchanged. The Face events page shows the reason in words. Evidence: `test_recognition.py` (10 reason cases, and an ingestion-to-API test producing `insufficient_quality`, `several_people_match`, `between_thresholds`), `faces-pages.test.tsx` (+1).

## 17. Re-check against the recognition brief (2026-10-30)
The owner re-issued the recognition brief. Already covered by section 12 and the Phase 8 fixes: an approved, pinned model with provenance and licence; detector and embedder kept separate; embeddings only from uploaded references and detected faces; the enrollment gate; L2 normalisation and cosine comparison; configurable thresholds with `UNRECOGNIZED` below them and never a forced closest match (`INCONCLUSIVE` for two people above accept); insufficient-quality, unavailable-model and error states; persisted outcome, person, similarity, thresholds and model version; encrypted embeddings never returned; the gallery re-read before every event so edits and deletions apply at once; tests for empty gallery, known and unknown subjects, low quality, ambiguity, invalid embeddings and model failures. The missing item was **memory use and inference time on the available PC**: `python -m inference_service.benchmark` (with `tests/test_benchmark.py`) now measures load time, latency and process memory, and the laptop results are in [RECOGNITION.md](../RECOGNITION.md) (detect 13.5 ms / embed 9.5 ms per face on Windows at 480×600; 50 ms detect at 1080p; under 300 MB). No accuracy or false-match rate is claimed: four photos of three people are not an evaluation set.

## 18. Re-check against the recognition ON/OFF brief (2026-10-30)
The behaviour already existed (sections 12, 14). The brief asked for specific evidence, now in `backend/tests/unit/test_recognition_switch.py` (16 tests; no production change was needed):
- with recognition off, every matching step (`match`, `match_face`, `load_gallery`, `embed_missing_references`, `current_embedder`) is replaced by a function that fails if called; a valid face is processed and the event, frame file and detection (`NOT_MATCHED`) are stored; a control test shows the same replacement trips when recognition is on, so the check sits on the real path;
- no stored setting means off; a damaged value fails closed; the default after a fresh install is off;
- the setting survives an application restart in both directions;
- switching off leaves reference embeddings byte-for-byte unchanged;
- switching on does not identify past events, even when Frigate lists them again;
- off, unavailable and attempted states are recorded and explained; a recognition failure while on is `SYSTEM_ERROR`;
- only `GET`/`PUT /api/recognition` touch the setting, `"true"` as a string is refused, unauthorized requests change nothing.
Switching off while events are queued is covered by `test_privacy_review.py` (section 14).

## 19. Re-check against the frontend brief (2026-10-30)
The pages already existed (section 13). Gaps closed:
- **Editing people**: the People page could only activate or deactivate; it now edits name, notes and the consent record (only changed fields are sent, so the audit log lists exactly those; nothing changed sends nothing; server refusals are shown).
- **Automated, not verified**: a match read "Authorized: <name>", like a fact. Badges now read "Automated match: <name>", the reason text and the event view say "automated matches, not verified identities", and the outcome filter says "Authorized (automated match)".
- **Network and api-down states**: a failed request showed the browser's "Failed to fetch". The API client now says the server could not be reached. Checking against the real stack showed that an api that is down does not fail the request at all: the proxy answers 502 (Caddy) or 500 (Vite in development) without our JSON body. Gateway errors 502–504 without that body now read "The server is not responding (it may be restarting)"; the api's own 503 answers keep their detail. A bare 500 cannot be told apart from a real server error and is shown as is.
Evidence: `faces-pages.test.tsx` (+7; whole frontend 68), and a run against the real api: edit form prefilled, a control character refused with the server's message, a valid edit saved and audited by field name, the embedder shown as "Not ready (unreachable)" with the inference service stopped.

## 20. Second security and reliability audit (2026-10-30)
Focused on what the Phase 8 review (section 14) did not exercise: concurrency between enrollment, deletion and the per-person limit; long-running memory; symlinks in the face and gallery stores; what exception text reaches stored data; licensing and operator procedures. Three defects reproduced by failing tests and fixed (`backend/tests/unit/test_reliability_audit.py`):
- Deleting a person while their image was being enrolled crashed the upload with an unhandled foreign-key error (HTTP 500). The upload now ends its read transaction after the slow detection step, checks the person again (deleted → 404, deactivated → 409) and treats a deletion between that check and the commit as 404; the written file is removed in every case.
- The limit of 10 reference images per person was checked only before the upload, so overlapping uploads could exceed it. It is checked again right before the row is written.
- Retry counters for events that failed temporarily and then disappeared from Frigate stayed in memory for the life of the api. After a poll that lists the whole window, counters of events no longer listed are dropped.
No defect found: symlinks planted in `media/faces` or `media/gallery` are refused by the media store (tests run on Linux; Windows needs privileges), Frigate error texts stored in event details carry exception type names only. New documentation: [LICENSES.md](../LICENSES.md) (models, Python and npm packages from their own metadata, container images, test photos; what is not verified) and an operator procedure for enrollment and removal requests in [GALLERY.md](../GALLERY.md).
