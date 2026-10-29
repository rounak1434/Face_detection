# Architecture

## Goals
- Reliable, local-only surveillance for cameras the operator owns and is authorized to monitor.
- Reuse a proven video engine instead of rebuilding detection, recording and restreaming.
- A clear boundary: the browser talks only to our API, and the API talks to the engine.
- Every hardware claim is backed by a recorded test ([VERIFICATION.md](VERIFICATION.md)).

## Components
```
 CP Plus camera ──RTSP──► CP Plus NVR (independent 24/7 recording)
                               │ RTSP main + sub stream
 ┌──────────── Linux host (i7, 32 GB RAM, GTX 1650 4 GB) · docker compose ────────────┐
 │                                                                                    │
 │  frigate   0.18.2: go2rtc restream · detection · recordings · exports             │
 │     ▲      internal network only (port 5000); UI on 127.0.0.1:8971                 │
 │     │ HTTP + websocket                                                             │
 │  api       FastAPI: sign-in, camera list, live relay, snapshot/clip capture,       │
 │            capture index (SQLite) and media store                                  │
 │     ▲                                                                              │
 │  inference face models (YuNet detection, SFace embeddings) behind adapters; internal│
 │            only; models verified at build time; keeps no images                    │
 │     ▲                                                                              │
 │  web       Caddy: HTTPS (local CA), security headers, serves the React UI,         │
 │            proxies /api → api. The only service published on the LAN.            │
 │                                                                                    │
 │  test-stream (optional profile): mediamtx + ffmpeg test pattern over RTSP          │
 └────────────────────────────────────────────────────────────────────────────────────┘
                               ▲ HTTPS
                        operator browser
```

## Request flows
- **Live view:** the browser opens `wss://<host>/api/cameras/<cam>/live`. The api checks the session cookie and that the Origin matches the Host, then relays go2rtc's MSE websocket from Frigate. If MSE fails, the UI polls `/api/cameras/<cam>/latest.jpg` once per second.
- **Snapshot:** `POST /api/cameras/<cam>/snapshot`. The api first checks with `/api/stats` that Frigate is receiving frames, because Frigate serves a placeholder image for an offline camera. It then fetches the latest frame, rejects offline fallback frames, writes the image atomically under `media/captures/` and indexes it.
- **Clip:** `POST /api/cameras/<cam>/clip` returns 202 with a pending capture. A background task then does the following:
  1. Waits for the current recording segment to be flushed.
  2. Queues a Frigate export of `[now-N, now]`.
  3. Polls the export job (`/api/jobs/export/<id>`). Once Frigate forgets the finished job, it reads the export row instead.
  4. Downloads the MP4 and marks the capture `ready`, or `failed` with Frigate's reason.

  The exact Frigate interfaces are listed in [FRIGATE-API.md](FRIGATE-API.md).
- **Media:** `GET /api/captures/<id>/file`. Requires sign-in. File names are server-generated (`<uuid>.jpg|mp4`) and validated before any disk access.

