# Setup on Ubuntu

Starts the full stack on a clean Ubuntu machine with the **synthetic RTSP test stream**. No camera, NVR, GPU or WSL is needed.

Run these commands yourself. Steps marked **(system change)** install packages or change system configuration.

> **Status:** these steps have **not yet been run** on a Linux host. Docker, Frigate and the test stream have never run here (see [VERIFICATION.md](VERIFICATION.md)). Please report any step that fails.

## Supported host
| | |
|---|---|
| OS | Ubuntu **22.04 LTS** or **24.04 LTS**, 64-bit x86 (amd64). Other releases may work but are untested. |
| CPU / RAM | 4+ cores and 8 GB RAM for testing. The target AI PC has an i7 and 32 GB. |
| Disk | About 10 GB free for images and recordings. The test stream records about 3 days at most. |
| Network | Internet access for the first image download. |
| Needed software | Docker Engine with the Compose plugin, git, curl |
| **Not** needed | WSL, Docker Desktop, FFmpeg on the host, Python on the host, an NVIDIA GPU |

FFmpeg/ffprobe ship inside the test-stream, api and Frigate images. The integration tests run in a throwaway Python container, so the host's own Python version does not matter. Ubuntu 22.04's Python 3.10 is too old for the backend, which is why the tests don't use it.

## Quick start (copy and paste)
After Docker is installed (section 1):
```bash
git clone -b feat/initial-platform https://github.com/rounak1434/Face_detection.git ~/ai-surveillance
cd ~/ai-surveillance/deploy
cp .env.example .env && chmod 600 .env
sed -i "s/^INFERENCE_API_TOKEN=.*/INFERENCE_API_TOKEN=$(openssl rand -hex 32)/" .env   # api ↔ inference secret
docker compose up -d --build                          # first run downloads images: several minutes
docker compose ps                                     # frigate, api, inference, web, test-stream → running
docker compose exec api ai-surveillance create-admin admin
bash doctor.sh
```
Then open **https://localhost/** on the same machine, accept the certificate warning, and sign in. Details follow below.

## 1. Install Docker Engine and Compose (system change)
These commands follow Docker's official guide, https://docs.docker.com/engine/install/ubuntu/. Check that page if one fails.
```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl git
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | sudo tee /etc/apt/sources.list.d/docker.list
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo usermod -aG docker "$USER"
```
**Log out and back in** so the `docker` group applies. Or run `newgrp docker` in the current terminal. Then check:
```bash
docker version && docker compose version && docker run --rm hello-world
```
- If Ubuntu's own `docker.io` package or Docker Desktop is installed, remove it first, as the Docker guide describes.
- If the firewall (ufw) is on and other PCs should reach the site:
  ```bash
  sudo ufw allow 80,443/tcp
  ```
  Never open 5000, 8554 or 8971.

## 2. Get the code
```bash
git clone -b feat/initial-platform https://github.com/rounak1434/Face_detection.git ~/ai-surveillance
cd ~/ai-surveillance
git log --oneline -3          # newest commit first
```
Until it is merged, the work lives on the `feat/initial-platform` branch. In an existing clone, update it with:
```bash
git switch feat/initial-platform && git pull --ff-only
```

## 3. Configure (`deploy/.env`)
```bash
cd ~/ai-surveillance/deploy
cp .env.example .env
chmod 600 .env
sed -i "s/^INFERENCE_API_TOKEN=.*/INFERENCE_API_TOKEN=$(openssl rand -hex 32)/" .env
nano .env
```
The example works unchanged for a test on the same machine. It contains **no credentials**. The `sed` line generates this install's own `INFERENCE_API_TOKEN`: a shared secret that the api sends to the face-analysis service, so no other container on the internal network can use the detector. In an existing `.env`, add the line `INFERENCE_API_TOKEN=` first, run the `sed` command, then `docker compose up -d` (both containers are recreated with the new value). If it is left empty, everything still works, but the inference log warns that the detector is open to the internal network. If the two containers ever disagree, face analysis reports "inference unavailable … INFERENCE_API_TOKEN must be the same".

| Variable | Default | Change when |
|---|---|---|
| `COMPOSE_PROFILES` | `test-stream` | You switch to a real NVR (section 9): remove it. |
| `SITE_ADDRESS` | `localhost` | Other PCs should open the site. Use this machine's LAN IP or host name, for example `192.168.1.50`. |
| `TZ` | `Asia/Kolkata` | You are in another timezone. |
| `FRIGATE_CAMERA_MAIN_URL` / `_SUB_URL` | the test stream | You switch to a real NVR (section 9). |
| `DEVICE_ALLOWED_NETWORKS` | `192.168.0.0/16,10.0.0.0/8` | You narrow it to your camera subnet. |
| `CAPTURE_MIN_FREE_MB` | `1024` | Snapshots and clips share the data volume with the database. New captures are refused (HTTP 507, with the free space in the message) while less than this is free, so the database keeps room. `0` turns the check off (not recommended). |
| `CAPTURE_RETENTION_DAYS` | `0` (keep) | You want old captures deleted automatically: captures older than this many days are deleted at startup and hourly. Clips still being saved are never deleted. |

