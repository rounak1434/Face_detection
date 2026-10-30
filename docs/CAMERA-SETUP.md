# Camera & NVR Setup page

The **Camera & NVR Setup** tab in the web app has three clearly separated kinds of source.

| Source | What it tests | What it does **not** test |
|---|---|---|
| **Browser webcam** (purple, "Local preview only") | That the page, layout and camera permission work in your browser | Nothing on the server. The video never leaves the browser; it is not sent, stored, recorded or seen by the engine. **It does not verify RTSP, Frigate, snapshots or clips.** |
| **Synthetic RTSP test source** (amber, "Test pattern, not a camera") | The real server-side pipeline: the api's ffprobe check, and Frigate reading RTSP | A real camera's codec, credentials or network |
| **IP cameras and NVRs** (blue, "Real devices") | Reachability, credentials and the stream of a real device on your LAN | Nothing beyond the connection test, see "Current limits" |

## Statuses
| Status | Meaning |
|---|---|
| **Not Tested** | Saved, or changed since the last test. No claim either way. |
| **Connected** | The server ran ffprobe and **decoded a video stream** (codec and resolution are shown). This is the only status that means "works". |
| **Unreachable** | DNS failed, or no TCP answer on the RTSP port, or the connection was refused or timed out. |
| **Invalid Credentials** | The device answered 401/403. |
| **Stream Not Found** | The device answered 404 for the path. |
| **Reachable, Stream Not Verified** | The port answers, but ffprobe is not installed where the api runs, so the stream and credentials were **not** checked. This is what a dev PC without FFmpeg shows. |
| **Address Not Allowed** | The address is loopback, link-local or public, or outside `DEVICE_ALLOWED_NETWORKS`. |
| **Error** | Anything else. The detail line shows the reason, with credentials masked. |

The main stream decides the device status. When a substream is configured, its own result is shown next to it.

## Security rules
- **Passwords are write-only.**
  - They are stored encrypted with AES-256-GCM, bound to the device id.
  - The key comes from `CREDENTIALS_KEY`, or is generated once into `CREDENTIALS_KEY_FILE` (`/data/credentials.key` in Docker) with owner-only permissions.
  - The API returns only `has_password`, and URLs with `***@` in place of the credentials. Passwords are never logged.
  - When editing, an empty password field keeps the stored one.
- **No arbitrary probing.** Tests run only for saved devices, by id. There is no endpoint that accepts a URL to probe.
- **SSRF guard.**
  - The host must be an IP address or host name. Every address it resolves to must lie in `DEVICE_ALLOWED_NETWORKS`, which is private LAN ranges by default.
  - Loopback, link-local (including cloud metadata `169.254.169.254`), multicast and reserved addresses are always refused.
  - IP literals are checked when saving, host names when testing.
  - The probe connects to the **checked IP**, so a DNS change between check and connect has no effect.
- **Stream input.** A path (`/cam/realmonitor?channel=1&subtype=0`) or an `rtsp://` URL for the **same** host and port. Credentials inside the URL are refused. Other schemes, such as `file:` or `http:`, are refused, and ffprobe is limited to network streaming protocols.
- **Rate limit.** One test per device every 5 seconds.
- **Known limitation.** While a test runs, the ffprobe command line inside the api container contains the credentials, as RTSP clients require. Only processes inside that container can see it.
- **Recommendation.** Set `DEVICE_ALLOWED_NETWORKS` to your camera subnet only, for example `192.168.1.0/24`.

## Current limits (honest scope)
- **The engine does not use saved devices yet.** The live view and recordings still use `FRIGATE_CAMERA_MAIN_URL` and `FRIGATE_CAMERA_SUB_URL` in `deploy/.env`. Connecting saved devices to Frigate is a later step.
- **Device compatibility is not assumed.** The CP Plus path shown as a placeholder is a common format, not a verified one for your model.

## Testing on this Windows laptop (no Docker, no FFmpeg, no WSL2)
This runs the real backend and UI, but not Frigate or the RTSP pipeline. In PowerShell:
```powershell
cd backend; python -m venv .venv; .\.venv\Scripts\pip install -e ".[dev]"
.\.venv\Scripts\pytest                                   # unit tests
$env:COOKIE_SECURE = "false"; $env:FRIGATE_TIMEOUT_SECONDS = "2"
.\.venv\Scripts\ai-surveillance create-admin admin
.\.venv\Scripts\uvicorn --factory ai_surveillance.main:app --port 8000
```
Then, in a second PowerShell window:
```powershell
cd frontend; npm install; npm run dev                    # open http://localhost:5173
```
What to expect:
| Source | Result |
|---|---|
| Webcam | Works (localhost is a secure context) |
| Synthetic source | **Unreachable**: there is no `test-stream` container |
| Real device on your LAN | **Reachable, Stream Not Verified** at best, because there is no ffprobe |
| Live tab | "Engine unreachable" |

If you install FFmpeg for Windows (`winget install Gyan.FFmpeg`, your choice), tests of real devices can reach **Connected**. Frigate still needs Linux and Docker.
