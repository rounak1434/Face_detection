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
- A captures gallery with an image viewer and video player.
- An RTSP probe tool (`ai-surveillance probe`) that reports the codec, resolution and frame rate it actually decoded. Credentials are masked in its output.
- A synthetic RTSP test stream, for testing without camera hardware.

## Layout
| Path | What |
|---|---|
| `backend/` | API service (Python 3.12, FastAPI). Package `ai_surveillance`. |
| `frontend/` | Web UI (React + TypeScript + Vite). |
| `deploy/` | Docker Compose stack: Frigate, api, web (Caddy HTTPS) and an optional test stream. Also holds the systemd unit. |
| `docs/` | Architecture, setup, troubleshooting, verification record and decision records (ADRs). |

## Quick start
- **Linux host, full stack:** follow [docs/SETUP.md](docs/SETUP.md).
- **Development without Docker** (any OS, for UI and API work against a Frigate instance you can reach):

```bash
cd backend && python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest                     # unit tests, no network needed
FRIGATE_URL=http://<frigate-host>:5000 COOKIE_SECURE=false .venv/bin/ai-surveillance create-admin admin
FRIGATE_URL=http://<frigate-host>:5000 COOKIE_SECURE=false .venv/bin/uvicorn --factory ai_surveillance.main:app --port 8000
cd ../frontend && npm install && npm run dev     # http://localhost:5173
```

On Windows, use `.venv\Scripts\` instead of `.venv/bin/`.

## Renaming
The product name shown to users comes from `APP_DISPLAY_NAME` in `deploy/.env`. Code identifiers are deliberately generic, so you can rename the product without editing code.
