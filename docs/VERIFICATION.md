# Verification record

Status labels:
- **Verified**: tested for real; the line says where and when.
- **Source-checked**: matches the Frigate v0.18.2 source code, but not yet observed on a running instance.
- **Mock-only**: tested only against a simulated Frigate.
- **Unverified (host)**: needs the Ubuntu host. **Unverified (hardware)**: needs the GPU or the CP Plus devices.

Results are never upgraded without a recorded test.

## Known limitation of the development PC
This Windows PC has no Docker and no FFmpeg, and WSL is broken. **Frigate, the test stream, the Docker images and the compose stack have never run here.** Everything below that needs them is unverified until the steps in [SETUP.md](SETUP.md) §1–5 are run on Ubuntu.

### WSL2 check on the development PC (read-only, 2026-10-22)
| Check | Result |
|---|---|
| `wsl --status` / `wsl --version` | `Class not registered`, error `Wsl/CallMsi/Install/REGDB_E_CLASSNOTREG` |
| `wsl -l -v` | Hung, apparently trying to start the WSL installer. Stopped after 2 minutes; no `wsl` or `msiexec` process was left running. |
| Windows features | `Microsoft-Windows-Subsystem-Linux` **Enabled**; `VirtualMachinePlatform` **Disabled**; `HypervisorPlatform` Disabled |
| WSL store package | `MicrosoftCorporationII.WindowsSubsystemForLinux` 2.7.3.0 installed, but its service (`WSLService`) is not registered |
| Hypervisor | `HypervisorPresent=True` |

**Conclusion:** WSL2 is **not usable** as the system stands: VirtualMachinePlatform is off and the WSL package registration is broken. A repair needs administrator rights. One possible repair is to enable VirtualMachinePlatform, reboot, and run `wsl --update` or reinstall WSL from the Microsoft Store. **Not attempted.** It needs your approval and a reboot, and the Ubuntu PC is the intended target anyway.

## Camera & NVR Setup, 2026-10-22
| Item | Status | Evidence |
|---|---|---|
| Device CRUD, encrypted credentials, password never returned or logged, SSRF guard, input validation, status classification, cooldown | Mock-only network; rules verified | `tests/unit/test_devices.py` (60 tests). TCP, DNS and ffprobe are replaced by fakes. |
| Setup page: three separated source types, status labels, form, edit keeps the password, remove, webcam start/stop/denied | Verified (jsdom) | `src/test/setup-page.test.tsx` (11 tests) |
| Smoke test in a real browser, Windows, no Docker | Verified | Synthetic source → **Unreachable** ("Host 'test-stream' could not be resolved"), as expected with no stack. A link-local host was refused with a clear message. A saved NVR showed **Not Tested** and a masked URL. The raw `/api/devices` response and the server log did not contain the test password. |
| Webcam preview in a real browser | Not exercised | Needs your camera-permission click. jsdom tests cover the logic only. |
| Synthetic source → **Connected** through the real stack | Unverified (host) | `test_synthetic_source_connection_test` in `tests/integration` |
| Real CP Plus device → **Connected** | Unverified (hardware) | Needs the device on the LAN, plus ffprobe (Docker api image, or FFmpeg on Windows) |
| Saved devices used by the engine | Not implemented | Live view still uses `deploy/.env` streams |

## Validation review, 2026-10-22 (before milestone 2)
Defects found by checking our code and config against the Frigate v0.18.2 source. The mocked tests had passed with every one of these:

