"""Backup, verify and restore of the SQLite database, on temporary files only."""

import json
import os
import shutil
import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import text

from ai_surveillance import dbtools
from ai_surveillance.db import AdminUser, Database, Device


@pytest.fixture
def live(tmp_path) -> Path:
    """A migrated database with data, in WAL mode, like the api's."""
    path = tmp_path / "app.db"
    db = Database(f"sqlite:///{path}")
    db.migrate()
    with db.sessionmaker() as s:
        s.add(AdminUser(username="admin", password_hash="h"))
        s.add(Device(name="Cam", kind="nvr", host="192.168.1.2", rtsp_port=554, main_path="/x"))
        s.commit()
    db.engine.dispose()
    return path


def _usernames(path: Path) -> list[str]:
    con = sqlite3.connect(path)
    try:
        return [r[0] for r in con.execute("SELECT username FROM admin_users ORDER BY username")]
    finally:
        con.close()


def test_backup_is_verified_and_consistent(live):
    dest, result = dbtools.backup(live)
    assert dest.name.startswith("app.db.manual-") and dest.suffix == ".bak"
    assert result["ok"] and result["integrity"] == "ok" and result["revision"] == "0001_baseline"
    assert _usernames(dest) == ["admin"]


def test_backup_while_a_writer_has_uncheckpointed_wal(live):
    # Data that exists only in the -wal file must still be in the backup.
    db = Database(f"sqlite:///{live}")
    conn = db.engine.connect()
    conn.execute(text("PRAGMA wal_autocheckpoint=0"))
    conn.execute(text("INSERT INTO admin_users (id, username, password_hash, created_at) "
                      "VALUES ('x2', 'second', 'h', '2026-10-23 00:00:00')"))
    conn.commit()
    try:
        assert live.with_name("app.db-wal").stat().st_size > 0
        dest, _ = dbtools.backup(live)
        assert _usernames(dest) == ["admin", "second"]
    finally:
        conn.close()
        db.engine.dispose()


def test_backup_never_creates_an_empty_database(tmp_path):
    with pytest.raises(dbtools.InvalidBackup):
        dbtools.backup(tmp_path / "missing.db")
    assert not (tmp_path / "missing.db").exists()


@pytest.mark.parametrize("content, fragment", [(b"not sqlite at all" * 100, "not a readable SQLite"), (None, "not found")])
def test_verify_rejects_bad_files(tmp_path, content, fragment):
    path = tmp_path / "bad.bak"
    if content is not None:
        path.write_bytes(content)
    with pytest.raises(dbtools.InvalidBackup, match=fragment):
        dbtools.verify(path)


def test_verify_flags_unrelated_database(tmp_path):
    path = tmp_path / "other.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE something (id INTEGER)")
    con.close()
    result = dbtools.verify(path)
    assert result["ok"] is False and "admin_users" in result["missing_tables"]


def _write_into_wal_only(path: Path, username: str) -> None:
    """Commit a row that stays in the -wal file (no checkpoint), as after an unclean stop."""
    import shutil

    con = sqlite3.connect(path)
    con.execute("PRAGMA wal_autocheckpoint=0")
    con.execute("INSERT INTO admin_users (id, username, password_hash, created_at) "
                f"VALUES ('{username}', '{username}', 'h', '2026-10-23 00:00:00')")
    con.commit()
    # Snapshot the files while the connection is open, then "crash": put the
    # snapshot back so app.db + a non-empty -wal remain, like a killed process.
    snap = path.parent / "snap"
    snap.mkdir()
    for suffix in ("", "-wal", "-shm"):
        shutil.copy2(path.with_name(path.name + suffix), snap / (path.name + suffix))
    con.close()
    for suffix in ("", "-wal", "-shm"):
        shutil.copy2(snap / (path.name + suffix), path.with_name(path.name + suffix))
    shutil.rmtree(snap)


