# Linux integration test checklist (ADR-0004 Phase 10)

A reproducible list for the Ubuntu AI PC. Run it **yourself**, in order, in `~/ai-surveillance/deploy` unless a step says otherwise. Steps marked **(system change)** install or change system software. Do not paste `deploy/.env`, passwords, tokens or camera addresses with passwords into reports; the commands below print names, counts and statuses only.

Status labels: **Passed**, **Failed**, **Blocked** (cannot run yet), **Not run**. The "Local" column is what was shown on the development PC (Windows 11 + WSL 2, no Docker) at commit `9e2581a` on 2026-10-30; the "Host" column is for you to fill in.

Part A (steps 1–10 with the synthetic stream and public-domain photos) needs no camera and no real credentials. Part B (steps 11 onward with the real camera and consenting people) starts only after Part A has passed and been reviewed.

## 1. Git checkout and environment
```bash
git clone -b feat/initial-platform https://github.com/rounak1434/Face_detection.git ~/ai-surveillance
cd ~/ai-surveillance && git rev-parse --short HEAD && git status --short
lsb_release -ds; uname -srm; nproc; free -h; df -h ~
docker version --format '{{.Server.Version}}'; docker compose version
```
Expect: the agreed commit, a clean tree, Ubuntu 22.04/24.04, Docker Engine with the Compose plugin. Install Docker first if missing (SETUP.md §1, system change).

| Check | Local | Host |
|---|---|---|
| Commit and clean tree | Passed (`9e2581a`, clean, in sync with GitHub) | |
| Docker Engine + Compose present | **Blocked**: no docker, podman, docker-compose or nerdctl on Windows or WSL | |

## 2. Dependencies and models
```bash
cp .env.example .env && chmod 600 .env
sed -i "s/^INFERENCE_API_TOKEN=.*/INFERENCE_API_TOKEN=$(openssl rand -hex 32)/" .env
docker compose build                     # the inference build downloads YuNet and SFace and checks SHA-256
docker compose run --rm --no-deps inference python -m inference_service.download --models-dir /models
```
Expect: the build succeeds; the last command prints "already present and verified" for `yunet` and `sface`.

| Check | Local | Host |
|---|---|---|
| Python packages install from `constraints.txt` (backend, inference) | Passed (Windows venvs; fresh WSL venvs) | |
| YuNet and SFace download, size and SHA-256 verified | Passed (Windows and WSL; also in CI) | |
| Frontend `npm ci`, `tsc`, production build | Passed (Windows; CI on Ubuntu) | |
| Images build | **Blocked** (no Docker) | |

## 3. Database backup and migration
First start creates the database at the newest revision. On an existing install, back up first:
```bash
docker compose up -d api
docker compose exec api ai-surveillance db status          # expect "at head", revision 0005_recognition
docker compose exec api ai-surveillance db backup           # prints the backup path
docker compose exec api ai-surveillance db verify /data/<backup file printed above>
```

| Check | Local | Host |
|---|---|---|
| Migrations 0001→0005 on a fresh database, model drift, downgrade/upgrade with data, legacy-schema refusal | Passed (`test_migrations.py`, `test_dbtools.py`: 109 passed in WSL; all pass on Windows) | |
| Backup, verify, restore (WAL-safe, interruption handling) | Passed (`test_dbtools.py`) | |
| `db status` / `db backup` / `db verify` in the container | **Blocked** (no Docker) | |

## 4. Compose validation
```bash
docker compose config --quiet && echo base OK
docker compose -f compose.yaml -f compose.nvidia.yaml config --quiet && echo nvidia OK
```
Never run `docker compose config` without `--quiet` in a shared terminal: it prints `.env` values.

| Check | Local | Host |
|---|---|---|
| Compose Specification schema, both file sets, interpolated from `.env.example` only; references (depends_on, volumes, bind sources, same Frigate image for `frigate-init`) | Passed (0 schema errors, 0 problems; negative controls still caught) | |
| Compose defaults equal `.env.example` and the code defaults (`test_deploy_config.py`) | Passed | |
| `docker compose config --quiet` | **Blocked** (no Docker) | |

## 5. Service startup and health
```bash
docker compose up -d && docker compose ps -a
docker compose logs inference | grep "Model "
docker compose exec api ai-surveillance create-admin admin
bash doctor.sh
bash run-integration-tests.sh
```
Expect: `frigate-init` Exited (0); `api`, `inference`, `web` and `frigate` **(healthy)** — Frigate's check comes from its own image and may show "starting" for up to 5 minutes; `test-stream` running (no health check); `yunet: ready`, `sface: ready`; doctor without errors; integration tests pass. Also check that Caddy saw the browser's address, not its own: after one wrong sign-in from another PC, signing in from this machine must still work.