Never commit `deploy/.env`. It is gitignored.

## 4. Start, stop and update the stack
| Action | Command (run in `~/ai-surveillance/deploy`) |
|---|---|
| Start (and build) | `docker compose up -d --build` |
| Status | `docker compose ps` (frigate, api, inference, web, test-stream) |
| Logs | `docker compose logs -f frigate` (or `api`, `inference`, `web`, `test-stream`) |
| Stop, keeping all data | `docker compose down` |
| Restart one service | `docker compose restart api` |
| Update after `git pull` | Back up first ([DATABASE.md](DATABASE.md)), then `docker compose up -d --build`. The api migrates the database on start. |
| Database schema status | `docker compose exec api ai-surveillance db status` |
| Delete **all** data | `docker compose down -v && sudo rm -rf storage`. This deletes recordings, captures, accounts and the credentials key. |

The first start downloads the Frigate, mediamtx, Python, Node and Caddy images. It then builds the api, inference and web images. Building the inference image also downloads the YuNet face model (about 230 KB) from OpenCV Zoo and verifies its SHA-256. A mismatch stops the build. Frigate needs another minute or two to come up. To watch it:
```bash
docker compose logs -f frigate
```
Stop following with Ctrl+C.

With `COMPOSE_PROFILES=test-stream` in `.env`, every `docker compose` command includes the synthetic test stream. It is a moving test pattern, H.264 1280×720 at 15 fps, at `rtsp://test-stream:8554/test` inside the stack. On the host itself it is at `rtsp://127.0.0.1:8554/test`.

Detection runs on the **CPU** by default. The GPU is optional (section 8).

## 5. Create the administrator
```bash
docker compose exec api ai-surveillance create-admin admin
```
- It prompts twice for a password of at least 12 characters. The password is not echoed, not stored in shell history, and stored only as an argon2id hash.
- Running it again for the same name resets the password and signs that user out everywhere.

## 6. Open the website
- **On this machine:** https://localhost/
- **From another PC:** `https://<SITE_ADDRESS>/`. This needs `SITE_ADDRESS` set to that name or IP, and ports 80/443 allowed.

**The certificate.** Caddy issues the HTTPS certificate from its own local CA, so the first visit shows a browser warning. To trust it permanently:
1. Export the CA root certificate:
   ```bash
   docker compose cp web:/data/caddy/pki/authorities/local/root.crt ./caddy-root.crt
   ```
2. On Ubuntu, trust it system-wide:
   ```bash
   sudo cp caddy-root.crt /usr/local/share/ca-certificates/ && sudo update-ca-certificates
   ```
3. Firefox and Chrome use their own stores. Import `caddy-root.crt` in their certificate settings, under "Authorities".
4. On Windows, double-click the file and install it into "Trusted Root Certification Authorities".

**What you should see:**
1. **Live tab:** the camera **entrance** shows the moving test pattern, with a **Live** badge. "Still frames" means the live relay fell back; see TROUBLESHOOTING.md.
2. **Snapshot** saves a JPEG into Captures.
3. **Save 10 s clip**: wait at least one minute after start so recordings exist. The clip appears as "pending", then "ready".
4. **Camera & NVR Setup tab:** click **Add synthetic test source**, then **Test connection**. It should show **Connected (h264 1280×720)**.
5. **AI analysis tab:**
   - Models shows **yunet: Ready**.
   - Choose a photo you are allowed to use, then click **Detect faces**. You should see boxes, 5 keypoints per face and face crops, labelled "Real model inference".
   - Nothing you upload is stored. See [AI-ANALYSIS.md](AI-ANALYSIS.md).

**Back up the credentials key.** It encrypts the passwords stored for real devices:
```bash
docker compose cp api:/data/credentials.key ./credentials.key.backup && chmod 600 credentials.key.backup
```

**Frigate's own UI**, for administrators only, is at https://127.0.0.1:8971 on this machine only. From another PC, use `ssh -L 8971:127.0.0.1:8971 <user>@<host>`. Frigate prints its first admin password once:
```bash
docker compose logs frigate | grep -iA3 password
```

## 7. Diagnostics and integration tests
**Health check.** It is read-only and changes nothing:
```bash
bash doctor.sh
```

