# Troubleshooting

Start with the read-only check:
```bash
cd /opt/ai-surveillance/deploy && bash doctor.sh
```

## The header says "Engine unreachable"
The api could not get `/api/version` from Frigate. Check these in order:
1. **Is Frigate running?**
   ```bash
   docker compose ps frigate
   ```
   If it shows `restarting` or `exited`, run `docker compose logs --tail 100 frigate`. A config error is printed at startup with the offending key, for example `record -> retain: Extra inputs are not permitted`.
2. **Did compose start at all?** A missing `FRIGATE_CAMERA_*` variable stops compose with "set FRIGATE_CAMERA_MAIN_URL in .env".
3. **Can the api reach it?**
   ```bash
   docker compose exec api python -c "import urllib.request; print(urllib.request.urlopen('http://frigate:5000/api/version').read())"
   ```
   - "Name or service not known": the containers are not on the same network. Recreate them with `docker compose up -d`.
   - "Connection refused": Frigate is still starting, so wait, or it crashed (see step 1).
4. **Changed `FRIGATE_URL` to port 8971?** Then also set `FRIGATE_USERNAME`, `FRIGATE_PASSWORD` and `FRIGATE_VERIFY_TLS=false`. A login failure appears in `docker compose logs api`.

## The camera tile says "Still frames" instead of "Live"
Hover over the badge to see the reason.
- `browser lacks MSE`: iPhone Safari before iOS 17.1 has no MediaSource. Still frames are the intended fallback.
- `unsupported stream format`: the browser cannot play the codec. Firefox plays only H.264 over MSE. Switch the NVR sub stream to H.264, or reorder `live.streams` in `frigate/config.yml`.
- `live connection closed`: check `docker compose logs api` for "Live stream ... unavailable". In Frigate's UI (127.0.0.1:8971), check that the go2rtc stream exists.

## The picture is a grey "camera error" image, or snapshots fail with "Camera is offline"
Frigate is receiving no frames from the camera. Frigate still serves a placeholder image in that case, which is why the api checks `camera_fps` and refuses to save a snapshot. Common causes:
- **Wrong URL or credentials.** Probe the URL (SETUP.md §6) and look for `401 Unauthorized` or `404 Not Found`.
- **Too many RTSP sessions** on the device. Close other viewers such as a VMS app or VLC.
- **Unencoded special characters** in the password inside `.env` (`@` → `%40`).
- **Test stream not running.** Check `docker compose --profile test-stream ps test-stream`.

## A clip stays "pending", then becomes "failed"
| Error text | Meaning |
|---|---|
| `No recordings found for time range` | Recording has not run long enough, or `record.enabled` is off. Wait a minute after start. |
| `Export queue is full` | Several exports are already running. Try again shortly. |
| `Frigate export failed: ...` | Frigate's own reason. See `docker compose logs frigate`. |
| `Timed out waiting for Frigate` | The export is slow. Raise `CLIP_EXPORT_TIMEOUT_SECONDS`, or check disk I/O. |
| `Export download returned HTTP 4xx` | The file path differs in this Frigate version. Compare with docs/FRIGATE-API.md. |
| `Interrupted by a service restart` | The api restarted during the export. Request the clip again. |

## Sign-in problems
- **"Too many failed attempts"**: wait 5 minutes.
- **Forgotten password**: `docker compose exec api ai-surveillance create-admin <username>` resets it.
- **Signed in but immediately signed out**: you are on plain `http://`. The session cookie is HTTPS-only. Use `https://`, or for local development without Caddy set `COOKIE_SECURE=false`.
- **The browser shows a certificate warning**: install Caddy's root certificate (SETUP.md §4).

## The website does not load
- Check `docker compose ps web` and `docker compose logs --tail 50 web`.
- Caddy answers only for `SITE_ADDRESS`. Opening the site by a different name or IP gives a TLS error, so use the configured name.
- If the firewall is on: `sudo ufw allow 80,443/tcp`.

## GPU not used
- Run `docker run --rm --gpus all ubuntu nvidia-smi`. If it fails, the NVIDIA Container Toolkit is missing.
- Check that you started with `-f compose.nvidia.yaml` and that `deploy/frigate/model_cache/yolo.onnx` exists.
