"""SQLite backup, verification and restore (WAL-safe).

Used by `ai-surveillance db backup|verify|restore`. Copies are made with
SQLite's online backup API, so they are consistent even while the api is
running in WAL mode, and are written as standalone single-file databases.

A restore never leaves the active database missing, and keeps an
independent copy of the old one:
1. the backup is copied into a temporary file next to app.db and verified;
2. app.db must not be in use: no lock held, and no -wal/-shm/-journal left
   after our own connection closes (their presence means another
   connection is open). An unreadable or damaged app.db (no SQLite header,
   or PRAGMA quick_check not "ok") needs explicit consent;
3. an independent snapshot of the old app.db is written to
   pre-restore-<time>/ (app.db itself stays in place); leftover side files,
   possible only when app.db is unreadable or missing, are moved there;
4. the verified copy replaces app.db with one atomic rename.
At every moment app.db exists (old or new). Failures before step 4 are
rolled back; if a rollback step itself fails, RestoreStateUncertain lists
every affected path. Problems after step 4 do not raise: the restore has
happened, and they are returned as warnings.
"""

import os
import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

from .migrations import LEGACY_SCHEMA

APP_TABLES = set(LEGACY_SCHEMA)
SIDE_FILES = ("-wal", "-shm", "-journal")


class DatabaseInUse(RuntimeError):
    """Another connection holds the database; stop the api before restoring."""


class InvalidBackup(RuntimeError):
    """The file is not an intact database of this application."""


class ActiveUnreadable(RuntimeError):
    """The current database cannot be read; replacing it needs explicit consent."""


class RestoreStateUncertain(RuntimeError):
    """A failed restore could not be fully rolled back; `paths` need checking.

    The original failure is kept as __cause__.
    """

    def __init__(self, message: str, paths: list[str]) -> None:
        super().__init__(message)
        self.paths = paths


def _stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _unused(path: Path) -> Path:
    """`path`, or `path` with -2, -3, ... appended if it already exists (two runs in one second)."""
    candidate, n = path, 1
    while candidate.exists():
        n += 1
        candidate = path.with_name(f"{path.name}-{n}")
    return candidate


def _side_files(path: Path) -> list[Path]:
    return [path.with_name(path.name + s) for s in SIDE_FILES]


def sqlite_uri(path: Path, mode: str = "ro", immutable: bool = False) -> str:
    """URI for a file path. Percent-encoding matters: an unescaped '#' or '?'
    in a file name would cut the path short and open a different file."""
    uri = f"file:{quote(path.as_posix(), safe='/:')}?mode={mode}"
    return uri + "&immutable=1" if immutable else uri


def _ro(path: Path) -> sqlite3.Connection:
    """Read-only connection that writes nothing next to `path` when it can.

    With no -wal/-shm/-journal present, no other connection can be using the
    file (a WAL database keeps them while open), so it is opened as
    immutable: SQLite then creates no side files at all. Otherwise it is
    opened read-only in the normal way, so an existing WAL is read too.
    """
    no_sidecars = not any(f.exists() for f in _side_files(path))
    return sqlite3.connect(sqlite_uri(path, immutable=no_sidecars), uri=True)


def verify(path: Path) -> dict:
    """Integrity check and schema summary of a database file, opened read-only."""
    if not path.is_file():
        raise InvalidBackup(f"{path.name}: file not found")
    try:
        con = _ro(path)
        try:
            integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
            tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            revision = None
            if "alembic_version" in tables:
                row = con.execute("SELECT version_num FROM alembic_version").fetchone()
                revision = row[0] if row else None
        finally:
            con.close()
    except sqlite3.DatabaseError as exc:
        raise InvalidBackup(f"{path.name}: not a readable SQLite database ({exc})") from exc
    missing = sorted(APP_TABLES - tables)
    return {
        "file": path.name,
        "integrity": integrity,
        "revision": revision,
        "missing_tables": missing,
        "ok": integrity == "ok" and not missing,
    }


def _copy(src: sqlite3.Connection, dest: Path) -> None:
    dst = sqlite3.connect(dest)
    try:
        src.backup(dst)
        # Make the copy a self-contained file (rollback journal, no -wal/-shm),
        # so it can be moved, renamed or copied as one file. The api switches
        # it back to WAL on its first connection.
        dst.execute("PRAGMA journal_mode=DELETE")
    finally:
        dst.close()


def _remove_temp(path: Path) -> None:
    """Remove a file this module created (never user data), with its side files."""
    for f in (path, *_side_files(path)):
        f.unlink(missing_ok=True)


def _same_file(a: Path, b: Path) -> bool:
    if a.resolve() == b.resolve():
        return True
    try:
        return a.exists() and b.exists() and os.path.samefile(a, b)
    except OSError:
        return False


