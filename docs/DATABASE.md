# Database: migrations and backups

The api uses SQLite at `/data/app.db` inside the `api-data` Docker volume, and manages its schema with **Alembic**. Migrations live in `backend/ai_surveillance/migrations/`.

## What happens at api startup
The api runs `upgrade_to_head` automatically. The result depends on the database:

| Database found | What happens |
|---|---|
| None / empty | Created from the migrations (`created`) |
| Already under Alembic, at the latest revision | Nothing (`already at head`) |
| Under Alembic, older revision | A backup copy is written, then it is upgraded (`upgraded`) |
| **Created before migrations existed** (no `alembic_version` table) | Compared with revision `0001_baseline`: tables, column names, type family (text / integer / datetime; VARCHAR lengths are ignored because SQLite does not enforce them), nullability, primary keys, the UNIQUE columns `admin_users.username` and `devices.name`, and the foreign key `login_sessions.user_id → admin_users.id`. **Only a match** is backed up and stamped as the baseline, then upgraded. Otherwise startup stops with the differences listed (table, column and type names only, no data), and **nothing is changed**. |
| Contains none of this application's tables | Refused; nothing is changed |

`0001_baseline` creates exactly what the pre-migration code created with SQLAlchemy `create_all`: `admin_users`, `login_sessions`, `captures`, `devices`. This was checked against a real pre-migration database file. Phase 1 adds no business tables.

Later revisions:
| Revision | Change | Downgrade |
|---|---|---|
| `0002_device_use_in_frigate` | `devices.use_in_frigate` (boolean, not null, default false): the operator's "Use in Frigate" approval (ADR-0003). Existing devices start switched off. | Drops the column; device rows are kept |
| `0003_face_events` | Tables `face_events` (one row per Frigate person event: unique `frigate_event_id`, camera, start/end, status, face count, frame source/time/file/size, detector, detail) and `face_detections` (box, detection score, keypoints and quality as JSON, crop file; deleted with their event). See [FACE-EVENTS.md](FACE-EVENTS.md). | Drops both tables; the image files under `media/faces/` stay on disk and must be removed by hand if wanted |
| `0004_gallery` | Tables `people` (display name, notes, consent date and note, active, created by/at, updated at), `reference_faces` (encrypted image file, media type, image size, face box, detection score, keypoints and quality as JSON, detector, created by/at; deleted with their person) and `audit_log` (time, username, action, target type and id, detail; no foreign keys, so entries outlive what they describe). See [GALLERY.md](GALLERY.md). | Drops the three tables; the encrypted files under `media/gallery/` stay on disk and must be removed by hand if wanted |

SQLite runs with `journal_mode=WAL`, `busy_timeout=5000` ms and `foreign_keys=ON`.

## Files in the `api-data` volume
Inside the api container the volume is mounted at `/data`.
| File | What |
|---|---|
| `/data/app.db` | The database |
| `/data/app.db-wal`, `/data/app.db-shm` | SQLite WAL-mode companions. They exist while the api runs, or after an unclean stop. **Never delete them while anything uses the database:** they can hold committed data. |
| `/data/credentials.key` | Encrypts the stored camera/NVR passwords and the gallery's reference images |
| `/data/media/gallery/` | Encrypted reference images ([GALLERY.md](GALLERY.md)). **Not included in `db backup`:** copy it together with the database and the key |
| `/data/app.db.pre-<rev>-<time>.bak` | Automatic copy taken before each migration |
| `/data/app.db.manual-<time>.bak` | Copies you make with `ai-surveillance db backup` |
| `/data/pre-restore-<time>/` | An independent copy of the previous database made by a restore, plus any leftover side files of an unreadable one |

## Commands
All of them run inside the api image. Use `exec` while the api is running, or `run --rm --no-deps` while it is stopped.
| Command | What it does |
|---|---|
| `ai-surveillance db status` | Schema revision and legacy-schema check (read-only) |
| `ai-surveillance db upgrade` | Backs up, then migrates (the api does this at startup anyway) |
| `ai-surveillance db backup [DIR]` | Consistent copy via SQLite's online backup API, **safe while the api runs**, then an integrity check of the copy |
| `ai-surveillance db verify FILE` | `PRAGMA integrity_check` plus a check for this application's tables, opened read-only (details below). Exit `0` if ok, `4` if not. |
| `ai-surveillance db restore FILE --yes [--replace-unreadable]` | Replaces the database with a verified copy of FILE (details below) |

