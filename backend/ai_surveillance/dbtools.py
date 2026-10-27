"""SQLite backup, verification and restore (WAL-safe).

Used by `ai-surveillance db backup|verify|restore`. Copies are made with
SQLite's online backup API, so they are consistent even while the api is
running in WAL mode, and are written as standalone single-file databases.

A restore never leaves the active database missing, and keeps an
independent copy of the old one:
1. the backup is copied into a temporary file next to app.db and verified;
2. app.db must be intact (SQLite header, PRAGMA quick_check "ok", checked
   without changing any file) and not in use: no lock held, and no
   -wal/-shm/-journal left after our own connection closes (their presence
   means another connection is open). An unreadable or damaged app.db
   needs explicit consent and is never opened for writing;
3. an independent snapshot of the old app.db is written to
   pre-restore-<time>/ (app.db itself stays in place) and flushed; leftover
   side files, possible only when app.db is unreadable or missing, are
   moved there; directory entries are flushed where the OS allows it;
4. the verified copy replaces app.db with one atomic rename.
At every moment app.db exists (old or new). Failures before step 4 are
rolled back; if a rollback step itself fails or is interrupted,
RestoreStateUncertain lists every affected path. Problems after step 4 do
not raise: the restore has happened, and they are returned as warnings. A
Ctrl-C after step 4 raises RestoreInterruptedAfterSwap. This is not
crash-atomic across files and gives no power-loss guarantee.
"""

import os
import shutil
import sqlite3
import stat
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


class RestoreInterruptedAfterSwap(RuntimeError):
    """Ctrl-C after the restored database replaced app.db; the final checks did not finish."""

    def __init__(self, db_path: Path, previous: Path | None) -> None:
        where = (f"The previous database is in {previous}." if previous is not None
                 else "There was no previous database.")
        super().__init__(
            f"interrupted after the restored database replaced {db_path.name}; the final checks did not "
            f"finish, so it is not verified. {where}"
        )
        self.previous = previous


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


def _identity(path: Path) -> tuple[int, int]:
    """(device, file id). A rename keeps it, so it tells which file app.db is."""
    st = os.stat(path)
    return st.st_dev, st.st_ino


