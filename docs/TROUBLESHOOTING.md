# Troubleshooting

Start with the read-only check:
```bash
cd ~/ai-surveillance/deploy && bash doctor.sh
```

## Docker and startup errors
| Symptom | Cause and fix |
|---|---|
| `permission denied while trying to connect to the Docker daemon socket` | Your user is not in the `docker` group yet. Run `sudo usermod -aG docker $USER`, then log out and back in (or run `newgrp docker`). |
| `docker: 'compose' is not a docker command` | The Compose plugin is missing. Run `sudo apt-get install docker-compose-plugin` (SETUP.md §1). The old `docker-compose` v1 is not supported. |
| `required variable FRIGATE_CAMERA_MAIN_URL is missing a value` | `deploy/.env` is missing. Run `cp .env.example .env` in `deploy/`. |
| `Bind for 0.0.0.0:80 failed: port is already allocated` (or 443) | Another web server is running. Find it with `sudo ss -ltnp 'sport = :80'`, then stop it (e.g. `sudo systemctl stop apache2` or `nginx`). |
| `Bind for 127.0.0.1:8554 failed` | Another RTSP server is running on the host. Stop it, or change the left-hand port of `test-stream` in `compose.yaml`. |
| `toomanyrequests` / `pull rate limit` while pulling | Docker Hub's anonymous limit. Wait and retry, or run `docker login`. |
| `manifest unknown` for an image | The pinned tag is not available from your network or registry. Report the exact message. |
| `npm ci` fails while building `web` | It needs internet access to registry.npmjs.org. Retry `docker compose build web`. |
| A container keeps restarting | Check `docker compose logs --tail 100 <service>`. For Frigate, see the next section. |
| `test-stream` restarting, or mediamtx config errors | Check `docker compose logs test-stream` and report the message. The config is `deploy/test-stream/mediamtx.yml`. |
| Nothing from `test-stream` in `docker compose ps` | `COMPOSE_PROFILES=test-stream` is missing from `.env`. Add it back, or use `docker compose --profile test-stream up -d`. |

## The api keeps restarting with "does not match the baseline"
The database was created before migrations existed and differs from what this version expects. **Nothing was changed.** The log lists the differing tables and columns:
```bash
docker compose logs api | grep -i baseline
```
Check it without starting the api:
```bash
docker compose run --rm --no-deps api ai-surveillance db status
```
Do not delete the database. Keep the logged differences and see [DATABASE.md](DATABASE.md).

## "AI unreachable", or the AI analysis page says the model is unavailable
1. **Is the service running?**
   ```bash
   docker compose ps inference
   ```
   Then read `docker compose logs --tail 80 inference`. The startup line `Model yunet: ready` means the model loaded.
2. **`Model yunet: missing` or `checksum_mismatch`.** The image was built without a verified model. Rebuild it:
   ```bash
   docker compose build --no-cache inference && docker compose up -d
   ```
   The build must reach github.com / media.githubusercontent.com, and it fails if the downloaded file doesn't match the pinned SHA-256.
3. **Build error mentioning `download.py` / "does not match the pinned checksum".** The file fetched from OpenCV Zoo differs from the pinned one. Do not bypass the check; report the message.
4. **Can the api reach it?**
   ```bash
   docker compose exec api python -c "import urllib.request; print(urllib.request.urlopen('http://inference:8100/v1/health').read())"
   ```
5. **`503 analysis_busy`**: two analyses were already running for 15 s. Wait and retry. Several people analysing at once, or a very slow network, can cause this.
6. **`inference` restarting with exit code 137** in `docker compose ps -a`: it hit its memory cap. Very large images or a future heavier model can do this. Raise `INFERENCE_MEM_LIMIT` in `.env` (e.g. `2g`), then run `docker compose up -d`.
7. **`413 too_large`** for a normal photo: the limit is 10 MB and 40 megapixels. Raise `ANALYSIS_MAX_IMAGE_BYTES` in `.env` if needed; Caddy also caps bodies at 12 MB.
8. **`415 unsupported_format`**: only JPEG, PNG and WebP are accepted. HEIC photos from iPhones must be converted first.

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
- `unsupported stream format`: the browser cannot play the codec. Firefox plays only H.264 over MSE. Switch the NVR sub stream to H.264, or reorder `live.streams` in the template `deploy/frigate/config.yml` and press **Apply to Frigate** (SETUP.md section 4a).
- `live connection closed`: check `docker compose logs api` for "Live stream ... unavailable". In Frigate's UI (127.0.0.1:8971), check that the go2rtc stream exists.