| # | Defect | Effect if not fixed | Fix | Test |
|---|---|---|---|---|
| 1 | Frigate config used `record.retain`, which does not exist in 0.18 (now `record.continuous.days`) | Frigate would likely refuse to start | Config uses `continuous.days: 3` | Unverified (host): Frigate startup |
| 2 | The export was treated as finished on the POST response. Frigate 0.18 answers **202 queued**, and the export row only exists once the worker runs. | Clips fail with 404 while queued | Poll `/api/jobs/export/<id>`, then fall back to `/api/exports/<id>` once the job is forgotten | `test_clip_export_flow`, `..._after_frigate_forgets_finished_job`, `..._job_failure_reason_is_kept`, `..._times_out` |
| 3 | The export body sent `playback`, which is not a field. The real field is `source: "recordings"`. | Ignored or rejected | Body is `{"source":"recordings","name":...}` | `test_clip_export_flow` asserts the body |
| 4 | `latest.jpg` was called with `h=`. Frigate's parameter is `height`. | Full-size frames sent for the live fallback | Uses `height` | `test_latest_frame_uses_frigate_height_param` |
| 5 | For an offline camera Frigate returns **200** with a placeholder image | A snapshot would save an error image as evidence | Check `camera_fps` from `/api/stats` and `X-Frigate-Offline` | `test_snapshot_refused_when_camera_receives_no_frames`, `..._for_offline_fallback_frame` |
| 6 | Frigate error messages (e.g. "No recordings found for time range") were dropped | Operators saw only "HTTP 400" | The message is carried into the capture error | `test_clip_rejected_when_no_recordings`, `test_frigate_error_message_is_surfaced` |
| 7 | The live view assumed the go2rtc stream had the camera's name | Main 4MP / H.265 stream, or the wrong stream | Use the first `live.streams` entry, as Frigate's own UI does; config lists the sub stream first | `test_live_stream_choice` |
| 8 | `created_at` was sent without a timezone (SQLite drops it) | Times in the UI off by the UTC offset (5 h 30 min in IST) | Sent as UTC with offset | `test_snapshot_is_stored_and_served` |
| 9 | The api data dir was a bind mount, created root-owned | The non-root api could not write its database | Named volume `api-data` | Unverified (host) |
| 10 | The `-tensorrt` image was the default | Unnecessary on a host without a GPU set up | Standard image by default; `-tensorrt` in `compose.nvidia.yaml` | Unverified (host) |
| 11 | The record preset was forced to `audio-copy` | CP Plus G.711 audio cannot go into MP4 | Frigate's default record preset | Unverified (host) |
| 12 | A password reset left old sessions valid | A stolen session survives a reset | Reset revokes all of that user's sessions | `test_password_reset_signs_out_everywhere` |
| 13 | Expired sessions and old lockout entries were never removed | Slow growth in the database and memory | Purged at startup; entries dropped when empty | `test_throttle_forgets_old_addresses` |
| 14 | CSP `connect-src 'self'` may block `wss:` on older Safari | Live view blocked by CSP | `connect-src 'self' wss://{host}` | Unverified (host) |
| 15 | An unknown key `rtspTransports` in the test-stream config could be rejected by mediamtx 1.9.3 | Test stream fails to start | Removed; uses the defaults | Unverified (host) |
| 16 | The integration guide used `https://localhost` even when `SITE_ADDRESS` is a LAN name | Caddy does not answer, so the tests fail | Docs require `IT_BASE_URL=https://<SITE_ADDRESS>` | — |

Consistency of names and ports across compose, Dockerfiles, Frigate config and the api (checked by reading them side by side):
- **Service names:** `frigate`, `api`, `web` and `test-stream` are used the same way in compose, `.env.example`, the Caddyfile and `FRIGATE_URL`.
- **Ports:** 5000 internal only, 8971 on loopback, 8554 on loopback.
- **Streams:** go2rtc streams `entrance` and `entrance_sub` match the camera name, as restream requires.
- **Stored data:** the api writes to `/data`, which is the volume.

## Development PC results, 2026-10-22 (Windows 11, Python 3.12.10, Node 24)
| Item | Status | Evidence |
|---|---|---|
| Fresh clone, only remote is `rounak1434/Face_detection`; no old-project naming | Verified | git checks; case-insensitive grep found nothing |
| Backend unit tests (`tests/unit`, mocked Frigate) | Mock-only | **57 passed** |
| Backend integration tests (`tests/integration`) | Collected and skipped (no stack) | 6 skipped without `IT_BASE_URL` |
| Frontend unit tests | Verified | vitest: **4 passed** |
| Frontend production build | Verified | `tsc -b && vite build` |
| UI smoke test: sign-in, "Engine unreachable" state, phone width | Verified (no Frigate) | Browser, local dev servers |
| Pinned image tags exist on their registries | Verified (registry) | Docker Hub: `bluenviron/mediamtx:1.9.3-ffmpeg`, `caddy:2.8.4-alpine`, `node:20.18-alpine`; GitHub release lists `0.18.2` and `0.18.2-tensorrt` |
| Frigate endpoints, auth and ports | Source-checked | [FRIGATE-API.md](FRIGATE-API.md) |
| Docker images build; stack starts; Frigate accepts config | Unverified (host) | SETUP.md §3 |
| Test stream publishes and probes | Unverified (host) | `docker compose exec api ai-surveillance probe rtsp://test-stream:8554/test` |
| Live view, snapshot, clip through the real stack | Unverified (host) | `pytest -m integration` (SETUP.md §5) |
| CP Plus NVR main/sub stream | Unverified (hardware) | Models not yet recorded; SETUP.md §6 |
| GTX 1650 detection | Unverified (hardware) | SETUP.md §7 |
| Start on boot / power recovery | Unverified (host) | SETUP.md §8 |
| Alarm control through the CP Plus NVR | Not started | Not assumed. An RTSP stream does not imply alarm-output control. |

## To fill in on the Ubuntu host
```
Date:
Host OS / kernel (uname -a, lsb_release -a):
docker / compose versions:
bash doctor.sh output:
Frigate /api/version:
Probe of rtsp://test-stream:8554/test:
pytest -m integration -v (full output):
NVR model / firmware:
Camera model / firmware:
Probe (main stream, masked):
Probe (sub stream, masked):
nvidia-smi / detector inference_speed:
Reboot test:
```