def test_restore_preserves_committed_wal_data_in_moved_copy(live, tmp_path):
    (tmp_path / "b").mkdir()
    backup_path, _ = dbtools.backup(live, tmp_path / "b")
    _write_into_wal_only(live, "after-backup")
    assert live.with_name("app.db-wal").stat().st_size > 0

    result = dbtools.restore(live, backup_path)

    assert result["ok"] and result["integrity"] == "ok"
    assert _usernames(live) == ["admin"]  # the backup's state, not the later write
    moved = live.parent / result["previous_files"]
    # The later write survives in the moved-aside database: nothing was lost.
    assert "after-backup" in _usernames(moved / "app.db")


def test_restore_never_replays_stale_wal(live, tmp_path):
    (tmp_path / "b").mkdir()
    backup_path, _ = dbtools.backup(live, tmp_path / "b")
    # Garbage left beside the database must not end up next to the restored file.
    live.with_name("app.db-wal").write_bytes(b"stale wal bytes" * 100)
    live.with_name("app.db-shm").write_bytes(b"stale shm bytes" * 100)

    result = dbtools.restore(live, backup_path)

    assert result["ok"]
    for suffix in ("-wal", "-shm"):
        side = live.with_name("app.db" + suffix)
        assert not side.exists() or b"stale" not in side.read_bytes()
    assert _usernames(live) == ["admin"]


def test_restore_refuses_invalid_backup_and_changes_nothing(live, tmp_path):
    bad = tmp_path / "bad.bak"
    bad.write_bytes(b"garbage" * 200)
    before = live.read_bytes()
    with pytest.raises(dbtools.InvalidBackup):
        dbtools.restore(live, bad)
    assert live.read_bytes() == before
    assert not list(live.parent.glob("pre-restore-*"))


def test_restore_refuses_when_database_is_in_use(live, tmp_path):
    backup_path, _ = dbtools.backup(live, tmp_path)
    holder = sqlite3.connect(live)
    holder.execute("BEGIN IMMEDIATE")  # an active writer, like a running api mid-write
    try:
        with pytest.raises(dbtools.DatabaseInUse):
            dbtools.restore(live, backup_path)
    finally:
        holder.rollback()
        holder.close()
    assert not list(live.parent.glob("pre-restore-*"))


def _snapshot(path: Path) -> bytes:
    return path.read_bytes()


def test_restore_onto_itself_is_refused_and_db_untouched(live):
    before = _snapshot(live)
    with pytest.raises(dbtools.InvalidBackup, match="onto itself"):
        dbtools.restore(live, live)
    # Another spelling of the same path is caught too.
    with pytest.raises(dbtools.InvalidBackup, match="onto itself"):
        dbtools.restore(live, live.parent / "." / live.name)
    assert _snapshot(live) == before
    assert not list(live.parent.glob("pre-restore-*"))


def test_copy_failure_leaves_active_db_untouched(live, tmp_path, monkeypatch):
    (tmp_path / "b").mkdir()
    backup_path, _ = dbtools.backup(live, tmp_path / "b")
    before = _snapshot(live)

    def failing_copy(src, dest):
        Path(dest).write_bytes(b"partial")  # e.g. disk full halfway through
        raise sqlite3.OperationalError("database or disk is full")

    monkeypatch.setattr(dbtools, "_copy", failing_copy)
    with pytest.raises(sqlite3.OperationalError):
        dbtools.restore(live, backup_path)
    assert _snapshot(live) == before and _usernames(live) == ["admin"]
    assert not list(live.parent.glob("pre-restore-*"))
    assert not list(live.parent.glob("*.restore-*.tmp*"))  # temp file cleaned up


def test_unverifiable_copy_leaves_active_db_untouched(live, tmp_path, monkeypatch):
    (tmp_path / "b").mkdir()
    backup_path, _ = dbtools.backup(live, tmp_path / "b")
    before = _snapshot(live)
    real_verify = dbtools.verify

    def verify_fails_for_temp(path):
        result = real_verify(path)
        return {**result, "ok": False, "integrity": "simulated corruption"} if ".restore-" in path.name else result

    monkeypatch.setattr(dbtools, "verify", verify_fails_for_temp)
    with pytest.raises(dbtools.InvalidBackup, match="copied backup failed verification"):
        dbtools.restore(live, backup_path)
    assert _snapshot(live) == before
    assert not list(live.parent.glob("pre-restore-*"))
    assert not list(live.parent.glob("*.restore-*.tmp*"))


