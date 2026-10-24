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

SQLite runs with `journal_mode=WAL`, `busy_timeout=5000` ms and `foreign_keys=ON`.

## Files in the `api-data` volume
Inside the api container the volume is mounted at `/data`.
| File | What |
|---|---|
| `/data/app.db` | The database |
| `/data/app.db-wal`, `/data/app.db-shm` | SQLite WAL-mode companions. They exist while the api runs, or after an unclean stop. **Never delete them while anything uses the database:** they can hold committed data. |
| `/data/credentials.key` | Encrypts the stored camera/NVR passwords |
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
4. **In-use check** of the current `app.db`. Skipped if `app.db` does not exist.
   - A file that does not start with SQLite's 16-byte header (`SQLite format 3\0`) is **not opened through SQLite at all**, so its leftover side files are not touched. It counts as unreadable.
   - Otherwise the tool opens it and tries `BEGIN EXCLUSIVE` without waiting. A lock or busy error means another connection is reading or writing now: **refused** (exit `4`).
   - The tool closes its connection. SQLite's last connection removes `-wal`/`-shm` on a clean close. If **any `-wal`, `-shm` or `-journal` is still there afterwards, another connection is still open**, even an idle one holding no lock: **refused** (exit `4`). The other connection's WAL is left as it was.
   - Opening the file lets SQLite fold committed WAL content into `app.db` (a checkpoint). The data is unchanged; only where it is stored changes.
   - A file that passes is then checked with `PRAGMA quick_check`. A file whose header is valid but whose pages are damaged fails here and counts as **unreadable or damaged**.
5. **Unreadable or damaged `app.db`** (no header, SQLite error, or `quick_check` not `ok`): **refused** (exit `4`) unless `--replace-unreadable` is given. See the risks below.
6. **Independent copy of the old database** into a new directory `/data/pre-restore-<time>/`:
   - **readable `app.db`:** a separate file written with SQLite's backup API (reported as `"previous_database_preserved_by": "sqlite backup"`). It is **not a hard link**: later writes to `/data/app.db` never change it. It is opened with `immutable=1`, which is safe because step 4 showed no other connection and no side files. The copy is a single file, in `journal_mode=DELETE`.
   - **unreadable or damaged `app.db`:** a byte-for-byte copy with its timestamps (`shutil.copy2`, reported as `"file copy"`), kept for inspection.
   - `app.db` itself **stays in place** in `/data`.
   - Leftover `app.db-wal`, `app.db-shm` and `app.db-journal` are **moved** next to that copy. This can only happen when `app.db` is unreadable or missing, because a readable one with side files was refused in step 4. They are moved even when `app.db` itself is missing.
   - Nothing is deleted.
7. **Atomic swap:** `os.replace` renames the temporary copy onto `app.db` in one step. On POSIX the tool then fsyncs the `/data` directory (best effort, see the durability limits below).
8. **After the swap:** when run as root, the `pre-restore-<time>` directory is handed to the owner of `/data`; then the restored database is verified. Problems here are reported as warnings.

The temporary file is always removed, after success or failure. Two restores started in the same second get distinct names (`-2`, `-3`, ...).

#### `--replace-unreadable`: what it means and its risks
Use it only when the current database is really broken and you have stopped the api.
- The in-use check of step 4 needs SQLite to open the file. For an unreadable or damaged file, **the tool cannot reliably rule out that the api or another process still has it open.** With this flag it replaces the file anyway. If something still has it open, that process keeps writing to the old file, now replaced, and its writes are lost.
- The success output says so: `Note: the previous database was unreadable or damaged; it was replaced on request (--replace-unreadable), other users of it could not be reliably ruled out, and it was kept byte for byte for inspection.` The JSON shows `"previous_database_was_readable": false` and `"previous_database_preserved_by": "file copy"`.
- The damaged `app.db`, and any side files, are kept unchanged in `pre-restore-<time>/`. A file without a SQLite header is never opened through SQLite. A file with a valid header is opened by the in-use check, so SQLite may fold its WAL into it before it is copied.

#### If something fails
- **Steps 1–6, or the rename in step 7, fail:** everything done so far is undone. Moved side files are put back. The snapshot copy is removed together with the then-empty `pre-restore-<time>` directory. `app.db` was never moved or written by steps 5–7. Its content is unchanged, though step 4 may have checkpointed its WAL. Exit `4` (refused) or `5` (unexpected error), both saying "active database unchanged".
- **A rollback step itself fails** (rare, e.g. permissions): nothing is guessed. The command exits `6` with `State uncertain: ...` and names every file still in `pre-restore-<time>/`. In this case `app.db` is still the previous database, but some of its side files are in that directory, and the snapshot is kept beside them. **Do not start the api until they are back in `/data`.** The original error is part of the message.
- **Step 8 problems** happen after the restore has taken effect. They are reported as warnings (exit `3`, `Restored, with warnings: ...`), never as "not done".