- **AI analysis:** `POST /api/analysis/faces` takes a multipart upload, checks its size, structure and magic bytes, and forwards the image bytes to the internal inference service (`POST /v1/detect`). The service decodes it safely, runs YuNet and returns boxes, keypoints, crops and quality hints. Nothing is stored: uploads are parsed in memory, with no temporary files. Details in [AI-ANALYSIS.md](AI-ANALYSIS.md).
- **Face events:** a background loop in the api polls Frigate's finished person events, takes each event's best frame (from the recording when possible), runs the face detector in the inference service and stores one face event per Frigate event, with frame and crops only when faces were found. `GET /api/face-events` and its image routes serve the history behind sign-in. Faces are identified only while recognition is switched on (below). Details in [FACE-EVENTS.md](FACE-EVENTS.md).
- **Gallery:** `/api/people` holds authorized people with a consent record. `POST /api/people/<id>/faces` takes an uploaded image only, runs the face detector through the inference service and accepts it only with exactly one usable face; the image is stored AES-256-GCM encrypted under `media/gallery/`. Deleting a person needs the password again; changes go to `/api/audit-log`. Details in [GALLERY.md](GALLERY.md).
- **Recognition:** off by default; `PUT /api/recognition` (password again) switches it. While on, each face-event poll embeds pending reference faces, then sends each new event's frame to `/v1/embed` (detection plus SFace embeddings) and matches every face against the active gallery's encrypted embeddings: `AUTHORIZED`, `UNRECOGNIZED`, `INCONCLUSIVE` or `SYSTEM_ERROR`. Event embeddings are discarded after matching. Details in [RECOGNITION.md](RECOGNITION.md).
- **Database migrations:** Alembic runs at api startup (`ai_surveillance/migrations`).
  - A database created before migrations existed is checked column by column against `0001_baseline`. Only an exact match is backed up and stamped; anything else stops startup unchanged.
  - Any existing SQLite file is backed up before a schema change.
  - Details in [DATABASE.md](DATABASE.md).
- **Camera & NVR setup:** `/api/devices` (CRUD) and `POST /api/devices/<id>/test`. The api checks the address, opens a TCP connection, then runs ffprobe against the device. The browser webcam preview runs entirely in the browser and never reaches the api.

## Security model
| Concern | Measure |
|---|---|
| Operator access | Single admin account (more roles in a later milestone): argon2id, minimum 12-character password, per-client lockout after 5 failures, httpOnly + Secure + SameSite=Strict session cookie, server-side sessions stored as SHA-256 hashes. |
| Engine exposure | Frigate port 5000 is unauthenticated, so it is never published. Port 8971 is bound to loopback. The browser never contacts Frigate. |
| Camera credentials | Kept only in `deploy/.env` (gitignored, chmod 600) and passed only to the Frigate container. API responses never include stream URLs. Probe output masks `user:pass@`. |
| Transport | HTTPS via Caddy's local CA. HSTS, CSP, no framing. |
| Camera/NVR setup | Passwords are write-only and AES-256-GCM encrypted. Tests run only for saved devices. The SSRF guard allows private networks only, and the probe connects to the checked IP. Details in [CAMERA-SETUP.md](CAMERA-SETUP.md). |
| Biometric data | Reference images only from administrator uploads with a consent record; images and embeddings encrypted at rest with the credentials key; images served only to signed-in users without caching, embeddings never; event embeddings not stored; recognition off by default; deleting a person and switching recognition need the password again and are audited. Face-event frames and crops expire after `FACE_EVENT_RETENTION_DAYS`. |
| Uploaded and saved files | No client-chosen paths. Strict file-name pattern. Resolved path must stay inside the media directory. |
| Data residency | No cloud calls at runtime. Model downloads (later milestones) are one-time and documented. |

## Planned milestones
1. **Foundation (this milestone):** clean repo, live view, snapshot/clip, probe, tests, docs.
2. **Registry and matching:** authorized-person enrollment and import with consent records, face matching that yields `AUTHORIZED` / `UNRECOGNIZED` / `INCONCLUSIVE` / `SYSTEM_ERROR`, Frigate MQTT events, Alembic migrations. Starts by evaluating Frigate's built-in face recognition against our own matcher (see ADR-0001). Audit, proposed design and phase plan: [ADR-0004](adr/0004-face-events-and-recognition.md) (proposed, decisions pending).
3. **Events:** event history with snapshots, clips, detection details and retention rules. Facial attribute estimates are shown as fallible estimates only.
4. **Alerts and alarm:** confidence-gated alerts. Alarm output only through an integration verified on the real hardware.

## Rename-readiness
The display name comes from `APP_DISPLAY_NAME`. Package, service and database names are generic (`ai_surveillance`, `api`, `web`, `app.db`).
