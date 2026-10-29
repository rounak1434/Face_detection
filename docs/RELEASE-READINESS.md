# Release readiness

Status of the whole product against the goals in [ARCHITECTURE.md](ARCHITECTURE.md) (milestones 1–4). Test evidence lives in [VERIFICATION.md](VERIFICATION.md); this page is the summary and the plan. Last updated 2026-10-29, base commit `02c5ab8`.

Labels: **Done (verified)** tested here; **Done (tests only)** implemented, tested with mocks, never run on the real stack; **Partial**; **Missing**; **Blocked** needs the Ubuntu host, Docker, the GPU or the CP Plus devices.

## Feature matrix
| Area | Status | Evidence / gap |
|---|---|---|
| Admin sign-in, sessions, lockout | Done (tests only) | `security.py`, `routes/auth.py`; `test_auth.py`, `session.test.tsx`. One admin account; no roles, no password change in the UI (CLI `create-admin` resets). |
| Camera list, live view (MSE relay), still-frame fallback | Done (tests only) | `routes/cameras.py`, `routes/live.py` (Origin + session check); `test_rtsp_and_live.py`. Real go2rtc stream: Blocked. |
| Snapshot and clip capture, captures gallery | Done (tests only) | `captures.py`, `routes/captures.py`; `test_cameras_and_captures.py` (30). Real Frigate export: Blocked. |
| Capture deletion | Done (tests only) | `DELETE /api/captures/{id}` and a Delete button with confirmation in the gallery; `test_cameras_and_captures.py`, `app.test.tsx`. Frigate's export of each clip is deleted once our copy is stored (and when a clip is deleted), unless `CLIP_KEEP_FRIGATE_EXPORT=true`; mocked Frigate only. Retention does not yet remove Frigate exports whose cleanup failed. |
| Capture retention, free-space guard | Done (tests only) | `CAPTURE_MIN_FREE_MB` (default 1024): captures refused with 507 below the floor, also checked before a clip is downloaded; `CAPTURE_RETENTION_DAYS` (default 0 = keep): old finished captures deleted at startup and hourly, one commit per capture; an unreadable free-space value pauses captures (507). `test_capture_storage.py` (20), Windows and Linux. Real full disk and the real hourly timer not observed. Frigate's own recordings and exports are governed by Frigate's settings, not by these. |
| Camera stream reconnection | Delegated to Frigate | Not our code; behaviour on a real camera drop: Blocked. |
| RTSP probe (`ai-surveillance probe`) | Done (tests only) | `rtsp.py`; credentials masked. Needs ffprobe: Blocked here. |
| Camera & NVR setup page (CRUD, encrypted passwords, SSRF guard, test) | Done (tests only) | `devices.py`, `routes/devices.py`, `netguard.py`; `test_devices.py`. "Apply to Frigate" feeds switched-on, connected devices to Frigate (ADR-0003, all 5 steps), tested with a simulated Frigate and static Compose checks only: **Partial until one real apply on the Ubuntu host**. |
| Face detection (YuNet), crops, quality hints | Done (verified, dev PC) | Real model on the public-domain fixture; `inference/tests`. Accuracy on CP Plus footage: not measured. |
| Face tracking across frames | Missing | Frigate tracks objects (person); no face tracks of our own. |
| Face recognition, enrollment, consent records (milestone 2) | **Missing** | No code. Engine choice (Frigate built-in vs. own matcher) still to be evaluated per ADR-0001. |
| Age, expression, glasses, beard, mask estimates (milestone 3) | **Missing** | No models in the registry. Must be shown as fallible estimates only. |
| Event history (milestone 3), Frigate MQTT events | **Partial** | Face events from Frigate person events are ingested and stored (ADR-0004 Phase 3, [FACE-EVENTS.md](FACE-EVENTS.md); mocked Frigate and real YuNet only); history API with filters, paging, images and delete (Phase 4); no page yet (Phase 7). No MQTT: Frigate's HTTP events API is polled instead. |
| Alerts and alarm output (milestone 4) | **Missing** | No code; must be verified on the real hardware first. |
| Database migrations | Done (verified) | Alembic baseline plus `0002_device_use_in_frigate`; `test_migrations.py` (26). |
| Backup, verify, restore, interruption handling, recovery copies | Done (verified: Windows + Linux/WSL) | `dbtools.py`; `test_dbtools.py` (79); 41-check drill. Power loss and real signals: not shown. |
| Configuration and secrets | Done (tests only) | pydantic settings; `.env` gitignored; per-service env lists in compose; api ↔ inference bearer token (`INFERENCE_API_TOKEN`); `test_deploy_config.py`, `test_inference_token.py`. |
| Health checks | Partial | api (compose) and inference (Dockerfile) have health checks; none is defined for frigate or web in this repository (whether their images ship one is unchecked). |
| Logging | Partial | Plain stdout logging; no rotation config beyond Docker's default. |
| Start after reboot | Done (docs only) | `restart: unless-stopped` + systemd unit; Blocked: not run on the host. |
| Desktop launcher | Not in scope | This product is a web app served by Docker; there is no desktop launcher in this repository. |
| Packaging: Docker images, model download at build | Done (static checks only) | Dockerfiles, pinned `constraints.txt`; never built (no Docker here). |
| Static analysis / CI | Done; the intermittent failure on GitHub runners was a timing-dependent test (`test_delete_refused_while_clip_is_pending`), fixed; failures are reported as annotations | `.github/workflows/ci.yml`: backend, inference (with the real model) and frontend jobs; `ruff` (pyflakes, pycodestyle errors, bugbear) for both Python packages; `tsc` in the frontend build. No Python type checker yet. |

