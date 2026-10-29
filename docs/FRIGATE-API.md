# Frigate API contract (Frigate 0.18.2)

This page lists every Frigate interface the api service uses. Each entry was checked on 2026-10-22 against the **source code of the `v0.18.2` tag** (github.com/blakeblackshear/frigate). "Source-checked" means the code says so. It does **not** mean it was observed on a running instance: that is the job of `tests/integration`, recorded in [VERIFICATION.md](VERIFICATION.md).

FastAPI routes are defined without `/api`; Frigate's nginx maps `/api/` to them. All paths below are as called through nginx.

| Interface | Used by | Source-checked behaviour | Source |
|---|---|---|---|
| `GET /api/version` | health | Public, plain text | `frigate/api/app.py` |
| `GET /api/config` | camera list, live stream choice | Any authenticated user. ffmpeg input credentials are masked. `cameras.<name>.live.streams` maps labels to go2rtc stream names; Frigate's UI uses the first entry. | `frigate/api/app.py`, `frigate/config/camera/live.py` |
| `GET /api/stats` | snapshot liveness check | `cameras.<name>.camera_fps`, plus `detectors.<name>.inference_speed` (ms) | `frigate/stats/util.py` |
| `GET /api/<camera>/latest.jpg?height=N` | live fallback, snapshot | The query parameter is **`height`**; there is no `h` alias. When the camera is offline it still answers **200**: a recent preview with header `X-Frigate-Offline: true`, or a generic error image **without** that header. 404 for an unknown camera. | `frigate/api/media.py`, `frigate/api/defs/query/media_query_parameters.py` |
| `POST /api/export/<camera>/start/<ts>/end/<ts>` body `{"source":"recordings","name":...}` | clip | Returns **202** `{"success":true,"export_id":...,"status":"queued"}`. 400 "No recordings found for time range"; 404 invalid camera; 503 queue full. | `frigate/api/export.py`, `frigate/api/defs/request/export_recordings_body.py` |
| `GET /api/jobs/export/<id>` | clip | Job status `queued` → `running` → `success` / `failed` (`error_message`) / `cancelled`. The job is **dropped from memory ~5 s after it finishes**, then 404. | `frigate/api/export.py`, `frigate/jobs/export.py` |
| `GET /api/exports/<id>` | clip | Export row: `video_path`, `in_progress`. 404 until the worker creates it. | `frigate/api/export.py` |
| `POST /api/exports/delete` body `{"ids": ["<id>"]}` | clip cleanup | There is no single-export DELETE. Deletes the video, thumbnail and row; **200** `{"success": true, ...}`; 404 "One or more exports not found." (treated as already deleted); **400** "Can not delete in-progress export." while ffmpeg has the file open (nothing deleted). Requires the **admin** role: the anonymous user on port 5000 is admin; on 8971 `FRIGATE_USERNAME` must be an admin, otherwise the export is kept and a warning logged. | `frigate/api/export.py` (`bulk_delete_exports`), `frigate/api/defs/request/export_bulk_body.py` (`ids: conlist(constr(min_length=1), min_length=1)`); checked 2026-10-29 at tag v0.18.2 |
| `GET /exports/<file>.mp4` | clip download | nginx serves files from `/media/frigate/exports/`, behind its auth check | `docker/main/rootfs/usr/local/nginx/conf/nginx.conf` |
| `WS /live/mse/api/ws?src=<go2rtc stream>` | live view | nginx proxies to go2rtc `/api/ws`, GET only, behind its auth check | `nginx.conf` |
| `POST /api/login` body `{"user","password"}` | only when `FRIGATE_URL` is port 8971 | 200 with empty body; JWT in cookie `frigate_token` (`auth.cookie_name` default). Later requests may send `Authorization: Bearer <jwt>`. 401 "Login failed". | `frigate/api/auth.py`, `frigate/config/auth.py` |

## Ports and authentication
- **5000 (internal).** Every request is treated as user `anonymous` with role `admin`. The api uses this port over the private Docker network. It must never be published to the LAN.
- **8971 (external).** Requires login, and roles are enforced. TLS uses Frigate's own certificate, so with `FRIGATE_URL=https://frigate:8971` set `FRIGATE_VERIFY_TLS=false`, or install the certificate.

## Config schema points that changed in recent Frigate versions
- Recording retention is `record.continuous.days` and `record.motion.days`. The old `record.retain` key does not exist in 0.18, and the first draft of our config used it: that was a bug, now fixed.
- `snapshots.retain` has `default` and `objects` only.
- Stream URLs in `go2rtc.streams` use Frigate's `{FRIGATE_*}` environment substitution. go2rtc's own `${VAR}` syntax is disabled there.

## Not yet confirmed
Each of these is confirmed only by the integration tests on a running instance:
- The exact go2rtc MSE message exchange: the client sends `{"type":"mse","value":"<codecs>"}` and the server replies `{"type":"mse","value":"video/mp4; codecs=..."}` followed by binary fMP4.
- That `/exports/<file>` downloads work on port 5000 without credentials.
- The `ExportJob` id format, and whether it always equals the export row id. The source passes `job.id` as the export id, but this is unobserved.
