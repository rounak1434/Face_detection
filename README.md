# AI Surveillance Web Application

A locally hosted web application for monitoring your own IP cameras. It shows live video in the browser and saves snapshots and short clips on request. Later milestones add face recognition against an administrator-managed registry, event history and verified alarm output.

All processing and evidence storage stay on your own hardware. Nothing is sent to cloud services.

> **Status: milestone 1 (foundation).** See [docs/VERIFICATION.md](docs/VERIFICATION.md) for what has been tested and where. It also lists what is still unverified on real hardware.

## What milestone 1 does
- Administrator sign-in, with argon2id password hashing and an httpOnly session cookie.
- A camera list, read from the processing engine ([Frigate](https://github.com/blakeblackshear/frigate), MIT-licensed).
- Live view in the browser (MSE over a websocket), with a still-frame fallback.
- **Snapshot** button: saves the current frame.
- **Save 10 s clip** button: exports the last 10 seconds from the recordings.
- A captures gallery with an image viewer and video player. Captures can be deleted, after a confirmation, to free disk space.
- An RTSP probe tool (`ai-surveillance probe`) that reports the codec, resolution and frame rate it actually decoded. Credentials are masked in its output.
- A synthetic RTSP test stream, for testing without camera hardware.
- An **AI analysis** page that detects faces in an uploaded image with a real model (YuNet, MIT licence). It shows boxes, 5 keypoints and face crops, and stores nothing. See [docs/AI-ANALYSIS.md](docs/AI-ANALYSIS.md).
- A **Camera & NVR Setup** page. It has three separate kinds of source:
  - **Browser webcam:** local preview only.
  - **Synthetic RTSP test source.**
  - **Real IP cameras and NVRs:** add, edit, remove and test connection. Credentials are encrypted, and tests are limited to the private LAN.

  See [docs/CAMERA-SETUP.md](docs/CAMERA-SETUP.md).

## Layout
| Path | What |
|---|---|
| `backend/` | API service (Python 3.12, FastAPI). Package `ai_surveillance`. |
| `inference/` | Internal face-analysis service (FastAPI, OpenCV). Package `inference_service`. |
| `frontend/` | Web UI (React + TypeScript + Vite). |
| `deploy/` | Docker Compose stack: Frigate, api, web (Caddy HTTPS) and an optional test stream. Also holds the systemd unit. |
| `docs/` | Architecture, setup, troubleshooting, verification record, decision records (ADRs), the Frigate API contract (`FRIGATE-API.md`), AI analysis (`AI-ANALYSIS.md`) and database migrations and backups (`DATABASE.md`). |

## Quick start
**Ubuntu 22.04/24.04 with Docker**, using the synthetic test stream. No camera is needed. Full guide: [docs/SETUP.md](docs/SETUP.md).
```bash
git clone -b feat/initial-platform https://github.com/rounak1434/Face_detection.git ~/ai-surveillance
cd ~/ai-surveillance/deploy && cp .env.example .env && chmod 600 .env
sed -i "s/^INFERENCE_API_TOKEN=.*/INFERENCE_API_TOKEN=$(openssl rand -hex 32)/" .env
docker compose up -d --build
docker compose exec api ai-surveillance create-admin admin
bash doctor.sh                  # health check
bash run-integration-tests.sh   # real-stack tests
```
Then open https://localhost/.

- **Linux host, full details:** [docs/SETUP.md](docs/SETUP.md).
- **Development without Docker** (any OS, for UI and API work against a Frigate instance you can reach):

```bash
cd backend && python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest                     # unit tests (tests/unit, mocked engine, no network)
.venv/bin/pytest -m integration      # real-stack tests (tests/integration); skipped unless IT_BASE_URL is set
FRIGATE_URL=http://<frigate-host>:5000 COOKIE_SECURE=false .venv/bin/ai-surveillance create-admin admin
FRIGATE_URL=http://<frigate-host>:5000 COOKIE_SECURE=false .venv/bin/uvicorn --factory ai_surveillance.main:app --port 8000
cd ../frontend && npm install && npm run dev     # http://localhost:5173
```

On Windows, use `.venv\Scripts\` instead of `.venv/bin/`.

## Renaming
The product name shown to users comes from `APP_DISPLAY_NAME` in `deploy/.env`. Code identifiers are deliberately generic, so you can rename the product without editing code.