**Integration tests.** These run against the real stack, with no mocks. They need the stack running for at least a minute and the admin you created:
```bash
bash run-integration-tests.sh          # prompts for the web admin password
IT_FACE_IMAGE=~/photo.jpg bash run-integration-tests.sh   # also checks real face detection
```
- The script runs `backend/tests/integration` in a throwaway `python:3.12-slim` container.
- It checks:
  - engine reachable and version 0.18
  - camera listed
  - real JPEG frame
  - snapshot
  - live websocket video
  - synthetic source **Connected**
  - 10 s clip downloadable as MP4
  - face model ready, no-face image handled, and non-images rejected
  - a real face, when `IT_FACE_IMAGE` points to a photo with one visible face that you are allowed to use
- Please send the full output. It is what turns the "Unverified" lines in VERIFICATION.md into "Verified".

**Unit tests** (mocked engine) need no stack:
```bash
docker run --rm -v "$PWD/../backend:/src:ro" python:3.12-slim sh -c 'cp -r /src /tmp/app && cd /tmp/app && pip install -q ".[dev]" && python -m pytest -q'
```

## 8. Optional: NVIDIA GPU for detection (system change)
Everything works without this, on the CPU. Do it only after the CPU setup works.
1. Install the NVIDIA driver (`sudo ubuntu-drivers install`), reboot, and check it:
   ```bash
   nvidia-smi
   ```
2. Install the NVIDIA Container Toolkit: https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html. Check it:
   ```bash
   docker run --rm --gpus all ubuntu nvidia-smi
   ```
3. Export a YOLOv9-s 320×320 ONNX model as described at https://docs.frigate.video/configuration/object_detectors, and save it as `deploy/frigate/model_cache/yolo.onnx`.
4. Uncomment the `detectors`/`model` block in `deploy/frigate/config.yml`.
5. Start with the GPU override, which also switches to the `-tensorrt` image:
   ```bash
   docker compose -f compose.yaml -f compose.nvidia.yaml up -d
   ```
6. Record `detectors.*.inference_speed` from Frigate's `/api/stats` in VERIFICATION.md.

**To fall back to the CPU:** re-comment the block, then run `docker compose up -d` without the override.

## 9. Later: connect the CP Plus NVR
1. **Find the exact models.** Read the NVR and camera model numbers from the device labels or the web UI, and record them in VERIFICATION.md. Do not assume a model.
2. **Enable RTSP** on the NVR if your firmware needs it. Create a dedicated low-privilege user for streaming.
3. **Find the RTSP URL.** Many CP Plus devices use the Dahua-style format below. Treat it as a candidate until the probe succeeds; your model's manual is authoritative.
   `rtsp://<user>:<password>@<nvr-ip>:554/cam/realmonitor?channel=<n>&subtype=0` (main stream; `subtype=1` is the sub stream)
4. **Probe it.** Either use the Camera & NVR Setup tab, or run the command below, which keeps the password out of your shell history:
   ```bash
   read -rs NVR_MAIN_URL && export NVR_MAIN_URL      # paste the URL, press Enter
   read -rs NVR_SUB_URL && export NVR_SUB_URL
   docker compose run --rm -e NVR_MAIN_URL -e NVR_SUB_URL api \
     ai-surveillance probe --env NVR_MAIN_URL --env NVR_SUB_URL
   ```
5. **Switch the stack to the NVR.**
   1. In `.env`, set `FRIGATE_CAMERA_MAIN_URL` and `FRIGATE_CAMERA_SUB_URL` to the probed URLs. URL-encode special characters in the password.
   2. Remove `COMPOSE_PROFILES=test-stream`.
   3. Set `detect.width` and `detect.height` in `frigate/config.yml` to the sub-stream resolution.
   4. Restart:
      ```bash
      docker compose up -d --remove-orphans
      ```
   5. Run section 7 again.
   - If the sub stream is H.265, Firefox falls back to still frames. Set the sub stream to H.264 on the NVR if possible.

## 10. Optional: start on boot and after power loss (system change)
The systemd unit expects the code in `/opt/ai-surveillance`. Either clone it there, or point the unit at your folder:
```bash
sed "s#/opt/ai-surveillance#$HOME/ai-surveillance#" ~/ai-surveillance/deploy/systemd/ai-surveillance.service | sudo tee /etc/systemd/system/ai-surveillance.service
sudo systemctl daemon-reload
sudo systemctl enable docker
sudo systemctl enable --now ai-surveillance.service
```
Every container also has `restart: unless-stopped`. To verify, run `sudo reboot`, then check that the site returns without manual steps and that `bash doctor.sh` passes.

## 11. Something is wrong
Run `bash doctor.sh` first, then see [TROUBLESHOOTING.md](TROUBLESHOOTING.md).