### What `db verify` writes
- **FILE has no `-wal`, `-shm` or `-journal` next to it** (a stopped database, or any backup made by these tools): it is opened with SQLite's `immutable=1`. SQLite then writes **nothing at all**: no side files, no change to FILE. This relies on nothing else writing to FILE during the check. A WAL-mode database without side files has no open connection, so that holds as long as nothing is started on it meanwhile.
- **FILE has side files:** it is opened read-only in the normal way (`mode=ro`), so committed data still in the `-wal` is included in the check. The `-wal` content is not checkpointed or removed. SQLite may update the `-shm` index, which holds no data.

## Automatic backups
Before stamping or upgrading an existing SQLite file, the api writes a consistent copy using the online backup API, which is safe in WAL mode:
```
/data/app.db.pre-<revision>-<UTC time>.bak      e.g. app.db.pre-stamp-20261023T171500Z.bak
```

## Making a backup (safe while running)
Run these in `~/ai-surveillance/deploy`:
```bash
mkdir -p backups && chmod 700 backups
docker compose exec api ai-surveillance db backup            # prints "backup": "/data/app.db.manual-<time>.bak"
docker compose cp api:/data/app.db.manual-<time>.bak backups/
docker compose cp api:/data/credentials.key backups/credentials.key
chmod 600 backups/*
```
- **Do not** copy `/data/app.db` with `cp` or `docker compose cp` while the api runs. Recent changes may still be only in `app.db-wal`, so a plain file copy can be incomplete or inconsistent. `db backup` avoids this.
- **Validate the off-machine copy.** On the Ubuntu host, `python3` is preinstalled:
  ```bash
  python3 -c "import sqlite3,sys; print(sqlite3.connect('file:'+sys.argv[1]+'?mode=ro', uri=True).execute('PRAGMA integrity_check').fetchone()[0])" backups/app.db.manual-<time>.bak
  ```
  It must print `ok`.

## Upgrading an existing installation
1. **Make a backup** as above.
2. **Update the code:** `git pull --ff-only`
3. **Optional, read-only:** see what the migration will do, without starting the api:
   ```bash
   docker compose build api
   docker compose run --rm --no-deps api ai-surveillance db status
   ```
   - `"legacy, matches baseline"` or `"upgrade needed"` is fine.
   - `"legacy, DOES NOT match baseline"` lists the differences. Stop there and ask for help.
4. **Start:** `docker compose up -d --build`. The api migrates on startup. Check the logs:
   ```bash
   docker compose logs api | grep -iE "schema|stamped|backed up|migration"
   ```
5. **If the api refuses to start** with "does not match the baseline", nothing was changed. Keep the logged differences and do not delete anything.

## Restoring a backup (WAL-safe)
`db restore` does the following, in this exact order. **`/data/app.db` exists at every moment**: it is the old database until the single rename in step 7, and the restored one after it.

1. **Self-restore check.** Refuses if FILE *is* the active database, under any spelling of the path (`resolve()` plus `os.path.samefile`, which also covers hard links and symlinks).
2. **Verify FILE** (as `db verify`, above). A file that is not an intact database of this application is refused.
3. **Temporary copy.** FILE is copied with SQLite's online backup API into `/data/app.db.restore-<time>.tmp`, next to `app.db` and so on the same filesystem. The copy is switched to `journal_mode=DELETE`, so it is one self-contained file with no `-wal`/`-shm`. The copy is verified again. When run as root, it is handed to the owner of `/data`.
4. **Integrity check, then in-use check** of the current `app.db`. Skipped if `app.db` does not exist.
   - A file that does not start with SQLite's 16-byte header (`SQLite format 3\0`) is **not opened through SQLite at all**. It counts as unreadable.
   - Otherwise `PRAGMA quick_check` runs **without changing any file**. With no side files, `app.db` is read as `immutable=1`. With side files, the check runs on a scratch copy of `app.db` and its `-wal`/`-journal`, in `/data/app.db.check-<time>/`, which is removed afterwards. A file whose header is valid but whose pages are damaged fails here and counts as **unreadable or damaged**.
   - **Consistency of the check:** the size and modification time of `app.db`, `-wal`, `-shm` and `-journal` are recorded before and after it. If any of them changed, or a side file disappeared during the copy, something is using the database: **refused** (exit `4`, "changed while being checked"), even with `--replace-unreadable`. This catches writes that change size or modification time. It cannot prove that a copy taken during a write is consistent, so stopping the api is still required.
   - If the scratch directory cannot be removed, it is left in place and its path is reported (a `Note:` in the output). The originals were not touched.
   - Only an intact file is then opened for writing. The tool tries `BEGIN EXCLUSIVE` without waiting. A lock or busy error means another connection is reading or writing now: **refused** (exit `4`).
   - The tool closes its connection. SQLite's last connection removes `-wal`/`-shm` on a clean close. If **any `-wal`, `-shm` or `-journal` is still there afterwards, another connection is still open**, even an idle one holding no lock: **refused** (exit `4`). The other connection's WAL is left as it was.
   - For an intact file, closing that connection lets SQLite fold committed WAL content into `app.db` (a checkpoint). The data is unchanged; only where it is stored changes.
   - An unreadable or damaged file is **never opened for writing**, so it and its `-wal`/`-shm` stay exactly as found, whether the restore is refused or goes ahead.