def test_failed_swap_puts_old_database_back(live, tmp_path, monkeypatch):
    (tmp_path / "b").mkdir()
    backup_path, _ = dbtools.backup(live, tmp_path / "b")
    before = _snapshot(live)

    def failing_replace(src, dst):
        raise OSError("rename failed")

    monkeypatch.setattr(dbtools.os, "replace", failing_replace)
    with pytest.raises(OSError, match="rename failed"):
        dbtools.restore(live, backup_path)
    assert live.exists() and _snapshot(live) == before and _usernames(live) == ["admin"]
    assert not list(live.parent.glob("*.restore-*.tmp*"))


def test_failure_while_moving_old_files_puts_them_back(live, tmp_path, monkeypatch):
    (tmp_path / "b").mkdir()
    backup_path, _ = dbtools.backup(live, tmp_path / "b")
    # Side files are only moved when app.db is unreadable (a readable app.db
    # with side files left is refused as "in use"), so use that situation.
    before = _make_unreadable_with_sidecars(live)
    real_move = dbtools.shutil.move
    calls = {"n": 0}

    def move_fails_second_time(src, dst):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("no space left on device")
        return real_move(src, dst)

    monkeypatch.setattr(dbtools.shutil, "move", move_fails_second_time)
    with pytest.raises(OSError, match="no space left"):
        dbtools.restore(live, backup_path, replace_unreadable=True)
    monkeypatch.setattr(dbtools.shutil, "move", real_move)
    assert calls["n"] >= 2  # second forward move failed (the rollback move is counted too)
    # Everything is back in place, byte for byte, and nothing extra is left.
    assert _files_of(live) == before
    assert not list(live.parent.glob("pre-restore-*"))
    assert not list(live.parent.glob("*.restore-*.tmp*"))


def _make_unreadable_with_sidecars(live: Path) -> dict[str, bytes]:
    """Turn app.db into an unreadable file with -wal/-shm beside it; return all their bytes."""
    live.write_bytes(b"not a database " * 64)
    live.with_name("app.db-wal").write_bytes(b"wal bytes " * 64)
    live.with_name("app.db-shm").write_bytes(b"shm bytes " * 64)
    return _files_of(live)


def _files_of(live: Path) -> dict[str, bytes]:
    return {s: live.with_name("app.db" + s).read_bytes()
            for s in ("", "-wal", "-shm", "-journal") if live.with_name("app.db" + s).exists()}


def test_stale_side_files_are_moved_even_when_db_is_missing(live, tmp_path):
    (tmp_path / "b").mkdir()
    backup_path, _ = dbtools.backup(live, tmp_path / "b")
    # An interrupted earlier run: app.db is gone, its WAL/SHM are still there.
    _write_into_wal_only(live, "in-wal")
    wal_bytes = live.with_name("app.db-wal").read_bytes()
    live.unlink()
    assert live.with_name("app.db-wal").exists() and live.with_name("app.db-shm").exists()

    result = dbtools.restore(live, backup_path)

    assert result["ok"] and _usernames(live) == ["admin"]
    moved = live.parent / result["previous_files"]
    assert {p.name for p in moved.iterdir()} == {"app.db-wal", "app.db-shm"}
    assert (moved / "app.db-wal").read_bytes() == wal_bytes  # kept, not deleted
    for suffix in ("-wal", "-shm"):
        side = live.with_name("app.db" + suffix)
        assert not side.exists() or side.read_bytes() != wal_bytes


def test_restored_database_is_a_single_file_that_the_api_can_open(live, tmp_path):
    (tmp_path / "b").mkdir()
    backup_path, _ = dbtools.backup(live, tmp_path / "b")
    con = sqlite3.connect(backup_path)
    assert con.execute("PRAGMA journal_mode").fetchone()[0] == "delete"  # standalone backup file
    con.close()
    dbtools.restore(live, backup_path)
    db = Database(f"sqlite:///{live}")
    try:
        assert db.migrate() == "already at head"
        with db.engine.connect() as conn:
            assert conn.execute(text("PRAGMA journal_mode")).scalar() == "wal"  # api switches it back
    finally:
        db.engine.dispose()


