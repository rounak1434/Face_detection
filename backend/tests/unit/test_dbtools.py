"""Backup, verify and restore of the SQLite database, on temporary files only."""

import json
import os
import shutil
import sqlite3
import stat
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
    assert result["ok"] and result["integrity"] == "ok" and result["revision"] == "0002_device_use_in_frigate"
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
    assert "is still the previous database" in str(exc.value)
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


def _damaged_with_wal(live: Path) -> dict[str, bytes]:
    """Valid header, damaged pages, plus a -wal holding a committed row and its -shm."""
    _write_into_wal_only(live, "in-wal")
    _wreck_body(live)
    files = _files_of(live)
    assert set(files) == {"", "-wal", "-shm"} and dbtools.has_sqlite_header(live)
    return files


def test_damaged_wal_database_is_not_changed_by_a_refusal(live, tmp_path, monkeypatch, capsys):
    """Regression: the in-use probe's closing folded the -wal into app.db and deleted -wal/-shm,
    while the CLI said 'active database unchanged'."""
    from ai_surveillance import cli
    from ai_surveillance.config import Settings

    good = _good_backup(tmp_path, "bob")
    before = _damaged_with_wal(live)
    monkeypatch.setattr(cli, "get_settings", lambda: Settings(database_url=f"sqlite:///{live}", _env_file=None))
    assert cli.main(["db", "restore", str(good), "--yes"]) == 4
    assert "Not done (active database unchanged)" in capsys.readouterr().err
    assert _files_of(live) == before  # app.db, -wal and -shm byte for byte
    assert sorted(p.name for p in live.parent.iterdir()) == ["app.db", "app.db-shm", "app.db-wal", "src"]


def test_damaged_wal_database_is_kept_byte_for_byte_when_replaced(live, tmp_path):
    good = _good_backup(tmp_path, "bob")
    before = _damaged_with_wal(live)
    result = dbtools.restore(live, good, replace_unreadable=True)
    assert result["ok"] and result["previous_database_was_readable"] is False
    assert result["previous_database_preserved_by"] == "file copy"
    assert _usernames(live) == ["bob"]
    pre = live.parent / result["previous_files"]
    assert {p.name: p.read_bytes() for p in pre.iterdir()} == {
        "app.db": before[""], "app.db-wal": before["-wal"], "app.db-shm": before["-shm"]}
    assert not list(live.parent.glob("app.db.check-*"))


def test_intact_wal_database_check_leaves_no_scratch_copy(live, tmp_path):
    """An intact database with leftover side files (unclean stop) still restores normally."""
    good = _good_backup(tmp_path, "bob")
    _write_into_wal_only(live, "in-wal")
    result = dbtools.restore(live, good)
    assert result["ok"] and result["previous_database_preserved_by"] == "sqlite backup"
    assert sorted(_usernames(live.parent / result["previous_files"] / "app.db")) == ["admin", "in-wal"]
    assert not list(live.parent.glob("app.db.check-*"))


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


# --- final restore findings: durability, rollback conflicts, Ctrl-C, damage-check races -----

def _use_cli(monkeypatch, live):
    from ai_surveillance import cli
    from ai_surveillance.config import Settings

    monkeypatch.setattr(cli, "get_settings", lambda: Settings(database_url=f"sqlite:///{live}", _env_file=None))
    return cli


def _record_flushes(monkeypatch, events):
    real_replace = os.replace
    real_fsync_file = dbtools._fsync_file
    monkeypatch.setattr(dbtools, "_fsync_file", lambda p: (events.append(("file", Path(p).name)), real_fsync_file(p)))
    monkeypatch.setattr(dbtools, "_fsync_dir", lambda d: events.append(("dir", Path(d).name)))
    monkeypatch.setattr(dbtools.os, "replace", lambda a, b: (events.append(("replace", Path(b).name)), real_replace(a, b)))


@pytest.mark.parametrize("damaged", [False, True])
def test_snapshot_and_new_file_are_flushed_before_the_swap(live, tmp_path, monkeypatch, damaged):
    """Order only; a unit test cannot show that data survives a power loss."""
    good = _good_backup(tmp_path, "bob")
    if damaged:
        _make_unreadable_with_sidecars(live)
    events = []
    _record_flushes(monkeypatch, events)
    result = dbtools.restore(live, good, replace_unreadable=damaged)
    pre = result["previous_files"]
    swap = events.index(("replace", "app.db"))
    before_swap = events[:swap]
    assert ("file", "app.db") in before_swap  # the snapshot in pre-restore-<time>/
    assert any(e[0] == "file" and e[1].startswith("app.db.restore-") for e in before_swap)
    assert ("dir", pre) in before_swap and ("dir", live.parent.name) in before_swap
    assert ("dir", live.parent.name) in events[swap:]  # the rename itself
    assert result["previous_database_preserved_by"] == ("file copy" if damaged else "sqlite backup")