5. **Unreadable or damaged `app.db`** (no header, SQLite error, or `quick_check` not `ok`): **refused** (exit `4`) unless `--replace-unreadable` is given. See the risks below.
6. **Independent copy of the old database** into a new directory `/data/pre-restore-<time>/`:
   - **readable `app.db`:** a separate file written with SQLite's backup API (reported as `"previous_database_preserved_by": "sqlite backup"`). It is **not a hard link**: later writes to `/data/app.db` never change it. It is opened with `immutable=1`, which is safe because step 4 showed no other connection and no side files. The copy is a single file, in `journal_mode=DELETE`.
   - **unreadable or damaged `app.db`:** a byte-for-byte copy, reported as `"file copy"` and kept for inspection. The bytes go into a new file, which is flushed; only then are the original's permissions and timestamps applied to it. This way a read-only `app.db` can be copied and flushed too.
   - `app.db` itself **stays in place** in `/data`.
   - Leftover `app.db-wal`, `app.db-shm` and `app.db-journal` are **moved** next to that copy. This can only happen when `app.db` is unreadable or missing, because a readable one with side files was refused in step 4. They are moved even when `app.db` itself is missing.
   - Nothing is deleted.
   - The copy's contents are **flushed to the device** (`fsync`) right after it is written, both the `sqlite backup` and the `file copy`. If that flush fails, the restore stops and is rolled back (exit `5`): a recovery copy that may not be on disk is not relied on.
7. **Flush, then atomic swap:** the tool fsyncs the temporary copy's contents, then the `pre-restore-<time>` directory and `/data` (directory entries; POSIX only, best effort). Then `os.replace` renames the temporary copy onto `app.db` in one step, and `/data` is fsynced again. See the durability limits below.
8. **After the swap:** when run as root, the `pre-restore-<time>` directory is handed to the owner of `/data`; then the restored database is verified. Problems here are reported as warnings.

The temporary file is always removed, after success or failure. Two restores started in the same second get distinct names (`-2`, `-3`, ...).

#### `--replace-unreadable`: what it means and its risks
Use it only when the current database is really broken and you have stopped the api.
- The in-use check of step 4 needs SQLite to open the file. For an unreadable or damaged file, **the tool cannot reliably rule out that the api or another process still has it open.** With this flag it replaces the file anyway. If something still has it open, that process keeps writing to the old file, now replaced, and its writes are lost.
- The success output says so: `Note: the previous database was unreadable or damaged; it was replaced on request (--replace-unreadable), other users of it could not be reliably ruled out, and it was kept byte for byte for inspection.` The JSON shows `"previous_database_was_readable": false` and `"previous_database_preserved_by": "file copy"`.
- The damaged `app.db`, and any `-wal`, `-shm` and `-journal`, are kept byte for byte in `pre-restore-<time>/`: `app.db` is copied, the side files are moved. None of them is opened for writing before that. A damaged file is only read, as `immutable=1` or through a scratch copy.