## Backlog (ranked)
| # | Priority | Task | Acceptance criteria | Files | Tests |
|---|---|---|---|---|---|
| 1 | Data loss / security | **Done 2026-10-29 (`9fe5078`):** disk errors while saving captures | Failed capture instead of 500; no storage path in API responses; no `.part` left; leftovers removed at startup; never stuck "pending" | `media.py`, `captures.py`, `main.py` | 4 new in `test_cameras_and_captures.py` |
| 2 | Data loss | **Done 2026-10-29 (`effd703`):** delete captures (API + UI button). Follow-up done 2026-10-29: Frigate's export is deleted via `POST /api/exports/delete` (checked against the 0.18.2 source) | `DELETE /api/captures/{id}` (auth) removes the row and file; 404 for unknown; the gallery offers delete with confirmation; deleting a pending clip is refused | `routes/captures.py`, `media.py`, `frontend/src/components/Captures.tsx`, `api.ts` | backend route tests; vitest for the button |
| 3 | Data loss | **Done 2026-10-29 (`4f83a69`, reviewed and hardened in the following commit):** capture retention and a free-space guard | Configurable `CAPTURE_RETENTION_DAYS` (default off) and `MIN_FREE_DISK_MB`; captures refused with a clear error below the free-space floor so `app.db` keeps room | `config.py`, `captures.py`, `main.py`, `.env.example`, docs | unit tests with a fake disk-usage function |
| 4 | Security (low) | **Done 2026-10-29 (`8db9246`):** shared secret between api and inference (`INFERENCE_API_TOKEN`, optional for compatibility; generated by the setup steps) | `/v1/detect` refuses requests without the token; token generated at install, never logged | `inference/main.py`, `inference_client.py`, compose, docs | both services' tests |
| 5 | Reliability | Health checks for frigate and web; `depends_on: condition: service_healthy` where useful | `docker compose ps` shows health for all services | `deploy/compose.yaml`, `test_deploy_config.py` | static test; host check Blocked |
| 6 | Release | **Done 2026-10-29:** CI: backend, inference, frontend tests and build on push; add `ruff` | Workflow green on the branch | `.github/workflows/ci.yml`, `pyproject.toml` | the workflow itself |
| 7 | UX (low) | Move the side effect out of the `setUser` updater in `App.tsx` | Session-expired notice shown once under StrictMode | `App.tsx` | `session.test.tsx` |
| 8 | Feature (M1 gap) | Feed saved devices to Frigate. **Design accepted in [ADR-0003](adr/0003-saved-devices-to-frigate.md)** (Frigate 0.18.2 API checked in source; option A). Steps 1–4 of 5 done (config renderer with placeholders only; secret-file writer: atomic, owner-only, stale files removed, snapshot and restore; "Use in Frigate" switch: migration `0002`, API, UI, only for connected devices; "Apply to Frigate": restart first when a secret is new, then `config/save` with restart, verification and rollback, mocked Frigate only). **Step 5 done:** Compose (live config in `storage/frigate-config` seeded once by `frigate-init`, `frigate-secrets` volume at `/run/secrets`), secret files ordered so go2rtc can always start, SETUP 4a and TROUBLESHOOTING. Remaining: the real-stack check (SETUP section 6, synthetic source), Blocked on the Ubuntu host | A saved, tested device appears as a Frigate camera without editing `.env` | `devices.py`, Frigate config generation, docs | unit tests; Blocked for the real NVR |
| 9 | Feature (M2) | Recognition engine evaluation, then enrollment with consent, matching (`AUTHORIZED`/`UNRECOGNIZED`/`INCONCLUSIVE`/`SYSTEM_ERROR`). **Phase 1 audit and plan done: [ADR-0004](adr/0004-face-events-and-recognition.md)** (recommends our matcher: Frigate person events → YuNet → SFace in the existing inference service); blocked on the owner's decisions and on identity test images | Per ADR-0004 section 6 | new modules, migrations | per phase, ADR-0004 |
| 10 | Feature (M3/M4) | Events with MQTT, attribute estimates, alerts and alarm output | Per ARCHITECTURE milestones; alarm only after hardware verification | new | plan separately |

