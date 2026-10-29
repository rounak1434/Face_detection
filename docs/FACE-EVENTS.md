# Face events

Each finished **person event** in Frigate is checked once for faces with the face detector (YuNet) and stored as a **face event**: where faces are. Only while the **recognition switch** is on (off by default) is each face also compared with the authorized-person gallery; see [RECOGNITION.md](RECOGNITION.md). Design and phase plan: [ADR-0004](adr/0004-face-events-and-recognition.md).

Status: ingestion and storage (Phase 3), the API (Phase 4) and matching (Phase 6) are implemented. The web page comes in Phase 7. Tested with a simulated Frigate and the real detector and embedding models; **not yet run against a real Frigate or camera**.

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

## API
All routes need a signed-in session (the existing login). Responses never contain file names, paths or embeddings.

| Method and path | Result |
|---|---|
| `GET /api/face-events` | `{"items": [event, …], "next_cursor": "…" or null}`, newest first. Query: `camera`, `status` (one of the statuses above), `has_faces` (`true`/`false`), `since`, `until` (ISO 8601 start time; `since` inclusive, `until` exclusive; UTC when no offset is given), `outcome` (events with at least one face with this recognition outcome), `person_id` (events where this person was recognized), `limit` (1–200, default 50), `cursor` (from `next_cursor`). Paging is by start time and id, so events arriving meanwhile do not shift or repeat pages. An unknown status, a bad cursor, a bad date or a limit out of range gives 422. |
| `GET /api/face-events/{id}` | One event; 404 if unknown |
| `GET /api/face-events/{id}/frame` | The stored frame (JPEG, `Cache-Control: private, no-store`); 404 when the event kept no frame or the file is missing |
| `GET /api/face-events/{id}/detections/{detection_id}/crop` | One face crop; only through its own event (another event's id gives 404) |
| `DELETE /api/face-events/{id}` | 204; images first, then the rows. If an image cannot be removed: 500 with the OS reason only, and the event is kept so it can be deleted again |

Event fields: `id`, `frigate_event_id`, `camera`, `started_at`, `ended_at`, `status`, `face_count`, `frame_source`, `frame` (`width`, `height`) or null, `frame_url` or null, `detector`, `detail`, `recognition_state` (`off`, `unavailable`, `done`; null for events stored before recognition existed), `recognizer` (e.g. `sface 2021dec`), `detections`. Detection fields: `id`, `position`, `box` (`x`, `y`, `w`, `h` in frame pixels), `score` (detection confidence only), `keypoints5`, `quality`, `crop_url` or null, `recognition` or null: `outcome`, `person` (`id`, `display_name`; only for `AUTHORIZED`, null once that person is deleted), `similarity`, `thresholds` (`accept`, `reject` used for this face).

```bash
curl -k -b cookies.txt "https://localhost/api/face-events?camera=entrance&has_faces=true&limit=20"
```

## Not known yet
- Whether faces at the real door are large enough, in the recording or the snapshot.
- How often the recording is not yet written when an event has just ended (the snapshot is then used).
- Load on the Ubuntu host with many events.