def test_failed_backup_leaves_no_partial_file(live, tmp_path, monkeypatch):
    def failing_copy(src, dest):
        Path(dest).write_bytes(b"partial")
        raise sqlite3.OperationalError("database or disk is full")

    monkeypatch.setattr(dbtools, "_copy", failing_copy)
    with pytest.raises(sqlite3.OperationalError):
        dbtools.backup(live, tmp_path)
    assert not list(tmp_path.glob("*.bak*"))


@pytest.mark.parametrize("exc", [sqlite3.OperationalError("disk I/O error"), PermissionError("Permission denied")])
def test_cli_reports_unexpected_errors_without_traceback(live, tmp_path, monkeypatch, capsys, exc):
    from ai_surveillance import cli
    from ai_surveillance.config import Settings

    settings = Settings(database_url=f"sqlite:///{live}", _env_file=None)
    monkeypatch.setattr(cli, "get_settings", lambda: settings)

    def boom(*args, **kwargs):
        raise exc

    monkeypatch.setattr(dbtools, "restore", boom)
    monkeypatch.setattr(dbtools, "backup", boom)
    (tmp_path / "x.bak").write_bytes(b"x")
    assert cli.main(["db", "restore", str(tmp_path / "x.bak"), "--yes"]) == 5
    err = capsys.readouterr().err
    assert "active database unchanged" in err and "Traceback" not in err
    assert cli.main(["db", "backup"]) == 5
    assert "no backup written" in capsys.readouterr().err


def test_cli_refuses_self_restore(live, monkeypatch, capsys):
    from ai_surveillance import cli
    from ai_surveillance.config import Settings

    settings = Settings(database_url=f"sqlite:///{live}", _env_file=None)
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    before = _snapshot(live)
    assert cli.main(["db", "restore", str(live), "--yes"]) == 4
    assert "onto itself" in capsys.readouterr().err
    assert _snapshot(live) == before


# --- audit findings -------------------------------------------------------------

def _good_backup(tmp_path: Path, username: str) -> Path:
    path = tmp_path / "src" / "good.bak"
    path.parent.mkdir(exist_ok=True)
    db = Database(f"sqlite:///{path}")
    db.migrate(backup=False)
    with db.sessionmaker() as s:
        s.add(AdminUser(username=username, password_hash="h"))
        s.commit()
    db.engine.dispose()
    con = sqlite3.connect(path)
    con.execute("PRAGMA journal_mode=DELETE")  # single file, so it can be copied around freely
    con.close()
    return path


@pytest.mark.parametrize("name", [
    "app.db#before-upgrade.bak",  # the part before '#' is the active database
    "nightly #3.bak",
    "50%25 off ü.bak",  # a literal '%25' must not be decoded to '%'
    pytest.param("app.db?mode=rw.bak", marks=pytest.mark.skipif(os.name == "nt", reason="'?' not allowed on Windows")),
])
def test_special_characters_in_backup_name_restore_that_file(live, tmp_path, name):
    good = _good_backup(tmp_path, "bob")
    tricky = live.parent / name
    shutil.copy2(good, tricky)
    result = dbtools.restore(live, tricky)
    assert result["ok"] and result["restored_from"] == name
    assert _usernames(live) == ["bob"]  # the named backup, not the active database


def test_verify_with_hash_in_name_creates_no_stray_file(tmp_path, live):
    good = _good_backup(tmp_path, "bob")
    tricky = tmp_path / "nightly#3.bak"
    shutil.copy2(good, tricky)
    assert dbtools.verify(tricky)["ok"] is True
    assert not (tmp_path / "nightly").exists()


def test_sqlite_uri_escapes_reserved_characters():
    uri = dbtools.sqlite_uri(Path("/data/a#b?c%d e.bak"))
    assert uri == "file:/data/a%23b%3Fc%25d%20e.bak?mode=ro"