#### If something fails
- **Steps 1–6, or the rename in step 7, fail:** everything done so far is undone. Moved side files are put back. The snapshot copy is removed together with the then-empty `pre-restore-<time>` directory. `app.db` was never moved or written by steps 5–7. Its content is unchanged. For an intact `app.db`, step 4 may have checkpointed its WAL. An unreadable or damaged `app.db` and its side files are byte-for-byte unchanged. Exit `4` (refused) or `5` (unexpected error), both saying "active database unchanged".
- **A rollback step itself fails** (rare, e.g. permissions): nothing is guessed. The command exits `6` with `State uncertain: ...` and lists each affected file with its full location. `app.db` is still the previous database, but some of its side files are in `pre-restore-<time>/`. The snapshot is **never deleted** in this case; it stays beside them. **Do not start the api.** With the api stopped, move each listed side file back into `/data`. The original error is part of the message.
- **A side file reappears during the restore** (a new `app.db-wal`, for example, because something opened the database meanwhile): the moved one is **not** put back, because that would overwrite the newer file. It is listed as `kept in pre-restore-<time>, not moved back because a new app.db-wal appeared ... keep both and do not overwrite either`, with exit `6`, and the snapshot is kept. Both WAL files may hold data. Stop everything using the database and ask for help before deciding which to keep.
- **Step 8 problems** happen after the restore has taken effect. They are reported as warnings (exit `3`, `Restored, with warnings: ...`), never as "not done".

#### Ctrl-C (interruption)
- **Before the rename:** everything done so far is rolled back, as for any failure. Exit `130`, `Not done (active database unchanged; everything done so far was rolled back): interrupted (Ctrl-C).`
- **During the rollback:** exit `6`, `State uncertain: ... the rollback was interrupted (Ctrl-C)`, followed by every file in `pre-restore-<time>/` and every moved side file still in `/data`.
- **After the rename** (including the instant right after it and the cleanup of the temporary file): nothing is rolled back, because putting the old side files back next to the new database would be wrong. Whether the rename happened is decided from the files themselves: `app.db` must carry the temporary file's identity (device and file id; on a filesystem without file ids, the temporary file must be gone). A temporary file that vanished for another reason before the rename is therefore not mistaken for a completed restore.
- **During the cleanup after a failed rollback:** the `State uncertain` report (exit `6`) is kept, with `interrupted (Ctrl-C) while cleaning up` added. The interruption does not hide it. Exit `3`, `Interrupted: interrupted after the restored database replaced app.db; the final checks did not finish, so it is not verified. The previous database is in <path>. Run 'ai-surveillance db verify /data/app.db' before starting the api.`
- **While the result is being printed** (after the restore or backup has finished; for example a Ctrl-C, or output piped into `head` that closes early): the command does not claim that nothing happened. A restore says `Restored: the restored database is active as app.db and verified, but printing the result was interrupted (...)`, with the `pre-restore` path and any warnings, and exits `3`. A backup says `Backup written and verified: <path>, but printing the result was interrupted (...)` and exits `3`. `db verify` says `Nothing changed: verify finished, but printing its result was interrupted` and exits `130` (Ctrl-C) or `5`. If stderr is gone too, the exit code alone still tells the truth.
- **A narrow window that is not covered:** Python delivers a real Ctrl-C between any two steps of the program. If one arrives in the instant after `dbtools.restore()` has returned (the restore has finished) but before the CLI starts reporting the result, no handler is active. The command then ends with a Python traceback ending in `KeyboardInterrupt`, not with one of the messages above. It does **not** print `Not done (active database unchanged)` or any other claim that nothing happened. On POSIX the shell may still see exit status `130`, the usual status for an uncaught Ctrl-C; the traceback, not a `Not done` line, is what tells this case apart. If you see such a traceback after a restore, assume the restore may have taken effect: check for a new `pre-restore-<time>/` directory and run `ai-surveillance db verify /data/app.db` before starting the api. This window was found by reading the code; it is not exercised by a test, because a test cannot send a signal at that exact instant.
- Other unexpected errors are not caught; they show a traceback as before.

