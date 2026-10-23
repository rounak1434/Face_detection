"""Command-line tools.

  ai-surveillance create-admin <username>       prompts for a password
  ai-surveillance probe --env CAMERA_MAIN_URL    checks streams with ffprobe
"""

import argparse
import getpass
import json
import os
import sys

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
    args = parser.parse_args(argv)

    if args.command == "create-admin":
        return create_admin(args.username)
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
