# Face events

Each finished **person event** in Frigate is checked once for faces with the face detector (YuNet) and stored as a **face event**. This is **face detection only**: it records where faces are, not who they are. Nobody is identified, and nothing is matched against anyone. Design and phase plan: [ADR-0004](adr/0004-face-events-and-recognition.md).

Status: ingestion and storage are implemented (Phase 3). The API and the web page for face events come in Phase 4 and Phase 7. Tested with a simulated Frigate and the real detector model; **not yet run against a real Frigate or camera**.

## How it works
1. Every 10 seconds the api asks Frigate for finished person events that have a snapshot, oldest first (`GET /api/events?labels=person&has_snapshot=1&in_progress=0`).
2. For each event it has not stored yet, it takes the event's best frame:
   - `FACE_FRAME_SOURCE=recording` (default): the same moment decoded from the recording, normally the main stream, so faces are larger;
   - if no recording covers that moment, or with `FACE_FRAME_SOURCE=snapshot`: Frigate's event snapshot from the detect stream, requested without boxes or timestamps drawn on it.
3. The frame goes to the face detector (the same model as the AI analysis page).
4. The result is stored once per Frigate event.

| Status | Meaning | Images kept |
|---|---|---|
| `faces_detected` | One or more faces found | the frame and one crop per face |
| `no_faces_detected` | A person, but no face found (turned away, too small, dark) | none |
| `frame_unavailable` | Frigate had no frame for the event | none |
| `frame_rejected` | The detector refused the frame (corrupt or too large) | none |
| `inference_failed` | The detector failed on this frame | none |
| `gave_up` | Frigate or the detector stayed unavailable for this event for 3 tries | none |

Per face: box, detection score (how sure the detector is that it is a face, not who it is), the 5 alignment keypoints and quality hints (face size, small face, at the image edge, sharpness).

## Reliability
- **Once per event**: the database refuses a second row for the same Frigate event, across polls and restarts.
- **After a restart** it continues from the newest stored event, looking back `FACE_EVENT_LOOKBACK_SECONDS` (1 hour) because Frigate lists events by their start time and an event appears only when it has ended. A person event longer than that is missed. On the very first start it looks back `FACE_BACKFILL_SECONDS` (1 hour); older events are not analysed.
- **Order**: events are handled oldest first. If Frigate or the detector is unavailable, the poll stops and the same event is tried again at the next poll; later events wait. After `FACE_MAX_ATTEMPTS` (3) tries the event is recorded as `gave_up` so it cannot block the rest.
- **Disk**: no face event is analysed while less than `CAPTURE_MIN_FREE_MB` is free on the data volume (the same floor as captures). Images are written before the database row; if the row cannot be written, the images are removed again.
- Problems are logged once when they start and once when they end, not on every poll.

## Storage, retention and privacy
- Images live in the api volume under `media/faces/`, with server-generated names. They show people's faces: treat backups of the volume accordingly.
- Frames are kept **only** for events where a face was found; for all other events only the row is kept.
- `FACE_EVENT_RETENTION_DAYS` (default **30**): older face events are deleted hourly, images first, then the row. `0` keeps them until deleted.
- Nothing is sent outside the machine.

## Settings (`deploy/.env`)
| Variable | Default | Effect |
|---|---|---|
| `FACE_EVENTS_ENABLED` | `true` | `false` stops the ingestion (stored events stay) |
| `FACE_EVENT_RETENTION_DAYS` | `30` | See above |
| `FACE_FRAME_SOURCE` | `recording` | `recording` or `snapshot` |
| `FACE_MIN_SCORE`, `FACE_MAX_FACES` | `0.7`, `10` | Detector threshold and faces per frame (not passed by Compose; set them in the api's environment if needed) |
| `FACE_POLL_SECONDS`, `FACE_EVENT_LOOKBACK_SECONDS`, `FACE_BACKFILL_SECONDS`, `FACE_MAX_ATTEMPTS` | `10`, `3600`, `3600`, `3` | Polling (not passed by Compose) |

## Not known yet
- Whether faces at the real door are large enough, in the recording or the snapshot.
- How often the recording is not yet written when an event has just ended (the snapshot is then used).
- Load on the Ubuntu host with many events.