#### Messages and exit codes
| Code | Message on stderr | Meaning |
|---|---|---|
| `0` | (JSON only; plus a `Note:` line after `--replace-unreadable`) | Restored and verified, no warnings |
| `2` | `Restore replaces the database. Stop the api first ...` or `db restore needs a file path` | Usage error. Nothing done. |
| `3` | `Restored, with warnings: ... The previous database is in pre-restore-<time>.` (or `There was no previous database.`) | **Restored**. Read the warnings. |
| `4` | `Not done (active database unchanged): ...` | Refused: bad backup, self-restore, database in use, or unreadable/damaged `app.db` without `--replace-unreadable` |
| `5` | `Not done (active database unchanged): <ErrorType>: ...` | Unexpected I/O or SQLite error before the swap, fully rolled back |
| `6` | `State uncertain: ... Do not start the api until these are back in place: ...` | A rollback step failed. The message lists each affected file and where it is now. |

`db backup` removes its own partial file if it fails (exit `5`, `Not done (no backup written)`). `db verify` exits `4` when the file is not ok.

#### Durability and crash limits
- **Not crash-atomic across files.** The only atomic step is the single rename of one file onto `app.db`. The restore as a whole involves several files (the temporary copy, the snapshot, moved side files), and **no claim is made that the whole operation is crash-atomic across multiple SQLite files.** What holds is narrower: `app.db` is always either the complete old file or the complete new file, and a cut-off before the rename leaves the old one in use.
- **Directory fsync** after the rename is done on POSIX only, as best effort; errors are ignored. Windows has no equivalent, so it is skipped there. The temporary copy and the `sqlite backup` snapshot are written by SQLite, which syncs them at commit. The `file copy` of a damaged database is a plain copy and is not fsynced.
- **Tested:** a process killed hard (`os._exit`) just before the rename, on Windows. **Not tested:** power loss, kernel crash, or a crash on Linux. After a power loss the filesystem decides whether a rename that had just happened survives. The tool cannot guarantee it.
- If `/data/app.db` is ever missing, the api creates a new **empty** database at startup (see the note below).

#### Known limits of the in-use check
- **Idle-connection detection relies on WAL mode.** The api always uses WAL. A process using the file in rollback-journal mode while idle leaves no side file and holds no lock, so the tool cannot see it. Stopping the api, as the procedure says, is still required.
- The idle-connection test ran on Windows only; it has **not been run on Linux** here. SQLite's close-time cleanup is the same mechanism on both, but that is unverified on Linux in this project.

### After an interruption (crash, power loss, killed container)
A restore cut off before the rename in step 7 **leaves the old `app.db` in place and usable**. The api starts on it normally; this was tested with a killed process. You may find:
| You see | Meaning | What to do |
|---|---|---|
| `/data/app.db.restore-<time>.tmp` | The verified copy, never activated | Remove it, or run the restore again. |
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
5. **Credentials key:** restore `credentials.key` only if it was lost or replaced. Without the matching key, stored device passwords cannot be decrypted; re-enter them on the Setup page.
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
  - the preserved copy is an independent file (not a hard link)
  - file names: `#`, space, a literal `%25` and non-ASCII (`ü`) restore the named file (run on Windows); `?` is skipped on Windows, where it is not a legal file-name character; percent-encoding of `#`, `?`, `%` and space is also unit-tested as a string
  - a symlink to the active database refused as a self-restore: **skipped on this Windows PC** (creating symlinks needs a privilege not granted here)
  - `app.db` present at the moment of the swap
  - a killed process just before the swap: the old database stays in place, the api starts on it, the preserved copy is unaffected by later writes, and restoring that copy works
  - an active database left untouched after a copy, verify, snapshot, move or rename failure
  - a failed rollback reporting uncertain paths, with the original error kept
  - warnings, not errors, after the swap
  - two restores in the same second
  - the restored file opening normally in the api
  - CLI exit codes 0 / 3 / 4 / 5 / 6, with truthful messages and no tracebacks
- Not covered by tests: power loss, any run on Linux (including the idle-connection and symlink cases), and a process using the database in rollback-journal mode.
- These tests always use temporary databases. Never point tests at a real database.