def test_failed_snapshot_flush_aborts_and_rolls_back(live, tmp_path, monkeypatch, capsys):
    good = _good_backup(tmp_path, "bob")
    before = _snapshot(live)

    def fsync_fails(path):
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(dbtools, "_fsync_file", fsync_fails)
    cli = _use_cli(monkeypatch, live)
    assert cli.main(["db", "restore", str(good), "--yes"]) == 5
    assert "Not done (active database unchanged)" in capsys.readouterr().err
    assert _snapshot(live) == before
    assert not list(live.parent.glob("pre-restore-*")) and not list(live.parent.glob("*.restore-*.tmp*"))


def test_fsync_file_flushes_a_real_file(tmp_path):
    f = tmp_path / "x.bin"
    f.write_bytes(b"data")
    dbtools._fsync_file(f)  # no error on this platform
    with pytest.raises(OSError):
        dbtools._fsync_file(tmp_path / "missing.bin")


def _new_wal_appears_then_swap_fails(live, monkeypatch, new_wal: bytes):
    def replace(src, dst):
        live.with_name("app.db-wal").write_bytes(new_wal)  # another process opened the database
        raise OSError("rename failed")

    monkeypatch.setattr(dbtools.os, "replace", replace)


def test_rollback_never_overwrites_a_side_file_that_appeared_meanwhile(live, tmp_path, monkeypatch):
    good = _good_backup(tmp_path, "bob")
    before = _make_unreadable_with_sidecars(live)
    _new_wal_appears_then_swap_fails(live, monkeypatch, b"newer wal")
    with pytest.raises(dbtools.RestoreStateUncertain) as exc:
        dbtools.restore(live, good, replace_unreadable=True)
    (pre,) = live.parent.glob("pre-restore-*")
    # Nothing recoverable is lost: the snapshot and the moved WAL stay together,
    # the newer WAL is untouched, the SHM went back, app.db never changed.
    assert {p.name: p.read_bytes() for p in pre.iterdir()} == {"app.db": before[""], "app.db-wal": before["-wal"]}
    assert live.with_name("app.db-wal").read_bytes() == b"newer wal"
    assert live.with_name("app.db-shm").read_bytes() == before["-shm"]
    assert live.read_bytes() == before[""]
    (entry,) = exc.value.paths
    assert entry.startswith("app.db-wal: kept in") and str(pre) in entry and "do not overwrite" in entry
    assert "is still the previous database" in str(exc.value)


def test_cli_reports_side_file_conflict_as_uncertain(live, tmp_path, monkeypatch, capsys):
    good = _good_backup(tmp_path, "bob")
    _make_unreadable_with_sidecars(live)
    _new_wal_appears_then_swap_fails(live, monkeypatch, b"newer wal")
    cli = _use_cli(monkeypatch, live)
    assert cli.main(["db", "restore", str(good), "--yes", "--replace-unreadable"]) == 6
    err = capsys.readouterr().err
    assert "State uncertain" in err and "Do not start the api" in err and "a new app.db-wal appeared" in err
    assert "unchanged" not in err and "Traceback" not in err


def test_ctrl_c_before_anything_changed(live, tmp_path, monkeypatch, capsys):
    good = _good_backup(tmp_path, "bob")
    before = _snapshot(live)

    def interrupted(src, dest):
        Path(dest).write_bytes(b"partial")
        raise KeyboardInterrupt

    monkeypatch.setattr(dbtools, "_copy", interrupted)
    cli = _use_cli(monkeypatch, live)
    assert cli.main(["db", "restore", str(good), "--yes"]) == 130
    err = capsys.readouterr().err
    assert "Not done (active database unchanged; everything done so far was rolled back): interrupted" in err
    assert _snapshot(live) == before and not list(live.parent.glob("*.restore-*.tmp*"))