def test_app_db_exists_at_the_moment_of_the_swap(live, tmp_path, monkeypatch):
    good = _good_backup(tmp_path, "bob")
    real_replace = os.replace
    seen = {}

    def checking_replace(src, dst):
        seen["db_present_before_swap"] = Path(dst).exists()
        seen["db_users_before_swap"] = _usernames(Path(dst))
        return real_replace(src, dst)

    monkeypatch.setattr(dbtools.os, "replace", checking_replace)
    dbtools.restore(live, good)
    assert seen == {"db_present_before_swap": True, "db_users_before_swap": ["admin"]}
    assert _usernames(live) == ["bob"]


def test_hard_crash_before_swap_leaves_old_database_in_place(live, tmp_path):
    """Kill the process right before the swap: app.db must still be the old, intact database."""
    import subprocess
    import sys

    good = _good_backup(tmp_path, "bob")
    child = (
        "import os; from pathlib import Path; from ai_surveillance import dbtools;"
        "os.replace = lambda a, b: os._exit(9);"
        f"dbtools.restore(Path({str(live)!r}), Path({str(good)!r}))"
    )
    rc = subprocess.run([sys.executable, "-c", child], capture_output=True).returncode
    assert rc == 9
    assert live.exists() and _usernames(live) == ["admin"]
    temps = list(live.parent.glob("*.restore-*.tmp"))
    assert len(temps) == 1 and dbtools.verify(temps[0])["ok"]  # the verified copy survives
    (pre,) = live.parent.glob("pre-restore-*")
    assert _usernames(pre / "app.db") == ["admin"]
    # The api would start on the old database, not create an empty one.
    db = Database(f"sqlite:///{live}")
    try:
        assert db.migrate(backup=False) == "already at head"
    finally:
        db.engine.dispose()
    # The preserved copy is an independent snapshot: later writes to app.db
    # (e.g. the api running after the crash) do not change it...
    con = sqlite3.connect(live)
    con.execute("INSERT INTO admin_users (id, username, password_hash, created_at) "
                "VALUES ('later', 'written-after-crash', 'h', '2026-10-24 00:00:00')")
    con.commit()
    con.close()
    assert _usernames(pre / "app.db") == ["admin"]
    # ...and the documented recovery command (restore the pre-restore copy) works.
    result = dbtools.restore(live, pre / "app.db")
    assert result["ok"] and _usernames(live) == ["admin"]


def test_post_swap_owner_failure_is_a_warning_not_an_error(live, tmp_path, monkeypatch, capsys):
    from ai_surveillance import cli
    from ai_surveillance.config import Settings

    good = _good_backup(tmp_path, "bob")
    real = dbtools._match_directory_owner

    def chown_fails(path, recursive=False):
        if recursive:
            raise PermissionError("Operation not permitted")
        return real(path, recursive)

    monkeypatch.setattr(dbtools, "_match_directory_owner", chown_fails)
    monkeypatch.setattr(cli, "get_settings", lambda: Settings(database_url=f"sqlite:///{live}", _env_file=None))
    assert cli.main(["db", "restore", str(good), "--yes"]) == 3
    out = capsys.readouterr()
    assert json.loads(out.out)["restored"] is True
    assert "Restored, with warnings" in out.err and "could not change the owner" in out.err
    assert "unchanged" not in out.err and "Not done" not in out.err
    assert _usernames(live) == ["bob"]


def test_post_swap_verify_error_is_reported_as_restored(live, tmp_path, monkeypatch, capsys):
    from ai_surveillance import cli
    from ai_surveillance.config import Settings

    good = _good_backup(tmp_path, "bob")
    real_verify = dbtools.verify
    calls = {"n": 0}

    def third_verify_raises(path):
        calls["n"] += 1
        if calls["n"] == 3:  # source, temp copy, then the final check after the swap
            raise dbtools.InvalidBackup("simulated read error")
        return real_verify(path)

    monkeypatch.setattr(dbtools, "verify", third_verify_raises)
    monkeypatch.setattr(cli, "get_settings", lambda: Settings(database_url=f"sqlite:///{live}", _env_file=None))
    assert cli.main(["db", "restore", str(good), "--yes"]) == 3
    out = capsys.readouterr()
    result = json.loads(out.out)
    assert result["restored"] is True and result["ok"] is False
    assert "could not be verified" in out.err and "Not done" not in out.err
    assert _usernames(live) == ["bob"]


