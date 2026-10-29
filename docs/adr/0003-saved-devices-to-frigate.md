# ADR-0003: Feeding saved cameras and NVRs to Frigate

- Status: **accepted** (option A), 2026-10-29
- Date: 2026-10-29

**Owner's decisions (2026-10-29):** option A; Compose may move Frigate's live config off the tracked file and add the `frigate-secrets` volume; a Frigate restart happens only on an explicit "Apply to Frigate", with a warning; only devices with "Use in Frigate" switched on **and** a successful connection test are applied; the fixed `entrance` camera from `.env` stays alongside the saved devices.

## Context

### How it works today
- **Saved devices** (`Device` table, `routes/devices.py`): name, kind, host, RTSP port, username, stream paths, and the password encrypted with AES-256-GCM (`credentials.py`). They are used only for connection tests. The browser never receives a password or a credential-bearing URL (`to_dict`).
- **Frigate's cameras** come from `deploy/frigate/config.yml`, which is **tracked in git** (`.gitignore`: `!deploy/frigate/config.yml`) and **bind-mounted** as Frigate's `/config` (`compose.yaml`: `./frigate:/config`). Stream URLs reach Frigate only through the environment variables `FRIGATE_CAMERA_MAIN_URL`/`_SUB_URL` from `deploy/.env`, via Frigate's `{FRIGATE_*}` substitution. One camera, `entrance`, is fixed in the file.

### What Frigate 0.18.2 offers (checked in the v0.18.2 source on 2026-10-29)
| Endpoint | Behaviour | Source |
|---|---|---|
| `POST /api/config/save?save_option=restart` | Body: the whole YAML as text. **Validated before writing** (`FrigateConfig.parse_yaml`); invalid → 400 "Your configuration is invalid." and the old file is untouched. Overwrites the whole file. New cameras apply only after a restart (`save_option=restart` restarts Frigate). Admin role. | `frigate/api/app.py` `config_save` |
| `PUT /api/config/set` | Merges keys. **Writes first, then validates**; restores the old file on validation errors, but **not** in its generic 500 path. Can add a camera live with `update_topic`. Admin role. | `frigate/api/app.py` `config_set` |
| `POST /api/restart` | Restarts Frigate. Admin role. | `frigate/api/app.py` |
| `GET /api/config` | Masks credentials in camera paths and go2rtc streams. | `frigate/api/app.py` `config` |
| `GET /api/config/raw`, `/api/config/raw_paths` | Return the raw file and unmasked input paths. Admin role; on the internal port 5000 every caller is admin. | `frigate/api/app.py` |
| `{FRIGATE_*}` substitution | Values come from environment variables **and from files in `/run/secrets`** (or `$CREDENTIALS_DIRECTORY`) whose names start with `FRIGATE_` (content stripped; files win over variables). Read **once at startup**. | `frigate/config/env.py` |

### Hazards found
1. **Tracked file.** Both write endpoints write `/config/config.yml`, which on the host is the git-tracked `deploy/frigate/config.yml`. Using them as deployed would modify a tracked file on the host (and conflict with `git pull`).
2. **Passwords in plain text.** Putting RTSP URLs with credentials into the config stores them in that file and returns them from `/api/config/raw` to anything on the internal network (port 5000).
3. **Restart needed.** Secrets are read at startup, so new or changed credentials take effect only after a Frigate restart: live view and recording pause for about a minute.
4. **`config/set` can leave a half-written file** on an unexpected error; `config/save` cannot.

## Options
| | How | Passwords | Failure safety | Changes needed |
|---|---|---|---|---|
| **A (recommended)** | The api renders the full config from a tracked **template** plus the approved devices, using placeholders such as `{FRIGATE_DEV_<id>_MAIN_URL}`; writes each URL into a **secret file** on a volume shared only with Frigate (`/run/secrets`, read-only in Frigate); then `POST /api/config/save?save_option=restart`. | Never in the config file, never in the browser, never in `/api/config/raw`. Visible to Frigate's admin-only `raw_paths`, as the `.env` URLs are today. | Invalid config is refused by Frigate before writing. The api keeps the last applied config and secret files and restores them if Frigate does not come back with the expected cameras. | Compose: Frigate's live config moves off the tracked file (seeded from the template on first start) and a `frigate-secrets` volume is added. DB migration: a per-device "Use in Frigate" flag. |
| B | Inline URLs via `config/set` with `update_topic` (no restart). | In the config file and `/api/config/raw`. | Half-written file possible. | Same Compose change for the tracked file. |
| C | Keep `deploy/.env` as today; the setup page only shows the values to copy. | As today. | Manual. | None. |
| (rejected) | Change Frigate's environment from the api. | — | — | Needs the Docker socket in the api: far too much privilege. |

## Decisions that were needed from the owner (answered above)
1. **Compose change** (Frigate is protected configuration): move Frigate's live `/config` off the tracked `deploy/frigate/config.yml` to an untracked directory or volume seeded from the template, and add the `frigate-secrets` volume (api read-write, Frigate read-only).
2. **Restart:** accept a Frigate restart (about a minute without live view and recording) each time the operator presses "Apply to Frigate".
3. **Approval rule:** only devices with an explicit "Use in Frigate" switch, and only after a successful connection test (`connected`)? Or allow untested devices with a warning?
4. **The fixed `entrance` camera** from `.env`: keep it alongside saved devices, or replace it once a saved device is applied?

## Planned steps once decided (each tested, committed and pushed on its own)
1. Pure renderer: template + approved devices → config text with placeholders only; safe camera names; tests prove no password ever appears in the output.
2. Secret-file writer: atomic writes, owner-only permissions, removal of files for devices no longer used; tests.
3. Migration and API: the "Use in Frigate" flag, validation, UI switch; tests.
4. `POST /api/frigate/apply` (sign-in required): render, stage, `config/save?save_option=restart`, verify cameras through `/api/config`, restore the previous config and secrets on any failure; error mapping; tests with a mocked Frigate.
5. Compose and setup documentation; real-stack verification stays pending until the Ubuntu host is available.
