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
    try:
        if action == "backup":
            dest, result = dbtools.backup(db_path, Path(path) if path else None)
            print(json.dumps({"backup": str(dest), **result}, indent=2))
            return 0
        if not path:
            print(f"db {action} needs a file path", file=sys.stderr)
            return 2
        if action == "verify":
            result = dbtools.verify(Path(path))
            print(json.dumps(result, indent=2))
            return 0 if result["ok"] else 4
        if not yes:
            print("Restore replaces the database. Stop the api first (docker compose stop api), "
                  "then repeat with --yes.", file=sys.stderr)
            return 2
        # From here on, restore() raises only before the active database
        # changed (fully rolled back) or with RestoreStateUncertain. Once the
        # swap has happened it returns, with any problems in "warnings".
        result = dbtools.restore(db_path, Path(path), replace_unreadable=replace_unreadable)
        print(json.dumps(result, indent=2))
        where = (f" The previous database is in {result['previous_files']}."
                 if result["previous_files"] else " There was no previous database.")
        for note in result.get("notes", []):
            print(f"Note: {note}.", file=sys.stderr)
        if result["warnings"]:
            print("Restored, with warnings: " + "; ".join(result["warnings"]) + "." + where, file=sys.stderr)
            return 3
        return 0
    except dbtools.RestoreStateUncertain as exc:
        print(f"State uncertain: {exc}. Do not start the api until these are back in place: "
              + "; ".join(exc.paths), file=sys.stderr)
        return 6
    except (dbtools.InvalidBackup, dbtools.DatabaseInUse, dbtools.ActiveUnreadable) as exc:
        state = "active database unchanged" if action == "restore" else "nothing written"
        print(f"Not done ({state}): {exc}", file=sys.stderr)
        return 4
    except (sqlite3.Error, OSError) as exc:
        # Unexpected I/O or SQLite failure (disk full, permissions, ...).
        # restore() only gets here before its swap, after a complete rollback.
        state = "active database unchanged" if action == "restore" else "no backup written"
        print(f"Not done ({state}): {type(exc).__name__}: {exc}", file=sys.stderr)
        return 5


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