## Frigate integration hardening, 2026-10-29
| Task | Result | Commit |
|---|---|---|
| B: damaged stored passwords | 3 kinds crashed with HTTP 500; now 409 with a repair hint everywhere, nothing changed | `ecb8596` |
| A: audit of Apply and rollback (15 scenarios) | 5 defects fixed (raw OS errors, unusable stored URL, lost restart answer vs HTTP error, unknown outcome not stated, kept copy overwritten on retry); table in VERIFICATION.md | `2d98ff2` |
| C: secret handling review | URL-encoded password survived redaction of cut-off Frigate messages (fixed); removal of a device used in Frigate now says it stays until the next apply | `ff5e853` |
| D: static Compose validation | No Docker available; both combinations valid against the Compose schema, cross-reference tests added | `02c5ab8` |
| E: real Frigate stack | **Not started: Blocked** (no Docker here, Linux AI PC unavailable). Checklist: SETUP section 6 step 4 and the host list below | — |

Test evidence by kind (do not mix them up):
| Kind | What exists | Count (2026-10-29) |
|---|---|---|
| Unit tests | Backend, inference, frontend | backend 447 passed / 7 skipped (Windows), 453 / 1 (Linux); inference 54 / 1; frontend 47 |
| Simulated integration | `test_frigate_apply.py` against a fake Frigate that copies the 0.18.2 behaviour read in its source; respx-mocked Frigate elsewhere | part of the backend count |
| Static Compose validation | `test_deploy_config.py` (CI) and a one-off schema check | 29 tests; 0 schema errors |
| Real Docker / Frigate / go2rtc | none | 0 |
| Real camera / NVR / GPU | none (only the YuNet model on a public-domain photo) | 0 |

## Hardware- and host-dependent checks (Blocked)
- Build all images; `docker compose config` and `up` on Ubuntu; `doctor.sh`; `run-integration-tests.sh`.
- Frigate accepts the config; test stream; live view, snapshot and clip through the real stack.
- `docker compose config --quiet` for `compose.yaml` and with `compose.nvidia.yaml` (never print `.env`).
- `frigate-init` exits 0, seeds the live config only when absent, and the secrets volume is uid 10001, mode 0700.
- One "Apply to Frigate" with the synthetic source (two restarts, camera listed, no credentials in the tracked template, the live config or logs), then one that removes it, then replacing one device with another; record restart times.
- In a test environment only: a refused config and Frigate stopped during an apply; the kept copies stay at two at most and stale secret files disappear after a successful apply.
- CP Plus NVR main/sub streams (models not yet recorded); GTX 1650 detection; start on boot and power recovery.

## Deployment blockers
1. No access to the Ubuntu AI PC (SSH destination unknown); no Docker on the dev PC.
2. Frigate's recordings and exports (`storage/frigate-media`) are a separate disk consumer, sized by Frigate's own retention (`record.continuous.days: 3`); their effect on the host disk is unmeasured.

## Release acceptance criteria (milestone 1)
- All unit suites green on Windows and Linux; frontend build green.
- Compose stack builds and starts on the Ubuntu host; `doctor.sh` clean; integration tests pass against the test stream.
- Live view, snapshot and 10 s clip verified in a browser against the real stack; then against the CP Plus NVR.
- Captures can be deleted, and the disk cannot be filled silently.
- VERIFICATION.md has no "Unverified (host)" lines left for milestone 1 features.