def test_ctrl_c_during_snapshot_is_rolled_back(live, tmp_path, monkeypatch, capsys):
    good = _good_backup(tmp_path, "bob")
    before = _make_unreadable_with_sidecars(live)

    def interrupted(db_path, dest, readable):
        Path(dest).write_bytes(b"partial copy")
        raise KeyboardInterrupt

    monkeypatch.setattr(dbtools, "_snapshot", interrupted)
    cli = _use_cli(monkeypatch, live)
    assert cli.main(["db", "restore", str(good), "--yes", "--replace-unreadable"]) == 130
    assert "rolled back" in capsys.readouterr().err
    assert _files_of(live) == before
    assert not list(live.parent.glob("pre-restore-*")) and not list(live.parent.glob("*.restore-*.tmp*"))


def test_ctrl_c_right_after_the_rename_is_not_rolled_back(live, tmp_path, monkeypatch, capsys):
    """Rolling back here would put the old WAL next to the new database."""
    good = _good_backup(tmp_path, "bob")
    before = _make_unreadable_with_sidecars(live)
    real_replace = os.replace

    def replace_then_interrupt(src, dst):
        real_replace(src, dst)
        raise KeyboardInterrupt

    monkeypatch.setattr(dbtools.os, "replace", replace_then_interrupt)
    cli = _use_cli(monkeypatch, live)
    assert cli.main(["db", "restore", str(good), "--yes", "--replace-unreadable"]) == 3
    err = capsys.readouterr().err
    (pre,) = live.parent.glob("pre-restore-*")
    assert err.startswith("Interrupted: ") and "not verified" in err and str(pre) in err and "db verify" in err
    assert "Not done" not in err and "Traceback" not in err
    assert not live.with_name("app.db-wal").exists() and not live.with_name("app.db-shm").exists()
    assert _usernames(live) == ["bob"]
    assert {p.name: p.read_bytes() for p in pre.iterdir()} == {
        "app.db": before[""], "app.db-wal": before["-wal"], "app.db-shm": before["-shm"]}


def test_ctrl_c_during_final_checks_reports_restored_not_verified(live, tmp_path, monkeypatch, capsys):
    good = _good_backup(tmp_path, "bob")
    real_verify = dbtools.verify
    calls = {"n": 0}

    def verify(path):
        calls["n"] += 1
        if Path(path) == live and calls["n"] > 2:  # source and temp first, then the final check
            raise KeyboardInterrupt
        return real_verify(path)

    monkeypatch.setattr(dbtools, "verify", verify)
    cli = _use_cli(monkeypatch, live)
    assert cli.main(["db", "restore", str(good), "--yes"]) == 3
    err = capsys.readouterr().err
    assert "Interrupted: interrupted after the restored database replaced app.db" in err
    assert "pre-restore-" in err
    assert _usernames(live) == ["bob"]


def test_ctrl_c_during_rollback_is_uncertain(live, tmp_path, monkeypatch, capsys):
    good = _good_backup(tmp_path, "bob")
    _make_unreadable_with_sidecars(live)
    real_move = dbtools.shutil.move

    def move(src, dst):
        if "pre-restore" in str(src):
            raise KeyboardInterrupt
        return real_move(src, dst)

    def replace_fails(src, dst):
        raise OSError("rename failed")

    monkeypatch.setattr(dbtools.shutil, "move", move)
    monkeypatch.setattr(dbtools.os, "replace", replace_fails)
    cli = _use_cli(monkeypatch, live)
    assert cli.main(["db", "restore", str(good), "--yes", "--replace-unreadable"]) == 6
    err = capsys.readouterr().err
    assert "State uncertain" in err and "rollback was interrupted" in err
    assert "app.db-wal: in" in err and "app.db-shm: in" in err and "app.db: in" in err
    (pre,) = live.parent.glob("pre-restore-*")
    assert (pre / "app.db").exists()  # the snapshot is kept


def test_damage_check_refuses_if_files_change_during_it(live, tmp_path, monkeypatch, capsys):
    good = _good_backup(tmp_path, "bob")
    _damaged_with_wal(live)
    real_check = dbtools._quick_check

    def check_while_api_writes(path, con=None):
        with live.with_name("app.db-wal").open("ab") as fh:
            fh.write(b"\0" * 4096)
        return real_check(path, con)

    monkeypatch.setattr(dbtools, "_quick_check", check_while_api_writes)
    cli = _use_cli(monkeypatch, live)
    assert cli.main(["db", "restore", str(good), "--yes", "--replace-unreadable"]) == 4
    err = capsys.readouterr().err
    assert "changed while being checked" in err and "Not done (active database unchanged)" in err
    assert not list(live.parent.glob("pre-restore-*")) and not list(live.parent.glob("app.db.check-*"))


