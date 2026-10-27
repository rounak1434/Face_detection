"""Command-line tools.

  ai-surveillance create-admin <username>       prompts for a password
  ai-surveillance probe --env CAMERA_MAIN_URL    checks streams with ffprobe
  ai-surveillance db status                      shows the schema revision (read-only)
  ai-surveillance db upgrade                     backs up, then migrates to the latest revision
  ai-surveillance db backup [DIR]                consistent, verified copy (safe while running)
  ai-surveillance db verify FILE                 integrity check of a database file (read-only)
  ai-surveillance db restore FILE --yes          replace the database (api must be stopped)
                     [--replace-unreadable]      ...even if the current database is unreadable or damaged
"""

import argparse
import getpass
import json
import os
import sqlite3
import sys
from pathlib import Path

from sqlalchemy import select

from .config import get_settings
from .db import AdminUser, Database
from .rtsp import probe
from .security import MIN_PASSWORD_LENGTH, hash_password, revoke_user_sessions


def create_admin(username: str) -> int:
    db = Database(get_settings().database_url)
    db.migrate()
    password = getpass.getpass(f"Password for {username} (min {MIN_PASSWORD_LENGTH} chars): ")
    if password != getpass.getpass("Repeat password: "):
        print("Passwords do not match.", file=sys.stderr)
        return 1
    try:
        password_hash = hash_password(password)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1
    with db.sessionmaker() as session:
        user = session.scalar(select(AdminUser).where(AdminUser.username == username))
        if user is None:
            session.add(AdminUser(username=username, password_hash=password_hash))
            action = "Created"
        else:
            user.password_hash = password_hash
            revoke_user_sessions(session, user.id)
            action = "Updated password (and signed out all sessions) for"
        session.commit()
    print(f"{action} admin user {username!r}.")
    return 0


def _sqlite_file() -> Path:
    url = get_settings().database_url
    if not url.startswith("sqlite:///"):
        raise SystemExit("backup/verify/restore support SQLite databases only")
    return Path(url.removeprefix("sqlite:///"))


def db_file_command(action: str, path: str | None, yes: bool, replace_unreadable: bool = False) -> int:
    from . import dbtools

    db_path = _sqlite_file()
    # Only the operation itself runs inside this try: its handlers describe a
    # database that was not changed. Printing the result happens afterwards,
    # in _report, so a broken pipe or a Ctrl-C there is never mistaken for a
    # failed operation.
    try:
        if action == "backup":
            dest, result = dbtools.backup(db_path, Path(path) if path else None)
            result = {"backup": str(dest), **result}
        elif not path:
            print(f"db {action} needs a file path", file=sys.stderr)
            return 2
        elif action == "verify":
            result = dbtools.verify(Path(path))
        elif not yes:
            print("Restore replaces the database. Stop the api first (docker compose stop api), "
                  "then repeat with --yes.", file=sys.stderr)
            return 2
        else:
            # restore() raises only before the active database changed (fully
            # rolled back), with RestoreStateUncertain, or with
            # RestoreInterruptedAfterSwap on a Ctrl-C after the swap. Otherwise,
            # once the swap has happened, it returns with problems in "warnings".
            result = dbtools.restore(db_path, Path(path), replace_unreadable=replace_unreadable)
    except dbtools.RestoreInterruptedAfterSwap as exc:
        print(f"Interrupted: {exc} Run 'ai-surveillance db verify {db_path}' before starting the api."
              + _notes(exc), file=sys.stderr)
        return 3
    except dbtools.RestoreStateUncertain as exc:
        print(f"State uncertain: {exc}. Do not start the api. Files to check: "
              + "; ".join(exc.paths) + _notes(exc), file=sys.stderr)
        return 6
    except (dbtools.InvalidBackup, dbtools.DatabaseInUse, dbtools.ActiveUnreadable) as exc:
        state = "active database unchanged" if action == "restore" else "nothing written"
        print(f"Not done ({state}): {exc}" + _notes(exc), file=sys.stderr)
        return 4
    except (sqlite3.Error, OSError) as exc:
        # Unexpected I/O or SQLite failure (disk full, permissions, ...).
        # restore() only gets here before its swap, after a complete rollback.
        state = "active database unchanged" if action == "restore" else "no backup written"
        print(f"Not done ({state}): {type(exc).__name__}: {exc}" + _notes(exc), file=sys.stderr)
        return 5
    except KeyboardInterrupt as exc:
        # restore() lets a bare KeyboardInterrupt through only before its swap
        # and after a complete rollback; backup() removes its partial file.
        state = {"restore": "active database unchanged; everything done so far was rolled back",
                 "backup": "no backup written"}.get(action, "nothing written")
        print(f"Not done ({state}): interrupted (Ctrl-C)." + _notes(exc), file=sys.stderr)
        return 130

    # The operation has finished. Anything that goes wrong from here on is
    # about showing its result, not about the database.
    try:
        return _report(action, db_path, result)
    except (KeyboardInterrupt, OSError) as exc:
        return _report_interrupted(action, db_path, result, exc)