#### Messages and exit codes
| Code | Message on stderr | Meaning |
|---|---|---|
| `0` | (JSON only; plus a `Note:` line after `--replace-unreadable`) | Restored and verified, no warnings |
| `2` | `Restore replaces the database. Stop the api first ...` or `db restore needs a file path` | Usage error. Nothing done. |
| `3` | `Restored, with warnings: ... The previous database is in pre-restore-<time>.` (or `There was no previous database.`), `Interrupted: ... not verified ...`, or `Restored: ... but printing the result was interrupted` (also used by `db backup`: `Backup written and verified: ...`) | **Done**, but read the message. After `Interrupted:`, run `db verify` first. |
| `4` | `Not done (active database unchanged): ...` | Refused: bad backup, self-restore, database in use or changed during the check, or unreadable/damaged `app.db` without `--replace-unreadable` |
| `5` | `Not done (active database unchanged): <ErrorType>: ...` | Unexpected I/O or SQLite error before the swap (including a failed flush of the recovery copy), fully rolled back |
| `6` | `State uncertain: ... Do not start the api. Files to check: ...` | A rollback step failed or was interrupted, or a side file reappeared. The message lists each affected file and where it is now. |
| `130` | `Not done (...): interrupted (Ctrl-C).` | Ctrl-C before the swap, fully rolled back (restore); no backup written (backup) |

`db backup` removes its own partial file if it fails (exit `5`, `Not done (no backup written)`), and exits `3` when the backup was written but printing its result was interrupted. `db verify` exits `4` when the file is not ok.

#### Durability and crash limits
- **Not crash-atomic across files.** The only atomic step is the single rename of one file onto `app.db`. The restore as a whole involves several files (the temporary copy, the snapshot, moved side files), and **no claim is made that the whole operation is crash-atomic across multiple SQLite files.** What holds is narrower: `app.db` is always either the complete old file or the complete new file, and a cut-off before the rename leaves the old one in use.
- **File contents** of the recovery copy (both kinds) and of the temporary copy are fsynced before the rename. A failed flush stops the restore. On Windows, `os.fsync` maps to `_commit`, which flushes the file's buffers.
- **Directory entries** (the new `pre-restore-<time>` directory, the moved side files and the rename) are fsynced on POSIX only, as best effort. Errors are ignored there, because some filesystems reject fsync on directories. Windows cannot fsync a directory, so it is skipped. So on Windows, and on such filesystems, a power loss right after the restore could lose the new directory entries even though the file contents were flushed.
- **Tested:** a process killed hard (`os._exit`) just before the rename, on Windows. The order of the flushes is checked with recorded calls, and a failing flush is checked to roll back. **Not tested:** power loss, kernel crash, or a crash on Linux. These tests do not simulate a power loss. After a power loss the filesystem and the storage device decide what survives; the tool cannot guarantee it.
- If `/data/app.db` is ever missing, the api creates a new **empty** database at startup (see the note below).

#### Read-only `app.db`
- **Linux:** a rename may replace a read-only file when `/data` itself is writable, so a read-only damaged `app.db` can be replaced with `--replace-unreadable`. **Not run here** (no Linux).
- **Windows:** the operating system refuses to rename over a read-only file. The restore stops at the rename and rolls back: exit `5`, `Not done (active database unchanged): PermissionError: ...`, and nothing is left behind (tested). Clear the read-only attribute of `app.db` yourself first if you want to replace it.

#### Known limits of the in-use check
- **Idle-connection detection relies on WAL mode.** The api always uses WAL. A process using the file in rollback-journal mode while idle leaves no side file and holds no lock, so the tool cannot see it. Stopping the api, as the procedure says, is still required.
- The idle-connection test ran on Windows only; it has **not been run on Linux** here. SQLite's close-time cleanup is the same mechanism on both, but that is unverified on Linux in this project.

### After an interruption (crash, power loss, killed container)
A restore cut off before the rename in step 7 **leaves the old `app.db` in place and usable**. The api starts on it normally; this was tested with a killed process. You may find:
| You see | Meaning | What to do |
|---|---|---|
| `/data/app.db.restore-<time>.tmp` | The verified copy, never activated | Remove it, or run the restore again. |
| `/data/app.db.check-<time>/` | Scratch copy from the integrity check of step 4 | Remove it. It is a copy only; the originals were not touched. |
| `/data/pre-restore-<time>/app.db` | An independent copy of the old database, complete or partial | Run `db verify` on it. If it is ok, it is a valid fallback. If not, it was cut off mid-write; the old `app.db` is still in `/data`. |
| `-wal` / `-shm` / `-journal` in `pre-restore-<time>/`, missing from `/data` | Only possible when the old `app.db` was unreadable or missing: side files moved just before the cut-off | They belong to the damaged file next to them. Keep them together for inspection. |
| Neither | The cut-off happened before step 6 | Nothing to clean up. |