def test_damage_check_reports_a_scratch_copy_it_could_not_remove(live, tmp_path, monkeypatch, capsys):
    good = _good_backup(tmp_path, "bob")
    before = _damaged_with_wal(live)

    def rmtree_fails(path, *a, **k):
        raise PermissionError("in use")

    monkeypatch.setattr(dbtools.shutil, "rmtree", rmtree_fails)
    cli = _use_cli(monkeypatch, live)
    assert cli.main(["db", "restore", str(good), "--yes"]) == 4
    err = capsys.readouterr().err
    (scratch,) = live.parent.glob("app.db.check-*")
    assert "could not be removed" in err and str(scratch) in err
    assert _files_of(live) == before  # the originals are still untouched
    # A copy only (the check's own connection folded the copied WAL into it).
    assert sorted(p.name for p in scratch.iterdir()) == ["app.db"]


def test_damage_check_leaves_a_journal_untouched(live, tmp_path, monkeypatch):
    good = _good_backup(tmp_path, "bob")
    _wreck_body(live)
    live.with_name("app.db-journal").write_bytes(b"\xd9\xd5\x05\xf9\x20\xa1\x63\xd7" + b"\x00" * 504)
    before = _files_of(live)
    with pytest.raises(dbtools.ActiveUnreadable, match="Nothing was changed"):
        dbtools.restore(live, good)
    assert _files_of(live) == before and not list(live.parent.glob("app.db.check-*"))


# --- final audit: Ctrl-C in the cleanup after the swap; read-only damaged database -----------

def _interrupt_cleanup_after(monkeypatch, condition):
    real_remove = dbtools._remove_temp

    def remove_then_interrupt(path):
        real_remove(path)
        if condition(path):
            raise KeyboardInterrupt

    monkeypatch.setattr(dbtools, "_remove_temp", remove_then_interrupt)


def test_ctrl_c_in_cleanup_after_the_swap_is_reported_as_restored(live, tmp_path, monkeypatch, capsys):
    """Regression: this was reported as 'active database unchanged ... rolled back' (exit 130)."""
    good = _good_backup(tmp_path, "bob")
    _interrupt_cleanup_after(monkeypatch, lambda path: not path.exists())
    cli = _use_cli(monkeypatch, live)
    assert cli.main(["db", "restore", str(good), "--yes"]) == 3
    err = capsys.readouterr().err
    assert err.startswith("Interrupted: ") and "not verified" in err and "pre-restore-" in err
    assert "unchanged" not in err and "rolled back" not in err and "Traceback" not in err
    assert _usernames(live) == ["bob"]


def test_ctrl_c_in_cleanup_after_failed_rollback_stays_uncertain(live, tmp_path, monkeypatch, capsys):
    """Regression: the Ctrl-C replaced the 'state uncertain' error with exit 130."""
    good = _good_backup(tmp_path, "bob")
    _make_unreadable_with_sidecars(live)
    _break_rollback(monkeypatch)
    _interrupt_cleanup_after(monkeypatch, lambda path: True)
    cli = _use_cli(monkeypatch, live)
    assert cli.main(["db", "restore", str(good), "--yes", "--replace-unreadable"]) == 6
    err = capsys.readouterr().err
    assert "State uncertain" in err and "interrupted (Ctrl-C) while cleaning up" in err
    assert "app.db-wal" in err and "rolled back" not in err.split("State uncertain")[0]


def test_ctrl_c_in_cleanup_before_the_swap_is_unchanged(live, tmp_path, monkeypatch, capsys):
    good = _good_backup(tmp_path, "bob")
    before = _snapshot(live)

    def replace_fails(src, dst):
        raise OSError("rename failed")

    monkeypatch.setattr(dbtools.os, "replace", replace_fails)
    _interrupt_cleanup_after(monkeypatch, lambda path: True)
    cli = _use_cli(monkeypatch, live)
    assert cli.main(["db", "restore", str(good), "--yes"]) == 130
    assert "active database unchanged" in capsys.readouterr().err
    assert _snapshot(live) == before and not list(live.parent.glob("pre-restore-*"))


def test_vanished_temporary_file_is_not_mistaken_for_a_swap(live, tmp_path, monkeypatch, capsys):
    """If the temporary copy disappears before the rename, app.db is still the old database."""
    good = _good_backup(tmp_path, "bob")
    before = _snapshot(live)
    real_fsync = dbtools._fsync_file

    def fsync(path):
        if Path(path).name.startswith("app.db.restore-"):
            Path(path).unlink()
        return real_fsync(path)

    monkeypatch.setattr(dbtools, "_fsync_file", fsync)
    cli = _use_cli(monkeypatch, live)
    assert cli.main(["db", "restore", str(good), "--yes"]) == 5
    err = capsys.readouterr().err
    assert "Not done (active database unchanged)" in err and "Interrupted" not in err
    assert _snapshot(live) == before and not list(live.parent.glob("pre-restore-*"))


