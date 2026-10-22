# ADR-0001: Use Frigate as the camera-processing engine

- Status: accepted for milestone 1; the face-recognition part is to be decided in milestone 2
- Date: 2026-10-22

## Context
We need RTSP ingestion, restreaming to browsers, person detection, continuous recording and clip export. Building these well takes significant effort. Frigate provides all of them, runs locally in Docker, and is MIT-licensed. The "Frigate" name and logo are trademarks of Frigate, Inc., so our UI does not use them as branding.

Facts checked on 2026-10-22 against docs.frigate.video and the GitHub repository:
- Latest release: 0.18.2.
- NVIDIA GPUs on x86 use the ONNX detector in the `-tensorrt` image. YOLOv9-s at 320×320 is the suggested starting model. The docs give no GTX 1650 benchmark.
- Port 8971 serves the authenticated UI and API (JWT via `POST /api/login`). Port 5000 is unauthenticated and treats every request as admin.
- Built-in face recognition exists:
  - Models: a small FaceNet model on CPU, or a large ArcFace model on GPU.
  - Output: a `sub_label` name, assigned only after results agree across multiple frames.
  - Limits: no negative training, and weaker on IR/grayscale images.
  - Storage and network: face data stays under `/media/frigate/clips/faces`, and models are downloaded once.

## Decision
Use Frigate (image `0.18.2-tensorrt`) for ingestion, restreaming, detection, recording and export. Our API integrates only through Frigate's HTTP API now, and MQTT later. It never reads Frigate's database or files directly.

## Consequences
- Milestone 1 depends on these Frigate endpoints:
  - `/api/version`, `/api/config`, `/api/<cam>/latest.jpg`
  - `POST /api/export/<cam>/start/<s>/end/<e>`, `/api/exports/<id>`, `/exports/<file>`
  - `/live/mse/api/ws?src=<cam>`

  They come from documentation and prior knowledge. **Each must be confirmed against the running instance's `/api/docs`** and recorded in VERIFICATION.md.
- Face recognition options for milestone 2:
  - **A:** Frigate's built-in face recognition, mapping `sub_label` to registry IDs.
  - **B:** our own matcher on OpenCV YuNet + SFace, fed by Frigate person events.

  The deciding questions:
  - Can we obtain per-attempt scores, which we need to report `INCONCLUSIVE` honestly?
  - How accurate is it on the real camera, measured on-site?
  - What does each option cost in GPU memory on a 4 GB card?

  InsightFace pretrained weights are excluded unless their non-commercial licence is accepted.
- The default CPU detector is used until GPU detection is measured on the GTX 1650.