def _break_rollback(monkeypatch):
    """os.replace fails, and moving files back out of pre-restore fails too."""
    real_move = dbtools.shutil.move

    def move(src, dst):
        if "pre-restore" in str(src):  # moving back during rollback
            raise PermissionError("rollback blocked")
        return real_move(src, dst)

    def replace_fails(src, dst):
        raise OSError("rename failed")

    monkeypatch.setattr(dbtools.shutil, "move", move)
    monkeypatch.setattr(dbtools.os, "replace", replace_fails)


def test_failed_rollback_reports_uncertain_paths_and_keeps_cause(live, tmp_path, monkeypatch):
    good = _good_backup(tmp_path, "bob")
    before = _make_unreadable_with_sidecars(live)
    _break_rollback(monkeypatch)
    with pytest.raises(dbtools.RestoreStateUncertain) as exc:
        dbtools.restore(live, good, replace_unreadable=True)
    assert isinstance(exc.value.__cause__, OSError) and "rename failed" in str(exc.value.__cause__)
    assert {p.split(":")[0] for p in exc.value.paths} == {"app.db-wal", "app.db-shm"}
    assert all("pre-restore-" in p for p in exc.value.paths)  # each path says where the file is
    assert "is the previous database" in str(exc.value)
    assert live.read_bytes() == before[""]  # app.db itself never moved
    (pre,) = live.parent.glob("pre-restore-*")
    # The stranded side files stay next to the snapshot of the file they belong to.
    assert {p.name: p.read_bytes() for p in pre.iterdir()} == {
        "app.db": before[""], "app.db-wal": before["-wal"], "app.db-shm": before["-shm"]}


def test_cli_reports_uncertain_state_not_unchanged(live, tmp_path, monkeypatch, capsys):
    from ai_surveillance import cli
    from ai_surveillance.config import Settings

    good = _good_backup(tmp_path, "bob")
    _make_unreadable_with_sidecars(live)
    _break_rollback(monkeypatch)
    monkeypatch.setattr(cli, "get_settings", lambda: Settings(database_url=f"sqlite:///{live}", _env_file=None))
    assert cli.main(["db", "restore", str(good), "--yes", "--replace-unreadable"]) == 6
    err = capsys.readouterr().err
    assert "State uncertain" in err and "app.db-wal" in err and "pre-restore-" in err
    assert "Do not start the api" in err
    assert "unchanged" not in err and "Traceback" not in err


def test_two_restores_in_the_same_second_do_not_collide(live, tmp_path):
    good = _good_backup(tmp_path, "bob")
    first = dbtools.restore(live, good)
    second = dbtools.restore(live, good)
    assert first["previous_files"] != second["previous_files"]
    assert _usernames(live) == ["bob"]


def test_unreadable_active_database_needs_explicit_consent(live, tmp_path, monkeypatch, capsys):
    from ai_surveillance import cli
    from ai_surveillance.config import Settings

    good = _good_backup(tmp_path, "bob")
    garbage = b"this is not a database" * 50
    live.write_bytes(garbage)
    monkeypatch.setattr(cli, "get_settings", lambda: Settings(database_url=f"sqlite:///{live}", _env_file=None))
    assert cli.main(["db", "restore", str(good), "--yes"]) == 4
    err = capsys.readouterr().err
    assert "Not done (active database unchanged)" in err and "--replace-unreadable" in err
    assert live.read_bytes() == garbage
    assert not list(live.parent.glob("pre-restore-*")) and not list(live.parent.glob("*.restore-*.tmp*"))