def _report(action: str, db_path: Path, result: dict) -> int:
    print(json.dumps(result, indent=2))
    if action == "backup":
        return 0
    if action == "verify":
        return 0 if result["ok"] else 4
    for note in result.get("notes", []):
        print(f"Note: {note}.", file=sys.stderr)
    if result["warnings"]:
        print("Restored, with warnings: " + "; ".join(result["warnings"]) + "." + _previous(result),
              file=sys.stderr)
        return 3
    return 0


def _previous(result: dict) -> str:
    return (f" The previous database is in {result['previous_files']}."
            if result["previous_files"] else " There was no previous database.")


def _report_interrupted(action: str, db_path: Path, result: dict, exc: BaseException) -> int:
    """The operation finished, but printing its result failed (Ctrl-C, closed pipe).

    Says what actually happened, on stderr, as far as stderr still works.
    restore and backup exit 3 (done, with a warning); verify, which changed
    nothing, exits 130 on Ctrl-C or 5 otherwise.
    """
    reason = "Ctrl-C" if isinstance(exc, KeyboardInterrupt) else f"{type(exc).__name__}: {exc}"
    if action == "restore":
        message = (f"Restored: the restored database is active as {db_path.name}"
                   f"{' and verified' if result['ok'] else ', but it failed verification'}, "
                   f"but printing the result was interrupted ({reason}).{_previous(result)}"
                   + "".join(f" Warning: {w}." for w in result["warnings"])
                   + "".join(f" Note: {n}." for n in result.get("notes", []))
                   + f" Run 'ai-surveillance db verify {db_path}' to see the result again.")
        code = 3
    elif action == "backup":
        message = (f"Backup written and verified: {result['backup']}, but printing the result was "
                   f"interrupted ({reason}).")
        code = 3
    else:
        message = f"Nothing changed: verify finished, but printing its result was interrupted ({reason})."
        code = 130 if isinstance(exc, KeyboardInterrupt) else 5
    try:
        print(message, file=sys.stderr)
    except (KeyboardInterrupt, OSError):
        pass  # stderr is gone too; the exit code still tells the truth
    return code


def _notes(exc: BaseException) -> str:
    return "".join(f" Note: {n}." for n in getattr(exc, "__notes__", []))


def db_command(action: str) -> int:
    from .migrations import SchemaMismatch, status

    db = Database(get_settings().database_url)
    try:
        if action == "status":
            print(json.dumps(status(db.engine), indent=2))
            return 0
        try:
            print(f"Database: {db.migrate()}")
        except SchemaMismatch as exc:
            print(f"Not migrated: {exc}", file=sys.stderr)
            return 3
        return 0
    finally:
        db.engine.dispose()


def probe_urls(urls: list[str], timeout: float) -> int:
    results = [probe(url, timeout_seconds=timeout).to_dict() for url in urls]
    print(json.dumps(results, indent=2))
    return 0 if all(r["ok"] for r in results) else 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ai-surveillance")
    sub = parser.add_subparsers(dest="command", required=True)
    p_admin = sub.add_parser("create-admin", help="create or reset an administrator")
    p_admin.add_argument("username")
    p_probe = sub.add_parser("probe", help="check RTSP streams with ffprobe (credentials are masked)")
    p_probe.add_argument("urls", nargs="*", help="URLs without secrets; prefer --env for credentialed URLs")
    p_probe.add_argument(
        "--env", action="append", default=[], metavar="VAR",
        help="read a URL from this environment variable (keeps passwords out of shell history)",
    )
    p_probe.add_argument("--timeout", type=float, default=15.0)
    p_db = sub.add_parser("db", help="database status, migration, backup, verify and restore")
    p_db.add_argument("action", choices=["status", "upgrade", "backup", "verify", "restore"])
    p_db.add_argument("path", nargs="?", help="backup: target directory; verify/restore: database file")
    p_db.add_argument("--yes", action="store_true", help="restore: confirm the api is stopped")
    p_db.add_argument("--replace-unreadable", action="store_true",
                      help="restore: allow replacing a current database that is unreadable or damaged "
                           "(other users of it cannot be ruled out; stop the api first)")
    args = parser.parse_args(argv)

    if args.command == "create-admin":
        return create_admin(args.username)
    if args.command == "db":
        if args.action in ("backup", "verify", "restore"):
            return db_file_command(args.action, args.path, args.yes, args.replace_unreadable)
        return db_command(args.action)
    urls = list(args.urls)
    for var in args.env:
        value = os.environ.get(var)
        if not value:
            parser.error(f"environment variable {var} is not set")
        urls.append(value)
    if not urls:
        parser.error("give at least one URL or --env VAR")
    return probe_urls(urls, args.timeout)


if __name__ == "__main__":
    sys.exit(main())