def test_swap_detection_uses_file_identity(live, tmp_path):
    a, b = tmp_path / "a.bin", tmp_path / "b.bin"
    a.write_bytes(b"a")
    b.write_bytes(b"b")
    ident = dbtools._identity(a)
    assert ident != dbtools._identity(b)
    os.replace(a, b)
    assert dbtools._identity(b) == ident


@pytest.fixture
def read_only(live):
    yield live
    for p in [live, *live.parent.rglob("*")]:
        try:
            os.chmod(p, 0o600 if p.is_file() else 0o700)
        except OSError:
            pass


def test_byte_copy_of_a_read_only_file_is_flushed_and_keeps_its_metadata(read_only, tmp_path, monkeypatch):
    """Regression: the flush opened the read-only byte copy for writing and failed (exit 5)."""
    live = read_only
    live.write_bytes(b"not a database " * 64)
    os.chmod(live, stat.S_IREAD)
    dest = tmp_path / "copy.db"
    flushed = []
    real_fsync = dbtools._fsync_file

    def fsync(path):
        flushed.append(os.access(path, os.W_OK))  # still writable when flushed
        return real_fsync(path)

    monkeypatch.setattr(dbtools, "_fsync_file", fsync)
    assert dbtools._snapshot(live, dest, readable=False) == "file copy"
    assert flushed == [True]
    assert dest.read_bytes() == live.read_bytes()
    assert not os.access(dest, os.W_OK) and dest.stat().st_mtime_ns == live.stat().st_mtime_ns


@pytest.mark.skipif(os.name == "nt", reason="Windows refuses to rename over a read-only file (see the next test)")
def test_read_only_damaged_database_is_replaced_and_kept(read_only, tmp_path):
    live = read_only
    good = _good_backup(tmp_path, "bob")
    live.write_bytes(b"not a database " * 64)
    os.chmod(live, stat.S_IREAD)
    damaged = live.read_bytes()
    mtime = live.stat().st_mtime_ns
    result = dbtools.restore(live, good, replace_unreadable=True)
    assert result["ok"] and result["previous_database_preserved_by"] == "file copy"
    assert _usernames(live) == ["bob"]
    copy = live.parent / result["previous_files"] / "app.db"
    assert copy.read_bytes() == damaged
    assert not os.access(copy, os.W_OK)  # permissions kept, as with copy2
    assert copy.stat().st_mtime_ns == mtime


@pytest.mark.skipif(os.name != "nt", reason="Windows-only behaviour")
def test_read_only_damaged_database_on_windows_is_refused_cleanly(read_only, tmp_path, monkeypatch, capsys):
    """Windows will not rename over a read-only file; the restore rolls back and says so."""
    live = read_only
    good = _good_backup(tmp_path, "bob")
    live.write_bytes(b"not a database " * 64)
    os.chmod(live, stat.S_IREAD)
    damaged = live.read_bytes()
    cli = _use_cli(monkeypatch, live)
    assert cli.main(["db", "restore", str(good), "--yes", "--replace-unreadable"]) == 5
    err = capsys.readouterr().err
    assert "Not done (active database unchanged): PermissionError" in err
    assert live.read_bytes() == damaged
    assert not list(live.parent.glob("pre-restore-*")) and not list(live.parent.glob("*.restore-*.tmp*"))


def test_read_only_damaged_database_rollback_leaves_nothing(read_only, tmp_path, monkeypatch):
    live = read_only
    good = _good_backup(tmp_path, "bob")
    live.write_bytes(b"not a database " * 64)
    os.chmod(live, stat.S_IREAD)
    damaged = live.read_bytes()

    def replace_fails(src, dst):
        raise OSError("rename failed")

    monkeypatch.setattr(dbtools.os, "replace", replace_fails)
    with pytest.raises(OSError, match="rename failed"):
        dbtools.restore(live, good, replace_unreadable=True)
    assert live.read_bytes() == damaged
    assert not list(live.parent.glob("pre-restore-*"))  # the read-only copy was removed too


# --- CLI output after the operation finished ------------------------------------------------