To go back to the database as it was before a restore, restore its preserved copy. This is a normal restore of a separate file, not a self-restore:
```bash
docker compose run --rm --no-deps api ai-surveillance db verify /data/pre-restore-<time>/app.db
docker compose run --rm --no-deps api ai-surveillance db restore /data/pre-restore-<time>/app.db --yes
```
This recovery path is covered by the killed-process test, which restores the `pre-restore` copy afterwards.

**Note:** if `/data/app.db` is ever missing, the api creates a new **empty** database at startup. The restore tool never leaves it missing. If you find it missing for another reason (manual deletion, disk error), do not start the api: restore from `pre-restore-*` or a backup first.

**Procedure** (in `~/ai-surveillance/deploy`):
1. **Stop everything that uses the database.** The api is the only long-running user. Then make sure no one-off api containers are still running:
   ```bash
   docker compose stop api
   docker compose ps -a     # the api container must show "exited"; no other api containers "running"
   ```
2. **Validate the backup first** (read-only). For a file on the host, use the `python3` command above. For an automatic backup inside the volume:
   ```bash
   docker compose run --rm --no-deps api ai-surveillance db verify /data/app.db.pre-stamp-<time>.bak
   ```
3. **Restore.** For a file from the host, mount it read-only into a one-off container. `--user root` lets the container read your `chmod 600` file; the tool then hands the restored database to the api user.
   ```bash
   docker compose run --rm --no-deps --user root \
     -v "$PWD/backups/app.db.manual-<time>.bak:/restore/app.db.bak:ro" \
     api ai-surveillance db restore /restore/app.db.bak --yes
   ```
   For a backup already in the volume:
   ```bash
   docker compose run --rm --no-deps api ai-surveillance db restore /data/app.db.pre-stamp-<time>.bak --yes
   ```
   - **Exit `0`:** the output shows `"restored": true`, `"integrity": "ok"`, an empty `"warnings"` list, and the `pre-restore-<time>` directory holding the previous files.
   - **Exit `3`:** restored with warnings; read them.
   - **Exit `4`:** refused, nothing changed. If it says the database is unreadable or damaged, read the `--replace-unreadable` section above before repeating with that flag.
   - **Exit `6`:** see the exit-code table above before going further.
4. **Start and check:**
   ```bash
   docker compose start api
   docker compose logs api | grep -iE "schema|stamped|migration"
   docker compose exec api ai-surveillance db verify /data/app.db
   ```
   Sign in to the website, then check the cameras and the Camera & NVR Setup page.
5. **Credentials key:** restore `credentials.key` only if it was lost or replaced. Without the matching key, stored device passwords cannot be decrypted (re-enter them on the Setup page) and gallery reference images cannot be decrypted (enroll them again).
   ```bash
   docker compose cp backups/credentials.key api:/data/credentials.key
   ```
   Do this while the api is stopped. Afterwards, run `docker compose run --rm --no-deps --user root api chown 10001:10001 /data/credentials.key`.

## Verification checklist
Before restoring:
- [ ] The backup file passes `integrity_check` (`ok`) and `db verify` shows `"ok": true`.
- [ ] `docker compose ps -a` shows the api stopped, and no other api container running.
- [ ] You have a second copy of the current database somewhere, for example a fresh `db backup` taken before stopping.

After restoring:
- [ ] `db restore` exited `0`, and its output shows `"restored": true`, `"integrity": "ok"` and no warnings.
- [ ] No `/data/app.db.restore-*.tmp` file is left over.
- [ ] `/data/pre-restore-<time>/` exists and contains the previous `app.db`.
- [ ] The api starts; its log shows the expected schema revision; `db verify /data/app.db` is `ok`.
- [ ] Sign-in works, and the cameras and devices are as expected.
- [ ] Only after all of the above: decide whether to keep or remove the `pre-restore-<time>` directory.

