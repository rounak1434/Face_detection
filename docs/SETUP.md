# Setup on the Linux host

Target: Ubuntu 22.04 or 24.04 LTS on x86-64 (i7, 32 GB RAM, NVIDIA GTX 1650 4 GB).

Run these commands yourself. Steps marked **(system change)** install packages or change system configuration.

## What has to be on the host
| Needed | Why | Not needed |
|---|---|---|
| Docker Engine + Compose plugin | Runs Frigate, the api, the web server and the test stream | Docker Desktop |
| git, curl | Get the code; `doctor.sh` uses curl | |
| python3-venv | Only for running the integration tests from the host | |
| NVIDIA driver + NVIDIA Container Toolkit | Only for GPU detection (section 7) | Not needed for milestone 1 |

**FFmpeg does not need to be installed on the host.** The test-stream image (`mediamtx:1.9.3-ffmpeg`) and the api image both include ffmpeg/ffprobe, and Frigate ships its own.

## 1. Prerequisites (system change)
Install Docker from Docker's own apt repository. This follows https://docs.docker.com/engine/install/ubuntu/, so check that page if a command fails.
```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl git python3-venv
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | sudo tee /etc/apt/sources.list.d/docker.list
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo usermod -aG docker "$USER"     # then log out and back in
```
Check:
```bash
docker version && docker compose version && docker run --rm hello-world
```
If the host firewall (ufw) is on, allow the web ports with `sudo ufw allow 80,443/tcp`. Do **not** open 5000, 8554 or 8971.

## 2. Get the code
```bash
sudo mkdir -p /opt/ai-surveillance && sudo chown "$USER" /opt/ai-surveillance
git clone https://github.com/rounak1434/Face_detection.git /opt/ai-surveillance
cd /opt/ai-surveillance && git switch feat/initial-platform    # until it is merged to main
cd deploy
cp .env.example .env && chmod 600 .env
```
Edit `.env`:
- **`SITE_ADDRESS`**: the host name or LAN IP operators will type, for example `192.168.1.50`. Keep `localhost` if you only test on the host itself.
- **`TZ`**: your timezone.
- **Camera URLs**: leave them on the test stream for now.

## 3. Start the stack with the synthetic test stream
```bash
cd /opt/ai-surveillance/deploy
docker compose --profile test-stream pull        # Frigate, mediamtx (fails here if an image tag is wrong)
docker compose --profile test-stream up -d --build
docker compose ps                                 # frigate, api, web, test-stream: all "running"
bash doctor.sh                                    # read-only health check
```
The first Frigate start takes a minute or two. To watch it:
```bash
docker compose logs -f frigate
```

## 4. Create the administrator and open the website
```bash
docker compose exec api ai-surveillance create-admin admin      # prompts for a password (min 12 chars)
```
- Open `https://<SITE_ADDRESS>/` in a browser and sign in.
- The certificate comes from Caddy's own local CA, so the first visit shows a warning. To trust it permanently, install the root certificate on each operator PC:
  ```bash
  docker compose cp web:/data/caddy/pki/authorities/local/root.crt ./caddy-root.crt
  ```
- You should see the camera **entrance** with a moving colour test pattern and a **Live** badge.
- Press **Snapshot**. Then, after the stack has run for at least a minute, press **Save 10 s clip**.

Running `create-admin` again with the same name resets the password and signs that user out everywhere.

**Frigate's own UI**, for administrators only, is on `https://127.0.0.1:8971` on the host. Reach it from another PC with `ssh -L 8971:127.0.0.1:8971 <host>`. Its first admin password is printed once at first start:
```bash
docker compose logs frigate | grep -iA3 password
```

## 5. Integration tests (real stack, no mocks)
```bash
cd /opt/ai-surveillance/backend
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest                          # unit tests (mocked engine)
IT_BASE_URL=https://<SITE_ADDRESS> IT_VERIFY_TLS=0 IT_ADMIN_USER=admin IT_ADMIN_PASSWORD='<password>' \
  .venv/bin/pytest -m integration -v      # real stack
```
- `IT_BASE_URL` must use the same name as `SITE_ADDRESS`, because Caddy only answers for that name.
- Wait about a minute after the stack starts, so that a recording exists for the clip test.
- Record the output in `docs/VERIFICATION.md`.

## 6. Connect the CP Plus NVR
1. **Find the exact models.** Read the NVR and camera model numbers from the device labels or the web UI, and record them in VERIFICATION.md. Do not assume a model.
2. **Enable RTSP** on the NVR if your firmware needs it. Create a dedicated low-privilege user for streaming.
3. **Find the RTSP URL.** Many CP Plus devices use the Dahua-style format below. Treat it as a candidate until the probe succeeds; your model's manual is authoritative.
   `rtsp://<user>:<password>@<nvr-ip>:554/cam/realmonitor?channel=<n>&subtype=0` (main stream; `subtype=1` is the sub stream)
4. **Probe it** without putting the password in your shell history:
   ```bash
   cd /opt/ai-surveillance/deploy
   read -rs NVR_MAIN_URL && export NVR_MAIN_URL      # paste the URL, press Enter
   read -rs NVR_SUB_URL && export NVR_SUB_URL
   docker compose run --rm -e NVR_MAIN_URL -e NVR_SUB_URL api \
     ai-surveillance probe --env NVR_MAIN_URL --env NVR_SUB_URL
   ```
   Success lists the codec, width, height and fps. Paste the masked output into VERIFICATION.md.
5. **Switch the stack to the NVR.**
   1. Put the two URLs into `FRIGATE_CAMERA_MAIN_URL` and `FRIGATE_CAMERA_SUB_URL` in `.env`.
   2. Set `detect.width` and `detect.height` in `frigate/config.yml` to the sub-stream resolution from the probe.
   3. Restart:
      ```bash
      docker compose up -d
      ```
   4. Repeat section 5.
   - If the sub stream is H.265, Firefox and older browsers fall back to still frames (see TROUBLESHOOTING.md). Set the sub stream to H.264 on the NVR if possible.

## 7. GPU detection (separate, measured step; system change)
1. Install the NVIDIA driver and check it:
   ```bash
   nvidia-smi
   ```
2. Install the NVIDIA Container Toolkit: https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html. Check it:
   ```bash
   docker run --rm --gpus all ubuntu nvidia-smi
   ```
3. Export a YOLOv9-s 320×320 ONNX model as described at https://docs.frigate.video/configuration/object_detectors, and place it at `deploy/frigate/model_cache/yolo.onnx`.
4. Uncomment the `detectors`/`model` block in `deploy/frigate/config.yml`.
5. Start with the GPU override, which also switches to the `-tensorrt` image:
   ```bash
   docker compose -f compose.yaml -f compose.nvidia.yaml up -d
   ```
6. Record `detectors.*.inference_speed` from Frigate's `/api/stats` in VERIFICATION.md.

## 8. Start on boot and after power loss (system change)
```bash
sudo cp /opt/ai-surveillance/deploy/systemd/ai-surveillance.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable docker
sudo systemctl enable --now ai-surveillance.service
```
Every container also has `restart: unless-stopped`. To verify, run `sudo reboot`, then check that `https://<SITE_ADDRESS>/` returns without manual steps and that `bash doctor.sh` passes.

## 9. Something is wrong
Run `bash deploy/doctor.sh` first, then see [TROUBLESHOOTING.md](TROUBLESHOOTING.md).