| Check | Local | Host |
|---|---|---|
| All services start and are healthy (health checks: api in Compose, inference in its Dockerfile, web on Caddy's admin API, Frigate from its image) | **Blocked**; configuration checked statically (`test_every_long_running_service_has_a_health_check`) | |
| Forwarded client address trusted only from the web proxy | **Blocked** on the stack; behaviour tested with uvicorn's middleware (`test_proxy_trust.py`) | |
| `doctor.sh`, `run-integration-tests.sh` | **Blocked** | |

## 6. Frigate / go2rtc stream
In the browser (`https://localhost/`): the Live tab shows `entrance` with the test pattern and a **Live** badge. Then:
```bash
docker compose exec -T api python -c "import json,urllib.request; s=json.load(urllib.request.urlopen('http://frigate:5000/api/stats', timeout=5)); print({k: v.get('camera_fps') for k, v in s['cameras'].items()}); print({k: v.get('inference_speed') for k, v in s['detectors'].items()})"
```
Expect: `camera_fps` near 15 for `entrance`; a detector inference speed in ms. Then Camera & NVR Setup: add the synthetic source, **Test connection** → Connected, **Use in Frigate**, **Apply to Frigate** → the second camera appears in Live.

| Check | Local | Host |
|---|---|---|
| Live relay, still-frame fallback, Origin check (mocked go2rtc) | Passed (`test_rtsp_and_live.py`) | |
| Apply to Frigate with a simulated Frigate 0.18.2 (two restarts, rollback, UNKNOWN outcome), 3 repeats | Passed (69 passed ×3 in WSL with secrets and concurrency tests; ×3 on Windows) | |
| Real Frigate, go2rtc, test stream, one real Apply | **Blocked** | |

## 7. Face detection with synthetic input
The test pattern contains no person, so Frigate creates no face events from it; that is expected.
- **Automated (inference path):** step 5's `run-integration-tests.sh` already sends the public-domain portrait through `/api/analysis/faces` (`IT_PD_FACE_IMAGE`) and expects one face.
- **Optional (Frigate event path, untested):** if you want a person event without a camera, play a video or show a printed public-domain portrait to a real camera in step 11 instead; no synthetic person source is shipped, because it could not be tested here.

| Check | Local | Host |
|---|---|---|
| Real YuNet on the public-domain portrait served as a Frigate snapshot | Passed (`test_real_yunet_finds_the_face_in_a_frigate_snapshot`, WSL) | |
| Inference service with the real model (one/several/no faces, degenerate sizes, edges) | Passed (Windows and WSL, 67 passed) | |
| Integration test with the portrait through the running stack | **Blocked** | |

## 8. Snapshot and event persistence
In the browser: Live → **Snapshot** and **Save 10 s clip** (wait one minute after start) appear in Captures. Face events need a person (step 11).

| Check | Local | Host |
|---|---|---|
| Face events: one row per Frigate event across polls and restarts, cursor, paging, retries, `gave_up`, low disk, failed writes, frame and crops (`test_face_events.py`) | Passed | |
| Event API: sign-in, filters, paging, frame and crop retrieval, missing files, path checks (`test_face_events_api.py`) | Passed | |
| Captures with a mocked Frigate | Passed | |
| On the real stack | **Blocked** | |

## 9. Gallery enrollment with approved test images
People page → **Add authorized person** ("Test – public-domain portrait", consent note "test fixture") → **Add reference image** with `~/ai-surveillance/inference/tests/data/public_domain/nasa_armstrong_1964_480x600.jpg`. Expect **Ready to match**. Upload a photo with two people → refused ("More than one face").

| Check | Local | Host |
|---|---|---|
| Enrollment gate with the real YuNet (too small, two faces, no face, accepted) | Passed (WSL) | |
| Enrollment with the real SFace embedding, encrypted at rest | Passed (WSL end-to-end test) | |
| API rules, encryption, deletion, audit, races (`test_gallery_api.py`, `test_reliability_audit.py`) | Passed | |
| In the browser against the real api (scratch database) | Passed (Phase 7 manual check) | |
| On the real stack | **Blocked** | |

## 10. Recognition ON/OFF
People page → **Switch recognition on** (password). Then off again. Check the audit log shows both.
```bash
docker compose restart api && sleep 20   # the setting must survive
```
The panel must show the same state after the restart.

| Check | Local | Host |
|---|---|---|
| Off: the matcher is replaced by fail-if-called functions and never runs; events and frames still stored; control test trips when on | Passed (`test_recognition_switch.py`) | |
| Switch survives a restart; default off; damaged value fails closed; gallery unchanged when off; no retroactive matching | Passed | |
| Switching off mid-poll applies to the next event | Passed (`test_privacy_review.py`) | |
| On the real stack | **Blocked** | |

## 11. Real camera and unknown-person behaviour (Part B, after review)
Add the CP Plus camera on the Setup page (password in the form only), test until Connected, Use in Frigate, Apply. Person **A** (consenting, enrolled with 2–3 photos) and person **B** (consenting, **not** enrolled) each walk past about 10 times; some in IR if possible. Expected: A mostly `AUTHORIZED` (shown as "Automated match"), B `UNRECOGNIZED` or `INCONCLUSIVE`, never A's name. Export without names or images:
```bash
docker compose exec -T api python - <<'EOF'
import sqlite3
con = sqlite3.connect("file:/data/app.db?mode=ro", uri=True)
rows = con.execute("""select e.started_at, e.camera, e.status, e.recognition_state, d.outcome,
    round(d.similarity, 3), min(d.box_w, d.box_h), substr(d.person_id, 1, 8)
    from face_events e left join face_detections d on d.event_id = e.id
    order by e.started_at desc limit 300""").fetchall()
for r in rows: print(*r, sep="\t")
EOF
```

| Check | Local | Host |
|---|---|---|
| Different people below the threshold (Aldrin 0.178, Collins 0.118 vs enrolled Armstrong) | Passed (public-domain photos, WSL) | |
| Real camera, consenting people, threshold measurement | **Blocked** (Part B) | |

## 12. Restart and rollback
```bash
docker compose restart api inference && sleep 30 && bash doctor.sh
sudo reboot     # optional, with the systemd unit from SETUP §10 (system change)
```
After restarts: the recognition setting, people, images and events are unchanged; ingestion resumes without duplicates. **Rollback** to the previous release: `docker compose down` (keeps data), `git checkout <previous commit>`, restore the backup from step 3 with `docker compose run --rm --no-deps api ai-surveillance db restore /data/<backup> --yes`, then `docker compose up -d --build`. Never use `down -v`.

| Check | Local | Host |
|---|---|---|
| Ingestion restart-safe (no duplicates), migrations reversible, restore WAL-safe | Passed (unit tests) | |
| Real restarts, reboot, rollback | **Blocked** | |

## 13. Performance and resource use
```bash
nvidia-smi || echo "no NVIDIA driver"
docker stats --no-stream
docker compose cp ../inference/tests/data/public_domain/nasa_s69-31741_480x600.jpg inference:/tmp/face.jpg
docker compose exec inference python -m inference_service.benchmark --models-dir /models /tmp/face.jpg
```
With the GPU (SETUP §8): `docker compose -f compose.yaml -f compose.nvidia.yaml up -d`, then the Frigate stats command from step 6.

| Check | Local | Host |
|---|---|---|
| Face models: detect 13.5 ms / embed 9.5 ms per face (Windows, 480×600), 50 ms detect at 1080p, < 300 MB | Passed (laptop i5-11320H, CPU) | |
| Benchmark inside the container; Frigate detector speed CPU vs GTX 1650; `docker stats` | **Blocked** | |

## 14. Log and credential leak inspection
```bash
docker compose logs --no-color > /tmp/stack.log
grep -Ec 'rtsp://[^*@/[:space:]]+:[^*@/[:space:]]+@' /tmp/stack.log    # expect 0 (unmasked camera passwords)
grep -Eci 'password[=:]["'"'"' ]*[^*" ]{4,}' /tmp/stack.log               # expect 0
t=$(grep '^INFERENCE_API_TOKEN=' .env | cut -d= -f2); [ -n "$t" ] && grep -c -- "$t" /tmp/stack.log   # expect 0
rm /tmp/stack.log
```
These print counts only. Also check that `deploy/frigate/config.yml` (tracked) has no credentials after an Apply: `git diff --stat` must be empty.

| Check | Local | Host |
|---|---|---|
| Redaction of passwords and URL-encoded passwords in Apply errors and logs; no paths, embeddings or file names in responses; logs carry ids only | Passed (Task C tests, Phase 8 sweeps) | |
| Real logs | **Blocked** | |

## 15. Retention and deletion
```bash
docker compose exec api sh -c 'ls /data/media/faces | wc -l; ls /data/media/gallery | wc -l'
```
Delete one face event and one test person in the UI (password for the person), run the command again: the counts drop by that event's images and the person's reference images; the audit log lists both. Retention (`FACE_EVENT_RETENTION_DAYS`, default 30) runs hourly; check later for "Retention: deleted" in `docker compose logs api`.

| Check | Local | Host |
|---|---|---|
| Deletion: files before rows, failed removal keeps the row, person deletion unlinks matches, `secure_delete` overwrites rows, audited | Passed (unit tests) | |
| Retention with files; a file that cannot be removed keeps its event | Passed | |
| On the real stack | **Blocked** | |

## Report back
For each step: the Host status, the command outputs above (they contain no secrets), and anything unexpected. Part B begins only after Part A is reviewed.