## Backup retention
Backups are **never deleted automatically**. Suggested practice:
- **Off the machine:** keep the newest **verified** backup plus `credentials.key`, and at least one older one.
- **In the volume:** keep the latest `pre-*` backup from each migration and the last 2–3 `manual-*` backups. Remove older ones only **after** verifying the newest one:
  ```bash
  docker compose exec api sh -c 'ls -lt /data/*.bak'
  docker compose exec api ai-surveillance db verify /data/<newest>.bak      # must report "ok": true
  docker compose exec api rm /data/<an-older>.bak
  ```
- Never remove the only backup you have, and never remove `app.db-wal` or `app.db-shm`.
- Check free space now and then: `docker system df -v | grep api-data`.

## For developers
- Never edit a released migration. Add a new revision under `migrations/versions/` with `revision`/`down_revision` set.
- `tests/unit/test_migrations.py` fails when the SQLAlchemy models and the migrations drift apart (it uses autogenerate comparison).
- `tests/unit/test_dbtools.py` covers:
  - backup with data still in the WAL, and no partial file left on failure
  - verify: read-only (no file created next to a side-file-free database), and still reading an existing WAL without checkpointing it
  - restore with stale and committed WAL files, and side files when `app.db` is missing
  - the refusals: in use (lock held), **idle open connection** (side files left after close), self-restore
  - an unreadable `app.db` (no header) and a **damaged `app.db` with a valid header**: both refused without `--replace-unreadable`, both replaced and kept byte for byte with it, with a note that other users could not be ruled out
  - a **damaged `app.db` with a `-wal` and `-shm`**: a refusal leaves all three byte for byte unchanged, and a replacement keeps all three byte for byte; an intact `app.db` with leftover side files restores normally and leaves no scratch copy
  - the preserved copy is an independent file (not a hard link)
  - file names: `#`, space, a literal `%25` and non-ASCII (`ü`) restore the named file (run on Windows); `?` is skipped on Windows, where it is not a legal file-name character; percent-encoding of `#`, `?`, `%` and space is also unit-tested as a string
  - a symlink to the active database refused as a self-restore: **skipped on this Windows PC** (creating symlinks needs a privilege not granted here)
  - `app.db` present at the moment of the swap
  - a killed process just before the swap: the old database stays in place, the api starts on it, the preserved copy is unaffected by later writes, and restoring that copy works
  - an active database left untouched after a copy, verify, snapshot, move or rename failure
  - a failed rollback reporting uncertain paths, with the original error kept
  - a side file that reappears during the restore: never overwritten, reported (exit `6`), snapshot and moved file kept
  - flushes: the recovery copy (both kinds), the temporary copy and the directories flushed before the rename (call order recorded; no power loss simulated); a failing flush rolls back (exit `5`)
  - Ctrl-C at five points: before anything changed (`130`), during the snapshot (`130`, rolled back), during the rollback (`6`), right after the rename (`3`, no rollback, old WAL not put back), during the final checks (`3`)
  - Ctrl-C in the cleanup of the temporary file: after the rename (`3`, restored, not "unchanged"), after a failed rollback (`6` kept), after a failed rename (`130`); a temporary file that vanished before the rename (`5`, not mistaken for a restore); file identity kept across a rename
  - the CLI's output failing after the operation finished (Ctrl-C or broken pipe): restore `3` "Restored", with warnings repeated; backup `3` "Backup written"; verify `130` / `5` "Nothing changed"; stderr gone too; a failure before the swap still says "unchanged"
  - a read-only damaged `app.db`: its byte copy is flushed while writable and then gets the original's permissions and timestamps; on Windows the restore is refused cleanly at the rename (exit `5`); a rollback removes the read-only copy. The Linux replacement test is **skipped here** (Windows).
  - the integrity check refusing when files change during it, reporting a scratch copy it could not remove, and leaving a `-journal` untouched
  - warnings, not errors, after the swap
  - two restores in the same second
  - the restored file opening normally in the api
  - CLI exit codes 0 / 3 / 4 / 5 / 6, with truthful messages and no tracebacks
- Not covered by tests: power loss, any run on Linux (including the idle-connection, symlink, `?` file-name and directory-fsync cases), and a process using the database in rollback-journal mode. A real Ctrl-C signal is not sent; the tests raise `KeyboardInterrupt` at chosen points.
- These tests always use temporary databases. Never point tests at a real database.