def _fail_output(monkeypatch, exc, on_call=1):
    """Make the n-th print of the result fail, as a closed pipe or a Ctrl-C would."""
    from ai_surveillance import cli

    real_dumps = cli.json.dumps
    calls = {"n": 0}

    def dumps(*a, **k):
        calls["n"] += 1
        if calls["n"] == on_call:
            raise exc
        return real_dumps(*a, **k)

    monkeypatch.setattr(cli.json, "dumps", dumps)


@pytest.mark.parametrize("exc, reason", [(KeyboardInterrupt(), "Ctrl-C"),
                                         (BrokenPipeError(32, "Broken pipe"), "BrokenPipeError")])
def test_output_failure_after_restore_is_reported_as_restored(live, tmp_path, monkeypatch, capsys, exc, reason):
    """Regression: this was reported as 'Not done (active database unchanged)' (exit 130 / 5)."""
    good = _good_backup(tmp_path, "bob")
    cli = _use_cli(monkeypatch, live)
    _fail_output(monkeypatch, exc)
    assert cli.main(["db", "restore", str(good), "--yes"]) == 3
    err = capsys.readouterr().err
    assert err.startswith("Restored: the restored database is active as app.db and verified")
    assert reason in err and "pre-restore-" in err and "db verify" in err
    assert "Not done" not in err and "unchanged" not in err and "Traceback" not in err
    assert _usernames(live) == ["bob"]


def test_output_failure_after_restore_with_warnings_repeats_them(live, tmp_path, monkeypatch, capsys):
    good = _good_backup(tmp_path, "bob")

    def owner_fails(path, recursive=False):
        if recursive:
            raise PermissionError("chown denied")

    monkeypatch.setattr(dbtools, "_match_directory_owner", owner_fails)
    cli = _use_cli(monkeypatch, live)
    _fail_output(monkeypatch, BrokenPipeError(32, "Broken pipe"))
    assert cli.main(["db", "restore", str(good), "--yes"]) == 3
    err = capsys.readouterr().err
    assert err.startswith("Restored:") and "Warning: could not change the owner" in err


@pytest.mark.parametrize("exc", [KeyboardInterrupt(), BrokenPipeError(32, "Broken pipe")])
def test_output_failure_after_backup_is_reported_as_written(live, tmp_path, monkeypatch, capsys, exc):
    cli = _use_cli(monkeypatch, live)
    _fail_output(monkeypatch, exc)
    assert cli.main(["db", "backup", str(tmp_path)]) == 3
    err = capsys.readouterr().err
    (written,) = tmp_path.glob("app.db.manual-*.bak")
    assert err.startswith("Backup written and verified: ") and str(written) in err
    assert "no backup written" not in err and dbtools.verify(written)["ok"]


@pytest.mark.parametrize("exc, code", [(KeyboardInterrupt(), 130), (BrokenPipeError(32, "Broken pipe"), 5)])
def test_output_failure_after_verify_says_nothing_changed(live, tmp_path, monkeypatch, capsys, exc, code):
    cli = _use_cli(monkeypatch, live)
    _fail_output(monkeypatch, exc)
    assert cli.main(["db", "verify", str(live)]) == code
    assert capsys.readouterr().err.startswith("Nothing changed: verify finished")


def test_output_failure_with_stderr_gone_still_exits_truthfully(live, tmp_path, monkeypatch):
    from ai_surveillance import cli as cli_module

    good = _good_backup(tmp_path, "bob")
    cli = _use_cli(monkeypatch, live)
    _fail_output(monkeypatch, BrokenPipeError(32, "Broken pipe"))
    real_print = print

    def print_(*a, **k):
        if k.get("file") is cli_module.sys.stderr:
            raise BrokenPipeError(32, "Broken pipe")
        return real_print(*a, **k)

    monkeypatch.setattr("builtins.print", print_)
    assert cli.main(["db", "restore", str(good), "--yes"]) == 3
    assert _usernames(live) == ["bob"]


def test_failure_before_the_swap_is_still_reported_as_unchanged(live, tmp_path, monkeypatch, capsys):
    """The operation's own failures keep their meaning after the split."""
    good = _good_backup(tmp_path, "bob")
    before = _snapshot(live)

    def replace_fails(src, dst):
        raise OSError("rename failed")

    monkeypatch.setattr(dbtools.os, "replace", replace_fails)
    cli = _use_cli(monkeypatch, live)
    assert cli.main(["db", "restore", str(good), "--yes"]) == 5
    assert "Not done (active database unchanged): OSError: rename failed" in capsys.readouterr().err
    assert _snapshot(live) == before