def _fsync_dir(directory: Path) -> None:
    """Best effort: make renames in `directory` durable (POSIX only)."""
    if os.name != "posix":
        return
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def backup(db_path: Path, dest_dir: Path | None = None) -> tuple[Path, dict]:
    """Consistent copy of the live database (safe while the api runs), verified."""
    if not db_path.is_file():
        raise InvalidBackup(f"{db_path.name}: no database to back up")  # never create an empty one
    dest = (dest_dir or db_path.parent) / f"{db_path.name}.manual-{_stamp()}.bak"
    try:
        src = sqlite3.connect(db_path)
        try:
            _copy(src, dest)
        finally:
            src.close()
        result = verify(dest)
        if not result["ok"]:
            raise InvalidBackup(f"the new backup failed verification: {result}")
    except BaseException:
        _remove_temp(dest)  # never leave a partial or unverified backup behind
        raise
    return dest, result


SQLITE_HEADER = b"SQLite format 3\x00"


def has_sqlite_header(path: Path) -> bool:
    """True if the file starts with SQLite's 16-byte header (read without SQLite, no side effects)."""
    try:
        with path.open("rb") as fh:
            return fh.read(len(SQLITE_HEADER)) == SQLITE_HEADER
    except OSError:
        return False


def check_not_in_use(db_path: Path) -> bool:
    """Refuse if anything else uses the database; return False if SQLite cannot read it.

    Two signals, because neither is enough alone:
    - an exclusive lock taken without waiting catches a connection that is
      reading or writing right now;
    - after our connection closes, SQLite removes -wal/-shm (and any hot
      -journal) if it was the last connection. If they are still there,
      another connection is open, even an idle one that holds no lock.
    An unreadable file ("file is not a database") is reported as False; the
    caller must then not assume it is unused (see ActiveUnreadable).
    """
    con = sqlite3.connect(db_path, timeout=0)
    try:
        con.execute("PRAGMA busy_timeout=0")
        con.execute("BEGIN EXCLUSIVE")
        con.execute("ROLLBACK")
    except sqlite3.OperationalError as exc:
        text = str(exc).lower()
        if "locked" in text or "busy" in text:
            raise DatabaseInUse(
                "the database is in use; stop the api (docker compose stop api) and any other container using it"
            ) from exc
        raise
    except sqlite3.DatabaseError:
        return False  # "file is not a database", "database disk image is malformed", ...
    finally:
        con.close()
    leftover = [f.name for f in _side_files(db_path) if f.exists()]
    if leftover:
        raise DatabaseInUse(
            f"{', '.join(leftover)} still present after closing the database, so another connection "
            "appears to be open; stop the api (docker compose stop api) and any other container using it"
        )
    return True


def is_intact(db_path: Path) -> bool:
    """PRAGMA quick_check on a file already known to be unused and free of side files.

    A valid header says nothing about the pages behind it; damaged pages
    would make the SQLite-level snapshot fail, so such a file is treated
    like an unreadable one (explicit consent, byte copy).
    """
    try:
        con = _ro(db_path)
        try:
            return con.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        finally:
            con.close()
    except sqlite3.DatabaseError:
        return False


def _snapshot(db_path: Path, dest: Path, readable: bool) -> str:
    """Write an independent copy of the old database to `dest` (app.db stays in place)."""
    if readable:
        # No other connection and no side files (checked), so an immutable
        # read is consistent and leaves nothing next to app.db.
        src = _ro(db_path)
        try:
            _copy(src, dest)
        finally:
            src.close()
        return "sqlite backup"
    shutil.copy2(db_path, dest)  # unreadable: keep the exact bytes for inspection
    return "file copy"