## The picture is a grey "camera error" image, or snapshots fail with "Camera is offline"
Frigate is receiving no frames from the camera. Frigate still serves a placeholder image in that case, which is why the api checks `camera_fps` and refuses to save a snapshot. Common causes:
- **Wrong URL or credentials.** Probe the URL (SETUP.md §9) and look for `401 Unauthorized` or `404 Not Found`.
- **Too many RTSP sessions** on the device. Close other viewers such as a VMS app or VLC.
- **Unencoded special characters** in the password inside `.env` (`@` → `%40`).
- **Test stream not running.** Check `docker compose ps test-stream` and `docker compose logs test-stream`.

## A clip stays "pending", then becomes "failed"
| Error text | Meaning |
|---|---|
| `No recordings found for time range` | Recording has not run long enough, or `record.enabled` is off. Wait a minute after start. |
| `Export queue is full` | Several exports are already running. Try again shortly. |
| `Frigate export failed: ...` | Frigate's own reason. See `docker compose logs frigate`. |
| `Timed out waiting for Frigate` | The export is slow. Raise `CLIP_EXPORT_TIMEOUT_SECONDS`, or check disk I/O. |
| `Export download returned HTTP 4xx` | The file path differs in this Frigate version. Compare with docs/FRIGATE-API.md. |
| `Interrupted by a service restart` | The api restarted during the export. Request the clip again. |

