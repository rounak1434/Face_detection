# Troubleshooting

## The header says "Engine unreachable"
- Check the Frigate container: `docker compose ps frigate` and `docker compose logs --tail 100 frigate`.
- A config error stops Frigate at startup, and the log names the bad key.
- Missing `FRIGATE_CAMERA_*` variables stop compose with "set FRIGATE_CAMERA_MAIN_URL in .env".

## The camera tile shows "Still frames" instead of "Live"
Hover over the badge to see the reason.
- `browser lacks MSE`: iPhone Safari before iOS 17.1 has no MediaSource. Still frames are the expected fallback there.
- `unsupported stream format`: the browser cannot decode the codec. H.265 (HEVC) needs a browser with HEVC support. Switch the camera or NVR sub stream to H.264.
- `live connection closed`: check `docker compose logs api` for "Live stream ... unavailable". Frigate's go2rtc may not have the stream, so open Frigate's UI (127.0.0.1:8971) to check.

## Black or frozen picture, or Frigate logs "No frames received"
- **Wrong URL or credentials.** Run the probe (SETUP.md §5) and look for `401 Unauthorized` or `404 Not Found` in its error.
- **Transport.** The probe and Frigate use TCP. Some devices only allow a limited number of RTSP sessions, so close other viewers (VMS app, VLC).
- **Special characters in the password** must be URL-encoded in `.env`.
- **Resolution mismatch.** `detect.width`/`height` must match the sub stream.

## Clip stays "pending", then "failed"
| Error text | Meaning |
|---|---|
| `No recordings ...` | Recording is off, or has not been running long enough. Check `record.enabled` and wait a minute after start. |
| `Timed out waiting for Frigate` | Export is slow. Raise `CLIP_EXPORT_TIMEOUT_SECONDS`, or check disk I/O. |
| `HTTP 404` on export endpoints | The endpoint path differs in this Frigate version. Compare with `/api/docs` on 127.0.0.1:8971 and record it in VERIFICATION.md. |
| `Interrupted by a service restart` | The api restarted during the export. Request the clip again. |

## Sign-in problems
- **"Too many failed attempts"**: wait 5 minutes (`LOGIN_LOCKOUT_SECONDS`).
- **Forgotten password**: `docker compose exec api ai-surveillance create-admin <username>` resets it.
- **Signing in over plain `http://` works in dev only** with `COOKIE_SECURE=false`. Production uses HTTPS via Caddy.

## GPU not used
- Run `docker run --rm --gpus all ubuntu nvidia-smi`. If it fails, the NVIDIA Container Toolkit is missing (SETUP.md §1).
- Check that you started with `-f compose.nvidia.yaml` and that the ONNX model path exists.