def restore(db_path: Path, source: Path, replace_unreadable: bool = False) -> dict:
    """Replace the database with a verified copy of `source`, keeping the old one.

    Raises only before the active database changes (with everything rolled
    back), or RestoreStateUncertain if a rollback step fails. After the
    atomic swap nothing raises; problems are returned in "warnings".
    """
    if _same_file(source, db_path):
        raise InvalidBackup("refusing to restore the active database onto itself; choose a backup file")
    checked = verify(source)
    if not checked["ok"]:
        raise InvalidBackup(f"refusing to restore: {checked}")

    temp = _unused(db_path.with_name(f"{db_path.name}.restore-{_stamp()}.tmp"))
    moved_to: Path | None = None
    preserved_by = None
    active_readable = None
    try:
        # 1. Copy and verify beside the database (same filesystem, so the
        #    final rename is atomic). The active database is not touched yet.
        src = _ro(source)
        try:
            _copy(src, temp)
        finally:
            src.close()
        result = verify(temp)
        if not result["ok"]:
            raise InvalidBackup(f"the copied backup failed verification: {result}")
        _match_directory_owner(temp)

        # 2. Nothing may be using the database. Opening it once also lets
        #    SQLite fold committed WAL content into it (content unchanged).
        if db_path.exists():
            # A file without a SQLite header is never opened through SQLite:
            # doing so could remove its leftover -wal/-shm, which we keep.
            active_readable = check_not_in_use(db_path) if has_sqlite_header(db_path) else False
            if active_readable:
                active_readable = is_intact(db_path)
            if not active_readable and not replace_unreadable:
                raise ActiveUnreadable(
                    f"{db_path.name} is unreadable or damaged, so other users of it cannot be reliably "
                    "ruled out. Stop the api, then repeat with --replace-unreadable to replace it "
                    "(the damaged file is kept byte for byte in pre-restore-<time>/)"
                )

        # 3. Preserve the old files: an independent snapshot of app.db, and
        #    any leftover side files (only possible when app.db is unreadable
        #    or missing) moved alongside it.
        side = [f for f in _side_files(db_path) if f.exists()]
        if db_path.exists() or side:
            moved_to = _unused(db_path.parent / f"pre-restore-{_stamp()}")
            moved_to.mkdir()
        moved_side: list[Path] = []
        try:
            if db_path.exists():
                preserved_by = _snapshot(db_path, moved_to / db_path.name, bool(active_readable))
            for f in side:
                shutil.move(str(f), moved_to / f.name)
                moved_side.append(f)

            # 4. Atomic swap: app.db is the old file until this instant, the new one after it.
            os.replace(temp, db_path)
        except BaseException as exc:
            _rollback(db_path, moved_to, moved_side, exc)
            raise
    finally:
        _remove_temp(temp)  # no-op after a successful rename

    # The restore has happened. Nothing below may raise.
    _fsync_dir(db_path.parent)
    warnings = []
    notes = []
    if active_readable is False:
        notes.append("the previous database was unreadable or damaged; it was replaced on request "
                     "(--replace-unreadable), other users of it could not be reliably ruled out, "
                     "and it was kept byte for byte for inspection")
    if moved_to is not None:
        try:
            _match_directory_owner(moved_to, recursive=True)
        except OSError as exc:
            warnings.append(f"could not change the owner of {moved_to.name}: {exc}")
    try:
        final = verify(db_path)
    except Exception as exc:  # noqa: BLE001 - reported, not raised, after activation
        final = {"file": db_path.name, "integrity": None, "revision": None, "missing_tables": [], "ok": False}
        warnings.append(f"the restored database could not be verified: {exc}")
    if not final["ok"] and not any("verif" in w for w in warnings):
        warnings.append("the restored database failed verification")
    return {
        "restored": True,
        "restored_from": source.name,
        "previous_files": moved_to.name if moved_to else None,
        "previous_database_preserved_by": preserved_by,
        "previous_database_was_readable": active_readable,
        "warnings": warnings,
        "notes": notes,
        **final,
    }


def _rollback(db_path: Path, moved_to: Path | None, moved_side: list[Path], cause: BaseException) -> None:
    """Undo step 3 after a failure before the swap. app.db itself never moved."""
    uncertain = []
    for f in moved_side:
        try:
            if not f.exists():
                shutil.move(str(moved_to / f.name), f)
        except OSError as exc:
            uncertain.append(f"{f.name}: still in {moved_to.name} ({exc})")
    if moved_to is not None and not uncertain:
        # The snapshot (complete or partial) is our own copy and app.db is
        # untouched, so removing it loses nothing. When side files are
        # stranded, keep it next to them: together they are a consistent copy.
        try:
            (moved_to / db_path.name).unlink(missing_ok=True)
            if not any(moved_to.iterdir()):
                moved_to.rmdir()
        except OSError:
            pass  # a leftover copy or empty directory is harmless
    if uncertain:
        raise RestoreStateUncertain(
            f"restore failed ({type(cause).__name__}: {cause}) and could not be fully rolled back; "
            f"{db_path.name} is the previous database but its side files were moved",
            uncertain,
        ) from cause


def _match_directory_owner(path: Path, recursive: bool = False) -> None:
    """When run as root (docker compose run --user root), hand files to the data directory's owner."""
    if not (hasattr(os, "geteuid") and os.geteuid() == 0):
        return
    owner = path.parent.stat()
    targets = [path, *path.rglob("*")] if recursive and path.is_dir() else [path]
    for target in targets:
        os.chown(target, owner.st_uid, owner.st_gid)