## Camera & NVR Setup statuses
See [CAMERA-SETUP.md](CAMERA-SETUP.md#statuses) for what each status means.
- **"Address Not Allowed"** for your NVR: its IP is outside `DEVICE_ALLOWED_NETWORKS`. Add its subnet in `.env`, then run `docker compose up -d`.
- **"Reachable, Stream Not Verified"**: ffprobe is missing where the api runs. That is expected on a Windows dev PC. Inside Docker, ffprobe is part of the api image.
- **"Stored credential cannot be decrypted: the credentials key changed or the stored value is damaged"**, **"Stored credential is damaged"** or **"…has an unknown format"** (connection test or Apply to Frigate, HTTP 409): the saved password cannot be read. The two causes cannot be told apart for certain (AES-GCM fails the same way), so:
  1. If the credentials key may have changed (a new `api-data` volume, a changed `CREDENTIALS_KEY`), restore the old `credentials.key` from your backup (SETUP.md section 6) and restart the api.
  2. Otherwise click **Edit** on the device, type the password again and save. That replaces only the stored password; nothing else is changed, and nothing is changed before you do it.
  Apply to Frigate stops before touching Frigate or its secret files while any switched-on, connected device has such a password. The api log names the device id and the reason, never the stored value.
- **The webcam doesn't start**: the browser needs https or localhost. Allow camera access in the browser's site settings.

## "Apply to Frigate" fails
How it works: SETUP.md section 4a and [ADR-0003](adr/0003-saved-devices-to-frigate.md). The message never contains a password.
- **"not set up: FRIGATE_CONFIG_TEMPLATE and FRIGATE_SECRETS_DIR are needed"**: the api runs without the Compose settings, for example on a dev PC. In Docker they are set by `compose.yaml`.
- **"Frigate refused the configuration"**: Frigate checked it and kept its previous configuration; the message is Frigate's reason. Usually the template has an error; fix it and apply again.
- **"Use in Frigate" switch is greyed out**: test the device first; only a **Connected** device can be switched on.
- **"…did not restart" or "…did not come back within 180 s", then "The previous configuration was restored"**: Frigate was slow or failed with the new configuration; nothing is lost. Check `docker compose logs frigate`. A slow machine can raise `FRIGATE_RESTART_TIMEOUT_SECONDS` for the api.
- **"Restoring the previous configuration also failed … The outcome is UNKNOWN"**: Frigate is down or did not answer, so nobody knows yet which configuration it will run. Check `docker compose ps -a` and `docker compose logs frigate`. The secret files for both configurations stay in place, so whichever configuration Frigate starts with can run. Once Frigate is back, look at its cameras (Live tab) before applying again. To go back by hand: `docker compose cp api:/data/frigate-config.previous.yml ./previous.yml`, then `sudo cp previous.yml storage/frigate-config/config.yml` and `docker compose restart frigate`. If you already applied again since, the copy from before that is `frigate-config.previous.yml.1`.
- **"…its stream address cannot be used"**: the saved record does not give a single-line `rtsp://` address (it was changed outside the setup page). Edit the device and save it again.
- **"An existing Frigate secret file holds an unusable value"** or **"… is not valid UTF-8"**: a file named `FRIGATE_DEV_…` in the secrets volume was changed by hand. Apply stopped before changing anything. Only the api should write that volume; remove the damaged file with `docker compose exec api rm /frigate-secrets/<name>`, then apply again.
- **Frigate's log says "Invalid substitution found"** at startup: go2rtc found a `{FRIGATE_*}` name with no secret file or variable. Apply keeps the secret files of the previous and the new configuration until it has finished, so this points to a hand-edited config or a removed volume: apply again, or start over from the template (SETUP.md section 4a).
- **"An apply to Frigate is already running"**: wait for it to finish (a few minutes at most).

## Face events, People and recognition
How it works: [FACE-EVENTS.md](FACE-EVENTS.md), [GALLERY.md](GALLERY.md), [RECOGNITION.md](RECOGNITION.md).
- **No face events appear**: they come only from Frigate **person** events that have ended. Check that Frigate detects people (its UI, or `docker compose logs frigate`) and that `FACE_EVENTS_ENABLED=true`. The api log says "Face events paused: …" with the reason (Frigate unreachable, detector unavailable, low disk) and "Face events resumed" when it recovers.
- **Many events say "No face detected"**: faces are too small or turned away in the frame. `FACE_FRAME_SOURCE=recording` (default) uses the main stream, which has larger faces than the snapshot.
- **"Embedder: Not ready (missing)" or "Switch recognition on" is greyed out**: the SFace model is not loaded in the inference container. Rebuild it (`docker compose build inference && docker compose up -d`); the build downloads and checks the model. `docker compose logs inference` shows each model's status.
- **"Recognition was not switched on: No active person has a usable reference image"**: add a reference image to an active person first. If images exist but show "Not yet embedded", the embedder could not process them; see the api log ("cannot be embedded").
- **A reference image is refused** ("No face", "More than one face", "not usable as a reference"): use a clear photo with exactly one face, at least 80 px, not touching the edge. The reasons are listed under the upload button.
- **"The reference image cannot be decrypted"** or **"Unreadable: N embeddings"**: the credentials key changed (a new `api-data` volume, a changed `CREDENTIALS_KEY`). Restore the previous key (DATABASE.md), or delete those images and enroll them again.
- **"Password is incorrect" when deleting a person or switching recognition**: it is your sign-in password. Five wrong tries lock both sign-in and these actions for 5 minutes.
- **Someone is "Inconclusive" or "Unrecognized" although enrolled**: add more reference images of them (different light, angle), and measure the thresholds at the door before changing `RECOGNITION_*` in `.env`.

## Sign-in problems
- **"Too many failed attempts"**: wait 5 minutes.
- **Forgotten password**: `docker compose exec api ai-surveillance create-admin <username>` resets it.
- **"Your session has expired. Please sign in again."**: the server answered 401. Causes: the session timed out (`SESSION_TTL_HOURS`), an admin password was reset with `create-admin`, or the database was restored. Sign in again. Server errors (5xx) and network problems do **not** sign you out; they show an error and, on first load, a Retry button.
- **Signed in but immediately signed out**: you are on plain `http://`. The session cookie is HTTPS-only. Use `https://`, or for local development without Caddy set `COOKIE_SECURE=false`.
- **The browser shows a certificate warning**: install Caddy's root certificate (SETUP.md §6).

## The website does not load
- Check `docker compose ps web` and `docker compose logs --tail 50 web`.
- Caddy answers only for `SITE_ADDRESS`. Opening the site by a different name or IP gives a TLS error such as `SSL_ERROR_INTERNAL_ERROR_ALERT`. Use exactly that name, or change `SITE_ADDRESS` and run `docker compose up -d`.
- **From another PC:** `SITE_ADDRESS` must be this machine's LAN IP or name, not `localhost`, and the firewall must allow it:
  ```bash
  sudo ufw allow 80,443/tcp
  ```
- **`curl: (60) SSL certificate problem`** is expected until Caddy's root certificate is trusted (SETUP.md §6). `doctor.sh` and the integration tests already account for it.
- **The browser shows "Not Secure" and the webcam preview is refused:** camera access requires a trusted HTTPS page or `localhost`.

## GPU not used
- Run `docker run --rm --gpus all ubuntu nvidia-smi`. If it fails, the NVIDIA Container Toolkit is missing.
- Check that you started with `-f compose.nvidia.yaml` and that `deploy/storage/frigate-config/model_cache/yolo.onnx` exists.
