# Release readiness

Status of the whole product against the goals in [ARCHITECTURE.md](ARCHITECTURE.md) (milestones 1–4). Test evidence lives in [VERIFICATION.md](VERIFICATION.md); this page is the summary and the plan. Last updated 2026-10-29, base commit `683e486`.

Labels: **Done (verified)** tested here; **Done (tests only)** implemented, tested with mocks, never run on the real stack; **Partial**; **Missing**; **Blocked** needs the Ubuntu host, Docker, the GPU or the CP Plus devices.

## Feature matrix
| Area | Status | Evidence / gap |
|---|---|---|
| Admin sign-in, sessions, lockout | Done (tests only) | `security.py`, `routes/auth.py`; `test_auth.py`, `session.test.tsx`. One admin account; no roles, no password change in the UI (CLI `create-admin` resets). |
| Camera list, live view (MSE relay), still-frame fallback | Done (tests only) | `routes/cameras.py`, `routes/live.py` (Origin + session check); `test_rtsp_and_live.py`. Real go2rtc stream: Blocked. |
| Snapshot and clip capture, captures gallery | Done (tests only) | `captures.py`, `routes/captures.py`; `test_cameras_and_captures.py` (30). Real Frigate export: Blocked. |
| Capture deletion | Done (tests only) | `DELETE /api/captures/{id}` and a Delete button with confirmation in the gallery; `test_cameras_and_captures.py`, `app.test.tsx`. Frigate's own copy of an exported clip (in `frigate-media/exports`) is not removed. |
| Capture retention, free-space guard | Done (tests only) | `CAPTURE_MIN_FREE_MB` (default 1024): captures refused with 507 below the floor, also checked before a clip is downloaded; `CAPTURE_RETENTION_DAYS` (default 0 = keep): old finished captures deleted at startup and hourly. `test_capture_storage.py`. Frigate's own recordings and exports are governed by Frigate's settings, not by these. |
| Camera stream reconnection | Delegated to Frigate | Not our code; behaviour on a real camera drop: Blocked. |
| RTSP probe (`ai-surveillance probe`) | Done (tests only) | `rtsp.py`; credentials masked. Needs ffprobe: Blocked here. |
| Camera & NVR setup page (CRUD, encrypted passwords, SSRF guard, test) | Done (tests only) | `devices.py`, `routes/devices.py`, `netguard.py`; `test_devices.py` (29). **Saved devices are not fed to Frigate yet** (Partial for the product). |
| Face detection (YuNet), crops, quality hints | Done (verified, dev PC) | Real model on the public-domain fixture; `inference/tests`. Accuracy on CP Plus footage: not measured. |
| Face tracking across frames | Missing | Frigate tracks objects (person); no face tracks of our own. |
| Face recognition, enrollment, consent records (milestone 2) | **Missing** | No code. Engine choice (Frigate built-in vs. own matcher) still to be evaluated per ADR-0001. |
| Age, expression, glasses, beard, mask estimates (milestone 3) | **Missing** | No models in the registry. Must be shown as fallible estimates only. |
| Event history (milestone 3), Frigate MQTT events | **Missing** | No MQTT broker in compose, no event tables. |
| Alerts and alarm output (milestone 4) | **Missing** | No code; must be verified on the real hardware first. |
| Database migrations | Done (verified) | Alembic baseline; `test_migrations.py` (24). |
| Backup, verify, restore, interruption handling, recovery copies | Done (verified: Windows + Linux/WSL) | `dbtools.py`; `test_dbtools.py` (79); 41-check drill. Power loss and real signals: not shown. |
| Configuration and secrets | Done (tests only) | pydantic settings; `.env` gitignored; per-service env lists in compose; `test_deploy_config.py`. |
| Health checks | Partial | api (compose) and inference (Dockerfile) have health checks; none is defined for frigate or web in this repository (whether their images ship one is unchecked). |
| Logging | Partial | Plain stdout logging; no rotation config beyond Docker's default. |
| Start after reboot | Done (docs only) | `restart: unless-stopped` + systemd unit; Blocked: not run on the host. |
| Desktop launcher | Not in scope | This product is a web app served by Docker; there is no desktop launcher in this repository. |
| Packaging: Docker images, model download at build | Done (static checks only) | Dockerfiles, pinned `constraints.txt`; never built (no Docker here). |
| Static analysis / CI | Missing | No linter, type checker for Python, or CI workflow. `tsc` runs in the frontend build. |

## Backlog (ranked)
| # | Priority | Task | Acceptance criteria | Files | Tests |
|---|---|---|---|---|---|
| 1 | Data loss / security | **Done 2026-10-29 (`9fe5078`):** disk errors while saving captures | Failed capture instead of 500; no storage path in API responses; no `.part` left; leftovers removed at startup; never stuck "pending" | `media.py`, `captures.py`, `main.py` | 4 new in `test_cameras_and_captures.py` |
| 2 | Data loss | **Done 2026-10-29 (`effd703`):** delete captures (API + UI button). Follow-up: also delete the clip's export in Frigate (`DELETE /api/export/{id}`, to be checked against the 0.18.2 source) | `DELETE /api/captures/{id}` (auth) removes the row and file; 404 for unknown; the gallery offers delete with confirmation; deleting a pending clip is refused | `routes/captures.py`, `media.py`, `frontend/src/components/Captures.tsx`, `api.ts` | backend route tests; vitest for the button |
| 3 | Data loss | **Done 2026-10-29 (uncommitted):** capture retention and a free-space guard | Configurable `CAPTURE_RETENTION_DAYS` (default off) and `MIN_FREE_DISK_MB`; captures refused with a clear error below the free-space floor so `app.db` keeps room | `config.py`, `captures.py`, `main.py`, `.env.example`, docs | unit tests with a fake disk-usage function |
| 4 | Security (low) | Shared secret between api and inference | `/v1/detect` refuses requests without the token; token generated at install, never logged | `inference/main.py`, `inference_client.py`, compose, docs | both services' tests |
| 5 | Reliability | Health checks for frigate and web; `depends_on: condition: service_healthy` where useful | `docker compose ps` shows health for all services | `deploy/compose.yaml`, `test_deploy_config.py` | static test; host check Blocked |
| 6 | Release | CI: backend, inference, frontend tests and build on push; add `ruff` | Workflow green on the branch | `.github/workflows/ci.yml`, `pyproject.toml` | the workflow itself |
| 7 | UX (low) | Move the side effect out of the `setUser` updater in `App.tsx` | Session-expired notice shown once under StrictMode | `App.tsx` | `session.test.tsx` |
| 8 | Feature (M1 gap) | Feed saved devices to Frigate | A saved, tested device appears as a Frigate camera without editing `.env` | `devices.py`, Frigate config generation, docs | unit tests; Blocked for the real NVR |
| 9 | Feature (M2) | Recognition engine evaluation, then enrollment with consent, matching (`AUTHORIZED`/`UNRECOGNIZED`/`INCONCLUSIVE`/`SYSTEM_ERROR`) | Per ADR-0001; needs a decision and test footage | new modules, migrations | large; plan separately |
| 10 | Feature (M3/M4) | Events with MQTT, attribute estimates, alerts and alarm output | Per ARCHITECTURE milestones; alarm only after hardware verification | new | plan separately |

## Hardware- and host-dependent checks (Blocked)
- Build all images; `docker compose config` and `up` on Ubuntu; `doctor.sh`; `run-integration-tests.sh`.
- Frigate accepts the config; test stream; live view, snapshot and clip through the real stack.
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
