# Verification record

Status labels:
- **Verified**: tested for real; the line names where and when.
- **Mock-only**: tested only against a simulated Frigate.
- **Unverified (hardware)**: still needs the Linux host, the GPU or the CP Plus devices.

Results are never upgraded without a recorded test.

## Milestone 1, 2026-10-22, development PC (Windows 11, Python 3.12.10, Node 24)
| Item | Status | Evidence |
|---|---|---|
| New repo is empty, workspace is a fresh clone, only remote is `rounak1434/Face_detection` | Verified | `git ls-remote` returned no refs. Clone shows only `origin` and no commits. Repo-local identity is set. |
| No old-project naming in any file | Verified | Case-insensitive `git grep` over all files returned no matches. |
| Backend unit tests (auth, lockout, cookie flags, camera list hides stream URLs, unknown-camera rejection, snapshot store/serve, clip export flow and failure paths, clip length limits, media path safety, RTSP credential masking, ffprobe parsing, websocket Origin and auth checks) | Verified (Mock-only Frigate) | `pytest`: **37 passed** |
| Frontend unit tests (login gate, camera tile snapshot, captures failure display, codec negotiation) | Verified | `vitest`: **4 passed** |
| Frontend production build | Verified | `tsc -b && vite build` succeeded (151 kB JS) |
| UI smoke test: sign-in, dashboard, "Engine unreachable" state, phone-width layout | Verified (no Frigate) | API + Vite dev server run locally with Frigate deliberately unreachable; checked in the browser |
| Docker images build; compose stack starts | Unverified (needs Linux host) | No Docker on the development PC |
| Pinned image tags exist (`frigate:0.18.2-tensorrt`, `mediamtx:1.9.3-ffmpeg`, `caddy:2.8.4-alpine`, `node:20.18-alpine`) | Unverified | Confirm on first `docker compose pull` |
| Frigate endpoint paths used by the API (`/api/version`, `/api/config`, `/api/<cam>/latest.jpg`, `POST /api/export/...`, `/api/exports/<id>`, `/exports/<file>`, `/live/mse/api/ws`) and the `frigate_token` login cookie name | Unverified | Compare with `/api/docs` on the running 0.18.2 instance |
| Frigate config file accepted by 0.18.2 | Unverified | Check Frigate logs on first start |
| Live MSE view end-to-end, including go2rtc message format | Unverified | Needs the running stack |
| Snapshot and 10 s clip against real Frigate recordings | Unverified | Run `pytest -m integration` (SETUP.md §4) |
| Synthetic RTSP test stream | Unverified | `ai-surveillance probe rtsp://test-stream:8554/test` |
| CP Plus NVR RTSP main/sub stream | Unverified (hardware) | Exact NVR/camera models not yet recorded; probe per SETUP.md §5 |
| GTX 1650 detection (ONNX, YOLOv9-s 320) | Unverified (hardware) | No benchmark in Frigate docs; measure per SETUP.md §6 |
| Start on boot / power recovery | Unverified (hardware) | Reboot test per SETUP.md §7 |
| Alarm control through the CP Plus NVR | Not started | Not assumed. An RTSP stream does not imply alarm-output control. To be checked against the exact model's documentation and on the device. |

## To fill in on the Linux host
```
Host OS / kernel:
NVIDIA driver / CUDA (nvidia-smi):
docker / compose versions:
Frigate /api/version:
NVR model:            firmware:
Camera model:         firmware:
Probe (main stream, masked):
Probe (sub stream, masked):
Integration tests (pytest -m integration):
Detector inference time (GPU):
Reboot test:
```
