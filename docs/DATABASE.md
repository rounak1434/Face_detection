# Database: migrations and backups

The api uses SQLite at `/data/app.db` inside the `api-data` Docker volume, and manages its schema with **Alembic**. Migrations live in `backend/ai_surveillance/migrations/`.

## What happens at api startup
The api runs `upgrade_to_head` automatically. The result depends on the database:

| Database found | What happens |
|---|---|
| None / empty | Created from the migrations (`created`) |
| Already under Alembic, at the latest revision | Nothing (`already at head`) |
| Under Alembic, older revision | A backup copy is written, then it is upgraded (`upgraded`) |
| **Created before migrations existed** (no `alembic_version` table) | Every baseline table **and column** is compared with revision `0001_baseline`. **Only an exact match** is backed up and stamped as the baseline, then upgraded. Otherwise startup stops with the difference listed, and **nothing is changed**. |
| Contains none of this application's tables | Refused; nothing is changed |

`0001_baseline` creates exactly what the pre-migration code created with SQLAlchemy `create_all`: `admin_users`, `login_sessions`, `captures`, `devices`. This was checked against a real pre-migration database file. Phase 1 adds no business tables.

SQLite runs with `journal_mode=WAL`, `busy_timeout=5000` ms and `foreign_keys=ON`.

## Automatic backups
Before stamping or upgrading an existing SQLite file, the api writes a consistent copy using SQLite's online backup API, which is safe with WAL:
```
/data/app.db.pre-<revision>-<UTC time>.bak      e.g. app.db.pre-stamp-20261023T171500Z.bak
```
Backups are **not deleted automatically**. List them and remove old ones yourself:
```bash
docker compose exec api sh -c 'ls -l /data/*.bak'
```

## Recommended procedure on an existing installation
Run these in `~/ai-surveillance/deploy`.
1. **Make your own backup first.** Copy the database and the credentials key out of the volume:
   ```bash
   docker compose exec api python -c "import sqlite3; s=sqlite3.connect('/data/app.db'); d=sqlite3.connect('/data/app.db.manual.bak'); s.backup(d); d.close()"
   docker compose cp api:/data/app.db.manual.bak ./app.db.manual.bak
   docker compose cp api:/data/credentials.key ./credentials.key.backup && chmod 600 app.db.manual.bak credentials.key.backup
   ```
2. **Update the code:** `git pull --ff-only`
3. **Optional, read-only:** check what the migration will do, using the new image without starting the api:
   ```bash
   docker compose build api
   docker compose run --rm --no-deps api ai-surveillance db status
   ```
   `"state": "legacy, matches baseline"` or `"upgrade needed"` is fine. `"legacy, DOES NOT match baseline"` lists the differences; stop there and ask for help.
4. **Start:** `docker compose up -d --build`. The api migrates on startup. Check the logs:
   ```bash
   docker compose logs api | grep -iE "schema|stamped|backed up|migration"
   ```
5. **If the api refuses to start** with "does not match the baseline", nothing was changed. Restore nothing; report the logged differences.

The same steps work explicitly with `docker compose run --rm --no-deps api ai-surveillance db upgrade`.

## Restoring a backup
Stop the api, put the copy back into the volume, then start it:
```bash
docker compose stop api
docker compose cp ./app.db.manual.bak api:/data/app.db
docker compose start api
```
`docker compose cp` works on stopped containers. Restore `credentials.key` the same way if it was lost, otherwise stored device passwords cannot be decrypted.

## For developers
- Never edit a released migration. Add a new revision under `migrations/versions/` with `revision`/`down_revision` set.
- `tests/unit/test_migrations.py` fails when the SQLAlchemy models and the migrations drift apart (it uses autogenerate comparison).
- These tests always use temporary databases. Never point tests at a real database.
