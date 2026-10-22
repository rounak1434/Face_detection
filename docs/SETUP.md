# Setup on the Linux host

Target: Ubuntu 22.04 or 24.04 LTS on x86-64 (i7, 32 GB RAM, NVIDIA GTX 1650 4 GB).

The commands below are shown for review; run them yourself. A note marks each step that changes system configuration.

## 1. Prerequisites
1. **Docker Engine + Compose plugin.** Follow the official guide at https://docs.docker.com/engine/install/ubuntu/ (this changes system packages). Then check:
   ```bash
   docker version && docker compose version
   ```
2. **NVIDIA driver**, needed only for GPU detection (section 6). Check with:
   ```bash
   nvidia-smi
   ```
   Note the driver and CUDA versions in VERIFICATION.md.
3. **NVIDIA Container Toolkit**, also only for GPU detection. Install it from https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html (this changes the Docker runtime configuration). Then check:
   ```bash
   docker run --rm --gpus all ubuntu nvidia-smi
   ```

## 2. Get the code
```bash
sudo mkdir -p /opt/ai-surveillance && sudo chown "$USER" /opt/ai-surveillance
git clone https://github.com/rounak1434/Face_detection.git /opt/ai-surveillance
cd /opt/ai-surveillance/deploy
cp .env.example .env && chmod 600 .env
```
Edit `.env`:
- Set `SITE_ADDRESS` to the host name or IP that operators use.
- Set `TZ`.
- Leave the camera URLs on the test stream for now.

## 3. First run with the synthetic test stream
```bash
docker compose --profile test-stream up -d --build
docker compose ps
docker compose logs frigate | grep -i password     # Frigate prints its initial admin password once
docker compose exec api ai-surveillance create-admin admin
```
Open `https://<SITE_ADDRESS>/` and sign in. You should see the camera `entrance` showing a moving test pattern.

To check the test stream from the host:
```bash
docker compose exec api ai-surveillance probe rtsp://test-stream:8554/test
```

**Trusting the HTTPS certificate:** Caddy uses its own local CA. Copy its root certificate and install it on each operator PC as a trusted root. This avoids browser warnings.
```bash
docker compose cp web:/data/caddy/pki/authorities/local/root.crt ./caddy-root.crt
```

## 4. Integration tests (on the host)
```bash
cd /opt/ai-surveillance/backend
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
IT_BASE_URL=https://localhost IT_VERIFY_TLS=0 IT_ADMIN_USER=admin IT_ADMIN_PASSWORD='<your password>' \
  .venv/bin/pytest -m integration -v
```
Record the result in `docs/VERIFICATION.md`.

## 5. Connect the CP Plus NVR
1. **Find the exact models.** Read the NVR and camera model numbers from the device labels or the web UI, then record them in VERIFICATION.md. Do not assume a model.
2. **Enable RTSP** on the NVR, if your firmware needs it. Create a dedicated low-privilege user for streaming.
3. **Find the RTSP URL.** Many CP Plus devices use the Dahua-style format below. Treat it as a candidate until the probe succeeds; your model's manual is authoritative.
   `rtsp://<user>:<password>@<nvr-ip>:554/cam/realmonitor?channel=<n>&subtype=0` (main stream; `subtype=1` is the sub stream)
4. **Probe without putting the password in shell history:**
   ```bash
   read -rs NVR_MAIN_URL && export NVR_MAIN_URL      # paste the URL, press Enter
   read -rs NVR_SUB_URL && export NVR_SUB_URL
   docker compose run --rm -e NVR_MAIN_URL -e NVR_SUB_URL api \
     ai-surveillance probe --env NVR_MAIN_URL --env NVR_SUB_URL
   ```
   A successful result lists the codec, width, height and fps. Paste the masked output into VERIFICATION.md.
5. **Switch the stack to the NVR.**
   1. Put the two URLs into `FRIGATE_CAMERA_MAIN_URL` and `FRIGATE_CAMERA_SUB_URL` in `.env`.
   2. Set `detect.width` and `detect.height` in `frigate/config.yml` to the sub-stream resolution from the probe.
   3. Restart:
      ```bash
      docker compose up -d
      ```
   4. Repeat section 4.

## 6. GPU detection (separate, measured step)
Export a YOLOv9-s 320×320 ONNX model as described at https://docs.frigate.video/configuration/object_detectors. Then:
1. Place the model at `deploy/frigate/model_cache/yolo.onnx`. The file is gitignored.
2. Uncomment the `detectors`/`model` block in `deploy/frigate/config.yml`.
3. Start with the GPU override:
   ```bash
   docker compose -f compose.yaml -f compose.nvidia.yaml up -d
   ```
4. Record the inference speed shown in Frigate's System page, or at `/api/stats`, in VERIFICATION.md.

## 7. Start on boot (changes system configuration)
```bash
sudo cp /opt/ai-surveillance/deploy/systemd/ai-surveillance.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ai-surveillance.service
sudo systemctl enable docker
```
Each container also has `restart: unless-stopped`. To verify, reboot the host and check that `https://<SITE_ADDRESS>/` comes back without manual steps.