def _fsync_file(path: Path) -> None:
    """Flush a file's contents to the device. Errors propagate: a recovery
    copy that may not be on disk must not be relied on."""
    fd = os.open(path, os.O_RDWR | getattr(os, "O_BINARY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_dir(directory: Path) -> None:
    """Best effort: make new and renamed entries in `directory` durable.

    POSIX only; Windows cannot open a directory for fsync. Errors are
    ignored because some filesystems reject fsync on directories.
    """
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


def _quick_check(path: Path, con: sqlite3.Connection | None = None) -> bool:
    try:
        con = con or sqlite3.connect(path)
        try:
            return con.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        finally:
            con.close()
    except sqlite3.DatabaseError:
        return False


def _signature(db_path: Path) -> list[tuple]:
    """Size and modification time of app.db and each possible side file."""
    sig = []
    for f in (db_path, *_side_files(db_path)):
        try:
            st = f.stat()
            sig.append((f.name, st.st_size, st.st_mtime_ns))
        except FileNotFoundError:
            sig.append((f.name, None, None))
    return sig


def _changed_while_checking(db_path: Path) -> DatabaseInUse:
    return DatabaseInUse(
        f"{db_path.name} or its side files changed while being checked, so something is using it; "
        "stop the api (docker compose stop api) and any other container using it"
    )


def is_intact(db_path: Path, leftovers: list[Path] | None = None) -> bool:
    """PRAGMA quick_check without changing any file of `db_path`.

    A valid header says nothing about the pages behind it; damaged pages
    would make the SQLite-level snapshot fail, so such a file is treated
    like an unreadable one (explicit consent, byte copy).

    Runs before anything opens the database for writing: the closing of a
    writable connection would fold the -wal into app.db and delete -wal and
    -shm, destroying the evidence of a damaged database. Without side files
    the check reads app.db as immutable. With side files it runs on a
    scratch copy of app.db and its -wal/-journal next to it, removed
    afterwards; a scratch directory that cannot be removed is appended to
    `leftovers`. If any of the files changes during the check, the result
    cannot be trusted and DatabaseInUse is raised.
    """
    before = _signature(db_path)
    side = [f for f in _side_files(db_path) if f.exists()]
    if not side:
        try:
            ok = _quick_check(db_path, _ro(db_path))
        except sqlite3.DatabaseError:
            ok = False
    else:
        scratch = _unused(db_path.with_name(f"{db_path.name}.check-{_stamp()}"))
        scratch.mkdir()
        try:
            for f in (db_path, *side):
                if not f.name.endswith("-shm"):  # the index is rebuilt from the -wal
                    try:
                        shutil.copyfile(f, scratch / f.name)
                    except FileNotFoundError as exc:
                        raise _changed_while_checking(db_path) from exc
            ok = _quick_check(scratch / db_path.name)
        finally:
            try:
                shutil.rmtree(scratch)
            except OSError:
                if leftovers is not None:
                    leftovers.append(scratch)
    if _signature(db_path) != before:
        raise _changed_while_checking(db_path)
    return ok


def _snapshot(db_path: Path, dest: Path, readable: bool) -> str:
    """Write an independent copy of the old database to `dest` (app.db stays in place),
    flushed to the device before it is relied on."""
    if readable:
        # No other connection and no side files (checked), so an immutable
        # read is consistent and leaves nothing next to app.db.
        src = _ro(db_path)
        try:
            _copy(src, dest)
        finally:
            src.close()
        method = "sqlite backup"
    else:
        # Unreadable: keep the exact bytes for inspection. Copy the bytes into
        # a new (writable) file and flush them first; the original's
        # permissions and timestamps are applied only afterwards, because a
        # read-only file cannot be opened for the flush on every platform.
        shutil.copyfile(db_path, dest)
        _fsync_file(dest)
        shutil.copystat(db_path, dest)
        return "file copy"
    _fsync_file(dest)
    return method


def restore(db_path: Path, source: Path, replace_unreadable: bool = False) -> dict:
    """Replace the database with a verified copy of `source`, keeping the old one.

    Raises only before the active database changes (with everything rolled
    back, including on Ctrl-C), RestoreStateUncertain if a rollback step
    fails or is interrupted, or RestoreInterruptedAfterSwap on a Ctrl-C after
    the swap. Otherwise, after the swap, problems are returned in "warnings".
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
    scratch_left: list[Path] = []
    temp_id: tuple[int, int] | None = None  # the temporary file's identity, taken right before the rename

    def swapped() -> bool:
        """Whether app.db is now the restored file. Decided from the files, not
        from how far the code got, so a Ctrl-C at any instant is judged right."""
        if temp_id is None:
            return False  # the rename was never attempted
        try:
            current = _identity(db_path)
        except OSError:
            return False
        if temp_id[1] and current[1]:
            return current == temp_id
        return not temp.exists()  # this filesystem has no usable file ids

    try:
        try:
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

                # 2. Nothing may be using the database. Only an intact file is
                #    opened for writing (the in-use probe); its closing may fold
                #    committed WAL content into app.db (content unchanged). A file
                #    without a SQLite header, or a damaged one, is never opened for
                #    writing, so it and its -wal/-shm stay exactly as found.
                if db_path.exists():
                    active_readable = has_sqlite_header(db_path) and is_intact(db_path, scratch_left)
                    if active_readable:
                        active_readable = check_not_in_use(db_path)
                    if not active_readable and not replace_unreadable:
                        raise ActiveUnreadable(
                            f"{db_path.name} is unreadable or damaged, so other users of it cannot be reliably "
                            "ruled out. Nothing was changed. Stop the api, then repeat with --replace-unreadable; "
                            f"{db_path.name} and any side files will then be kept byte for byte in pre-restore-<time>/"
                        )

                # 3. Preserve the old files: an independent snapshot of app.db, and
                #    any leftover side files (only possible when app.db is unreadable
                #    or missing) moved alongside it.
                side = [f for f in _side_files(db_path) if f.exists()]
                if db_path.exists() or side:
                    moved_to = _unused(db_path.parent / f"pre-restore-{_stamp()}")
                moved_side: list[Path] = []
                try:
                    if moved_to is not None:
                        moved_to.mkdir()
                    if db_path.exists():
                        preserved_by = _snapshot(db_path, moved_to / db_path.name, bool(active_readable))
                    for f in side:
                        shutil.move(str(f), moved_to / f.name)
                        moved_side.append(f)
                    # 4. Make the recovery copy and the new file durable before
                    #    relying on them: file contents first, then the directory
                    #    entries (best effort, POSIX only).
                    if moved_to is not None:
                        _fsync_dir(moved_to)
                    _fsync_file(temp)
                    _fsync_dir(db_path.parent)

                    # 5. Atomic swap: app.db is the old file until this instant, the new one after it.
                    temp_id = _identity(temp)
                    os.replace(temp, db_path)
                except BaseException as exc:
                    if swapped():
                        # Interrupted just after the rename: the new database is
                        # active. Rolling back now would put the old side files
                        # next to it.
                        raise RestoreInterruptedAfterSwap(db_path, moved_to) from exc
                    try:
                        _rollback(db_path, moved_to, moved_side, exc)
                    except KeyboardInterrupt:
                        raise RestoreStateUncertain(
                            f"restore failed ({type(exc).__name__}: {exc}) and the rollback was interrupted "
                            f"(Ctrl-C). {db_path.name} is still the previous database. Stop the api and everything "
                            "else using the database, then check each file listed",
                            _listing(db_path, moved_to, moved_side),
                        ) from exc
                    raise
            finally:
                _remove_temp(temp)  # no-op after a successful rename

            # The restore has happened. Only a Ctrl-C can interrupt what follows.
            return _after_swap(db_path, source, moved_to, preserved_by, active_readable, scratch_left)
        except KeyboardInterrupt as exc:
            # One place decides what a Ctrl-C means, wherever it landed,
            # including in the cleanup just above.
            if swapped():
                raise RestoreInterruptedAfterSwap(db_path, moved_to) from exc
            if isinstance(exc.__context__, RestoreStateUncertain):
                # Interrupted while cleaning up after a failed rollback: the
                # uncertain state is what the operator must hear about.
                uncertain = exc.__context__
                uncertain.add_note("interrupted (Ctrl-C) while cleaning up")
                raise uncertain
            raise
    except BaseException as exc:
        for path in scratch_left:
            exc.add_note(f"a scratch copy made by the integrity check could not be removed: {path}")
        raise


def _listing(db_path: Path, moved_to: Path | None, moved_side: list[Path]) -> list[str]:
    """Every file a rollback may have left half done, and where it is."""
    paths = []
    if moved_to is not None and moved_to.exists():
        paths += [f"{p.name}: in {p.parent}" for p in sorted(moved_to.iterdir())]
    paths += [f"{f.name}: in {f.parent}" for f in moved_side if f.exists()]
    return paths


def _after_swap(db_path: Path, source: Path, moved_to: Path | None, preserved_by: str | None,
                active_readable: bool | None, scratch_left: list[Path]) -> dict:
    """Checks after the swap. Nothing here raises except KeyboardInterrupt."""
    _fsync_dir(db_path.parent)
    warnings = []
    notes = [f"a scratch copy made by the integrity check could not be removed: {p}" for p in scratch_left]
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
    """Undo step 3 after a failure before the swap. app.db itself never moved.

    The snapshot is removed only when every moved side file is back in place.
    Otherwise it stays next to the stranded side files (together they are a
    consistent copy) and RestoreStateUncertain lists each of them.
    """
    uncertain = []
    for f in moved_side:
        kept = moved_to / f.name
        if f.exists():
            # Never overwrite: a new side file appeared meanwhile, so something
            # opened the database during the restore. Both copies may hold data.
            uncertain.append(
                f"{f.name}: kept in {moved_to}, not moved back because a new {f.name} appeared in "
                f"{f.parent} meanwhile (something opened the database); keep both and do not overwrite either"
            )
            continue
        try:
            shutil.move(str(kept), f)
        except OSError as exc:
            uncertain.append(f"{f.name}: still in {moved_to}, could not be moved back to {f.parent} ({exc}); "
                             "move it back with the api stopped")
    if uncertain:
        raise RestoreStateUncertain(
            f"restore failed ({type(cause).__name__}: {cause}) and could not be fully rolled back. "
            f"{db_path.name} is still the previous database, but some of its side files are in {moved_to}, "
            f"next to a copy of {db_path.name}. Stop the api and everything else using the database, "
            "then deal with each file listed",
            uncertain,
        ) from cause
    if moved_to is not None:
        # Everything is back. The snapshot (complete or partial) is our own
        # copy and app.db is untouched, so removing it loses nothing.
        try:
            copy = moved_to / db_path.name
            if copy.exists() and not os.access(copy, os.W_OK):
                os.chmod(copy, stat.S_IREAD | stat.S_IWRITE)  # a copy of a read-only app.db (Windows refuses the unlink)
            copy.unlink(missing_ok=True)
            if not any(moved_to.iterdir()):
                moved_to.rmdir()
        except OSError:
            pass  # a leftover copy or empty directory is harmless


def _match_directory_owner(path: Path, recursive: bool = False) -> None:
    """When run as root (docker compose run --user root), hand files to the data directory's owner."""
    if not (hasattr(os, "geteuid") and os.geteuid() == 0):
        return
    owner = path.parent.stat()
    targets = [path, *path.rglob("*")] if recursive and path.is_dir() else [path]
    for target in targets:
        os.chown(target, owner.st_uid, owner.st_gid)
