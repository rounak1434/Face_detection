# Authorized-person gallery

The gallery holds the people who are **authorized** and who have **consented** to being recognized, each with one or more reference images. An administrator creates every person and uploads every image by hand. Design and phase plan: [ADR-0004](adr/0004-face-events-and-recognition.md).

Status: Phase 5 (people, consent records, enrollment from uploads, deletion, audit log) and Phase 6 (embeddings, matching, the recognition switch: [RECOGNITION.md](RECOGNITION.md)) are implemented. Nobody is matched while the recognition switch is off (the default). The **People** page (Phase 7) adds people with the consent record, uploads reference images (showing every rejection reason), shows whether each image is ready to match, edits a person's name, notes and consent record, activates and deactivates people, removes images after confirmation, deletes people only with the password, and shows the audit log. Tested in-process with real SQLite, real encrypted files and the real detector and embedding models; not yet run on the Ubuntu host.

## Rules
- **Uploads only.** A reference image can only come from a file an administrator uploads. There is no way to enroll from a face event, a crop, a Frigate snapshot or a camera frame, and nobody is ever enrolled automatically.
- **Consent first.** A person cannot be created without a consent date and a consent note saying where the evidence is kept (for example "signed form #12, HR file"). The product records consent; it does not store the form and does not decide whether your use is lawful. Local law on biometric data (for example India's DPDP Act 2023 or the GDPR) may require notice, purpose limits and more.
- **Exactly one face.** The detector looks for every face down to a detection score of 0.5. No face, or more than one (including a weak face in the background), refuses the image with 422.
- **Quality gate.** The one face must have a detection score of at least 0.9, be at least 80 px wide and high, and not touch the image edge. Every failed condition is listed in the answer. These are starting points chosen for a later face-matching model (SFace works on 112 px aligned faces), not measured thresholds.
- **Encrypted at rest.** Reference images and their embeddings are encrypted with AES-256-GCM using the same key as camera passwords (`credentials.key` or `CREDENTIALS_KEY`), bound to the reference face's id. Images are decrypted only to answer the signed-in image route (never cached by the browser) and to compute an embedding; embeddings never leave the server.
- **Embedding at enrollment.** When the inference service has SFace loaded, the embedding is computed in the same pass as the face check. Without it the image is still enrolled (`embedded: false`) and embedded later, when recognition is switched on or during the next poll while it is on.
- **Password for deletion.** Deleting a person needs the signed-in administrator's password again. A wrong password gives 403 (the session stays valid), is written to the audit log and counts towards the same lockout as sign-in (5 failures, 5 minutes). Removing a single reference image needs a signed-in session only.
- **Kept until deleted.** Gallery data has no expiry. Making a person inactive keeps the record and images but excludes them from matching; deleting removes the images first, then the records and embeddings, and unlinks the person from past matches.
- **Audit log.** Creating, changing and deleting people, enrolling and removing reference images, and failed password confirmations are recorded with the username, time, action and ids. Changes record field names, not values; no names, notes, consent text or images go into the audit log or the server log.

## API
All routes need a signed-in session. JSON errors are `{"error", "detail"}` like `/api/analysis/faces`. Responses never contain file names, paths or encrypted data.

| Method and path | Result |
|---|---|
| `GET /api/people?active=` | `{"items": [person, …]}` by name; `active=true/false` filters |
| `POST /api/people` | 201 person. Body: `display_name` (1–100), `consent_date` (`YYYY-MM-DD`, not in the future), `consent_note` (1–500), optional `notes` (≤500). Missing or empty fields, control characters, a future or pre-2000 date: 422 |
| `GET /api/people/{id}` | The person with `faces` |
| `PATCH /api/people/{id}` | Change any of `display_name`, `notes` (`null` clears), `consent_date`, `consent_note`, `active`; the other fields cannot be cleared (422) |
| `DELETE /api/people/{id}` | Body `{"password": "…"}`. 204; 403 wrong password; 429 locked out; 500 with the OS reason if an image cannot be removed (the person is kept and can be deleted again) |
| `POST /api/people/{id}/faces` | Multipart field `file` (JPEG, PNG or WebP, up to 10 MB, the same upload checks as the analysis page). 201 reference face. 422 `no_face`, `multiple_faces`, `low_quality` (with `reasons`); 409 `person_inactive` or `too_many_faces` (10 per person); 503 when the detector is unavailable or busy; 507 when the data volume is below `CAPTURE_MIN_FREE_MB` |
| `GET /api/people/{id}/faces/{face_id}/image` | The original uploaded image, decrypted (`Cache-Control: private, no-store`). 500 if it cannot be decrypted (key changed or file damaged); 404 if the file is missing |
| `DELETE /api/people/{id}/faces/{face_id}` | 204 |
| `GET /api/audit-log?limit=&cursor=` | `{"items", "next_cursor"}`, newest first, 1–200 per page |

Person fields: `id`, `display_name`, `notes`, `consent` (`date`, `note`), `active`, `face_count`, `created_by`, `created_at`, `updated_at`, and `faces` on single-person answers. Reference face fields: `id`, `image` (`width`, `height`, `media_type`), `image_url`, `box`, `score` (detection confidence, not identity), `keypoints5`, `quality`, `detector`, `embedded` (an embedding exists), `embedder` (e.g. `sface 2021dec`), `created_by`, `created_at`. The embedding itself is never returned.

```bash
curl -k -b cookies.txt -X POST https://localhost/api/people -H 'Content-Type: application/json' \
  -d '{"display_name": "Test Person", "consent_date": "2026-10-30", "consent_note": "signed form #1"}'
curl -k -b cookies.txt -F file=@reference.jpg https://localhost/api/people/<id>/faces
```

## Settings
`GALLERY_MIN_FACE_PX` is in `deploy/.env` and passed by Compose; the others are not passed by Compose (the defaults apply; set them in the api's environment if needed).
| Variable | Default | Effect |
|---|---|---|
| `GALLERY_MIN_SCORE` | `0.9` | Detection score the one face must reach |
| `GALLERY_OTHER_FACE_SCORE` | `0.5` | Faces at or above this count towards "exactly one" |
| `GALLERY_MIN_FACE_PX` | `80` | Smallest accepted face side in pixels |
| `GALLERY_MAX_FACES_PER_PERSON` | `10` | Reference images per person |

## Storage and backups
- Encrypted images: `/data/media/gallery/<uuid>.enc` in the `api-data` volume. Records: `people`, `reference_faces`, `audit_log` in `app.db` (migration `0004_gallery`, [DATABASE.md](DATABASE.md)).
- **Back up the key with the data.** Without `credentials.key` (or the same `CREDENTIALS_KEY`) the reference images and embeddings cannot be decrypted; every person must then be enrolled again.
- `ai-surveillance db backup` copies the database only. Back up `/data/media/gallery` at the same time, or a restored database will list reference images whose files are missing (their image route answers 404; delete and enroll them again).