def test_corrupt_active_database_can_be_replaced_with_consent(live, tmp_path, monkeypatch, capsys):
    from ai_surveillance import cli
    from ai_surveillance.config import Settings

    good = _good_backup(tmp_path, "bob")
    before = _make_unreadable_with_sidecars(live)
    monkeypatch.setattr(cli, "get_settings", lambda: Settings(database_url=f"sqlite:///{live}", _env_file=None))
    assert cli.main(["db", "restore", str(good), "--yes", "--replace-unreadable"]) == 0
    out = capsys.readouterr()
    result = json.loads(out.out)
    assert result["previous_database_was_readable"] is False
    assert result["previous_database_preserved_by"] == "file copy" and result["warnings"] == []
    assert "Note: the previous database was unreadable or damaged" in out.err
    assert "could not be reliably ruled out" in out.err
    assert _usernames(live) == ["bob"]
    assert not live.with_name("app.db-wal").exists() and not live.with_name("app.db-shm").exists()
    pre = live.parent / result["previous_files"]
    # The corrupt file and its side files are kept byte for byte, together.
    assert {p.name: p.read_bytes() for p in pre.iterdir()} == {
        "app.db": before[""], "app.db-wal": before["-wal"], "app.db-shm": before["-shm"]}


def _wreck_body(path: Path) -> None:
    """Keep SQLite's 100-byte header, overwrite every page behind it."""
    data = bytearray(path.read_bytes())
    data[100:] = b"\xab" * (len(data) - 100)
    path.write_bytes(bytes(data))


def test_damaged_database_with_valid_header_needs_consent(live, tmp_path, monkeypatch, capsys):
    from ai_surveillance import cli
    from ai_surveillance.config import Settings

    good = _good_backup(tmp_path, "bob")
    _wreck_body(live)
    damaged = live.read_bytes()
    assert dbtools.has_sqlite_header(live)
    monkeypatch.setattr(cli, "get_settings", lambda: Settings(database_url=f"sqlite:///{live}", _env_file=None))
    assert cli.main(["db", "restore", str(good), "--yes"]) == 4
    assert "unreadable or damaged" in capsys.readouterr().err
    assert live.read_bytes() == damaged and not list(live.parent.glob("pre-restore-*"))


def test_damaged_database_with_valid_header_is_replaced_and_kept(live, tmp_path):
    """Regression: the SQLite-level snapshot failed with 'database disk image is malformed'."""
    good = _good_backup(tmp_path, "bob")
    _wreck_body(live)
    damaged = live.read_bytes()
    result = dbtools.restore(live, good, replace_unreadable=True)
    assert result["ok"] and result["previous_database_was_readable"] is False
    assert result["previous_database_preserved_by"] == "file copy"
    assert _usernames(live) == ["bob"]
    assert (live.parent / result["previous_files"] / "app.db").read_bytes() == damaged


def test_failed_snapshot_is_cleaned_up_and_db_untouched(live, tmp_path, monkeypatch):
    good = _good_backup(tmp_path, "bob")
    before = _snapshot(live)

    def snapshot_fails(db_path, dest, readable):
        Path(dest).write_bytes(b"partial copy")
        raise OSError("no space left on device")

    monkeypatch.setattr(dbtools, "_snapshot", snapshot_fails)
    with pytest.raises(OSError, match="no space left"):
        dbtools.restore(live, good)
    assert _snapshot(live) == before
    assert not list(live.parent.glob("pre-restore-*"))  # partial copy and empty dir removed
    assert not list(live.parent.glob("*.restore-*.tmp*"))


def test_idle_open_connection_is_detected(live, tmp_path):
    """A connection that is open but idle holds no lock; its side files give it away."""
    good = _good_backup(tmp_path, "bob")
    idle = sqlite3.connect(live)
    idle.execute("PRAGMA wal_autocheckpoint=0")
    idle.execute("INSERT INTO admin_users (id, username, password_hash, created_at) "
                 "VALUES ('i', 'idle-write', 'h', '2026-10-24 00:00:00')")
    idle.commit()  # committed, still only in the WAL; the connection stays open
    try:
        with pytest.raises(dbtools.DatabaseInUse, match="another connection appears to be open"):
            dbtools.restore(live, good)
        assert not list(live.parent.glob("pre-restore-*")) and not list(live.parent.glob("*.restore-*.tmp*"))
        assert live.with_name("app.db-wal").exists()  # its WAL was not touched
        assert sorted(r[0] for r in idle.execute("SELECT username FROM admin_users")) == ["admin", "idle-write"]
    finally:
        idle.close()
    assert sorted(_usernames(live)) == ["admin", "idle-write"]


def test_preserved_copy_is_an_independent_snapshot(live, tmp_path):
    good = _good_backup(tmp_path, "bob")
    result = dbtools.restore(live, good)
    pre = live.parent / result["previous_files"] / "app.db"
    assert result["previous_database_preserved_by"] == "sqlite backup"
    assert not os.path.samefile(pre, live)
    assert _usernames(pre) == ["admin"] and dbtools.verify(pre)["ok"]


def test_verify_is_read_only(tmp_path, live):
    """verify writes nothing next to a WAL-mode file that has no side files."""
    con = sqlite3.connect(live)
    assert con.execute("PRAGMA journal_mode").fetchone()[0] == "wal"  # the api's mode
    con.close()
    before = sorted(p.name for p in live.parent.iterdir())
    assert dbtools.verify(live)["ok"]
    assert sorted(p.name for p in live.parent.iterdir()) == before


def test_verify_still_reads_an_existing_wal(live, tmp_path):
    _write_into_wal_only(live, "in-wal")  # app.db + a -wal holding a committed row
    wal_before = live.with_name("app.db-wal").read_bytes()
    assert dbtools.verify(live)["ok"]
    con = dbtools._ro(live)
    try:
        assert sorted(r[0] for r in con.execute("SELECT username FROM admin_users")) == ["admin", "in-wal"]
    finally:
        con.close()
    assert live.with_name("app.db-wal").read_bytes() == wal_before  # not checkpointed or removed


def test_symlink_to_active_database_is_refused(live, tmp_path):
    link = tmp_path / "looks-like-a-backup.bak"
    try:
        link.symlink_to(live)
    except (OSError, NotImplementedError):
        pytest.skip("creating symlinks is not permitted here")
    before = _snapshot(live)
    with pytest.raises(dbtools.InvalidBackup, match="onto itself"):
        dbtools.restore(live, link)
    assert _snapshot(live) == before

def test_cli_refusal_states_database_unchanged(live, tmp_path, monkeypatch, capsys):
    from ai_surveillance import cli
    from ai_surveillance.config import Settings

    bad = tmp_path / "bad.bak"
    bad.write_bytes(b"x" * 500)
    monkeypatch.setattr(cli, "get_settings", lambda: Settings(database_url=f"sqlite:///{live}", _env_file=None))
    before = _snapshot(live)
    assert cli.main(["db", "restore", str(bad), "--yes"]) == 4
    assert "Not done (active database unchanged)" in capsys.readouterr().err
    assert _snapshot(live) == before


def test_restore_into_empty_location(tmp_path, live):
    backup_path, _ = dbtools.backup(live, tmp_path)
    target = tmp_path / "new" / "app.db"
    target.parent.mkdir()
    result = dbtools.restore(target, backup_path)
    assert result["ok"] and result["previous_files"] is None
    assert _usernames(target) == ["admin"]


def test_cli_backup_verify_restore(live, tmp_path, monkeypatch, capsys):
    from ai_surveillance import cli
    from ai_surveillance.config import Settings

    settings = Settings(database_url=f"sqlite:///{live}", _env_file=None)
    monkeypatch.setattr(cli, "get_settings", lambda: settings)

    assert cli.main(["db", "backup", str(tmp_path)]) == 0
    backup_file = json.loads(capsys.readouterr().out)["backup"]
    assert cli.main(["db", "verify", backup_file]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
    assert cli.main(["db", "restore", backup_file]) == 2  # refuses without --yes
    assert "Stop the api first" in capsys.readouterr().err
    assert cli.main(["db", "restore", backup_file, "--yes"]) == 0
    assert json.loads(capsys.readouterr().out)["integrity"] == "ok"
    bad = tmp_path / "bad.bak"
    bad.write_bytes(b"x" * 500)
    assert cli.main(["db", "verify", str(bad)]) == 4
