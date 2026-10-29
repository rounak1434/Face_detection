"""Secret files with saved devices' stream URLs for Frigate (ADR-0003, step 2). Fake URLs only."""

import os
import stat

import pytest

from ai_surveillance import frigate_secrets
from ai_surveillance.frigate_config import secret_name
from ai_surveillance.frigate_secrets import FrigateSecrets, SecretWriteError

ID_A = "a" * 32
ID_B = "0123456789abcdef0123456789abcdef"
MAIN_A = secret_name(ID_A, "main")
SUB_A = secret_name(ID_A, "sub")
MAIN_B = secret_name(ID_B, "main")
URL_1 = "rtsp://viewer:fake-pass-1@192.0.2.10:554/main"
URL_2 = "rtsp://viewer:fake-pass-2@192.0.2.10:554/sub"
URL_3 = "rtsps://192.0.2.11:322/stream"


@pytest.fixture
def secrets(tmp_path):
    return FrigateSecrets(tmp_path / "secrets")


def _names(secrets):
    return sorted(p.name for p in secrets.directory.iterdir())


def test_creates_the_directory_and_starts_empty(secrets):
    assert secrets.directory.is_dir()
    assert secrets.read() == {}


def test_writes_one_file_per_stream_with_the_exact_url(secrets):
    secrets.replace_all({MAIN_A: URL_1, SUB_A: URL_2})
    assert _names(secrets) == sorted([MAIN_A, SUB_A])
    assert (secrets.directory / MAIN_A).read_bytes() == URL_1.encode()
    assert secrets.read() == {MAIN_A: URL_1, SUB_A: URL_2}


def test_update_replaces_changed_urls_and_removes_devices_no_longer_applied(secrets):
    secrets.replace_all({MAIN_A: URL_1, SUB_A: URL_2})
    secrets.replace_all({MAIN_A: URL_3, MAIN_B: URL_1})
    assert secrets.read() == {MAIN_A: URL_3, MAIN_B: URL_1}
    assert _names(secrets) == sorted([MAIN_A, MAIN_B])


def test_empty_mapping_removes_every_managed_file(secrets):
    secrets.replace_all({MAIN_A: URL_1})
    secrets.replace_all({})
    assert _names(secrets) == []


def test_files_that_are_not_ours_are_never_touched(secrets):
    other = {
        "FRIGATE_CAMERA_MAIN_URL": "rtsp://set-by-hand",
        "FRIGATE_DEV_SHORT_MAIN": "x",
        f"FRIGATE_DEV_{ID_A}_MAIN": "lowercase id is not ours",
        "README": "notes",
    }
    for name, content in other.items():
        (secrets.directory / name).write_text(content)
    secrets.replace_all({MAIN_B: URL_1})
    secrets.replace_all({})
    for name, content in other.items():
        assert (secrets.directory / name).read_text() == content
    assert secrets.read() == {}


def test_read_strips_content_like_frigate(secrets):
    (secrets.directory / MAIN_A).write_text(URL_1 + "\n")
    assert secrets.read() == {MAIN_A: URL_1}


def test_read_and_replace_restore_a_previous_snapshot(secrets):
    secrets.replace_all({MAIN_A: URL_1, SUB_A: URL_2})
    snapshot = secrets.read()
    secrets.replace_all({MAIN_B: URL_3})
    secrets.replace_all(snapshot)
    assert secrets.read() == {MAIN_A: URL_1, SUB_A: URL_2}
    assert _names(secrets) == sorted([MAIN_A, SUB_A])


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_files_are_owner_only(secrets):
    secrets.replace_all({MAIN_A: URL_1})
    mode = stat.S_IMODE((secrets.directory / MAIN_A).stat().st_mode)
    assert mode == 0o600


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_a_new_directory_is_owner_only(tmp_path):
    old = os.umask(0)
    try:
        directory = FrigateSecrets(tmp_path / "fresh").directory
    finally:
        os.umask(old)
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700


@pytest.mark.parametrize(
    "values",
    [
        {"FRIGATE_CAMERA_MAIN_URL": URL_1},
        {f"FRIGATE_DEV_{ID_A}_MAIN": URL_1},  # lowercase id
        {f"FRIGATE_DEV_{ID_A.upper()}_THIRD": URL_1},
        {"../" + MAIN_A: URL_1},
        {MAIN_A: "http://192.0.2.10/stream"},
        {MAIN_A: ""},
        {MAIN_A: "rtsp://"},
        {MAIN_A: "rtsp://192.0.2.10/main\nFRIGATE_X=1"},
        {MAIN_A: "rtsp://192.0.2.10/ma in"},
        {MAIN_A: "rtsp://192.0.2.10/" + "a" * frigate_secrets.MAX_URL_LENGTH},
        {MAIN_A: None},
    ],
)
def test_invalid_input_is_refused_before_anything_changes(secrets, values):
    secrets.replace_all({MAIN_B: URL_3})
    with pytest.raises(ValueError):
        secrets.replace_all({MAIN_A: URL_1, **values})
    assert secrets.read() == {MAIN_B: URL_3}
    assert _names(secrets) == [MAIN_B]


def test_validation_errors_never_contain_the_url(secrets):
    bad = "rtsp://viewer:fake-secret@192.0.2.10/a b"
    with pytest.raises(ValueError) as caught:
        secrets.replace_all({MAIN_A: bad})
    assert "fake-secret" not in str(caught.value)
    assert MAIN_A in str(caught.value)


def test_a_failed_write_leaves_the_visible_files_unchanged(secrets, monkeypatch):
    secrets.replace_all({MAIN_A: URL_1, SUB_A: URL_2})
    real_write = os.write
    calls = {"n": 0}

    def failing_write(fd, data):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError(28, "No space left on device")
        return real_write(fd, data)

    monkeypatch.setattr(frigate_secrets.os, "write", failing_write)
    with pytest.raises(SecretWriteError) as caught:
        secrets.replace_all({MAIN_A: URL_3, MAIN_B: URL_3})
    monkeypatch.undo()

    assert "No space left on device" in str(caught.value)
    assert "192.0.2" not in str(caught.value)
    assert secrets.read() == {MAIN_A: URL_1, SUB_A: URL_2}
    assert _names(secrets) == sorted([MAIN_A, SUB_A])  # no temporary file left


def test_a_failed_rename_cleans_up_and_reports_without_urls(secrets, monkeypatch):
    secrets.replace_all({MAIN_A: URL_1})

    def failing_replace(src, dst):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(frigate_secrets.os, "replace", failing_replace)
    with pytest.raises(SecretWriteError) as caught:
        secrets.replace_all({MAIN_A: URL_3})
    monkeypatch.undo()

    assert "Permission denied" in str(caught.value)
    assert "192.0.2" not in str(caught.value)
    assert secrets.read() == {MAIN_A: URL_1}
    assert _names(secrets) == [MAIN_A]


def test_an_unreadable_directory_gives_secret_write_error_never_a_raw_os_error(secrets, monkeypatch):
    """Listing fails (permissions, a vanished mount): no OSError may escape, and the path is not shown."""
    secrets.replace_all({MAIN_A: URL_1})

    def refuse(self):
        raise PermissionError(13, "Permission denied", str(self))

    monkeypatch.setattr(type(secrets.directory), "iterdir", refuse)
    for call in (lambda: secrets.replace_all({MAIN_B: URL_3}), secrets.read):
        with pytest.raises(SecretWriteError) as caught:
            call()
        assert "Permission denied" in str(caught.value) and str(secrets.directory) not in str(caught.value)


def test_a_secret_file_that_is_not_utf8_cannot_be_read(secrets):
    (secrets.directory / MAIN_A).write_bytes(b"rtsp://\xff\xfe")
    with pytest.raises(SecretWriteError, match=f"{MAIN_A} is not valid UTF-8"):
        secrets.read()


def test_leftover_temporary_files_are_removed(secrets):
    (secrets.directory / f".tmp-{MAIN_A}").write_text(URL_1[:10])
    assert secrets.remove_leftovers() == 1
    secrets.replace_all({MAIN_B: URL_3})
    (secrets.directory / f".tmp-{MAIN_B}").write_text("partial")
    secrets.replace_all({MAIN_B: URL_1})
    assert _names(secrets) == [MAIN_B]


def test_temporary_names_are_invisible_to_frigate(secrets):
    """Frigate reads only files whose names start with FRIGATE_."""
    assert not frigate_secrets._TEMP_PREFIX.startswith("FRIGATE_")


@pytest.mark.skipif(os.name != "posix", reason="symlinks need privileges on Windows")
def test_a_symlink_with_a_managed_name_is_replaced_not_followed(secrets, tmp_path):
    target = tmp_path / "outside"
    target.write_text("must stay")
    (secrets.directory / MAIN_A).symlink_to(target)
    assert secrets.read() == {}
    secrets.replace_all({MAIN_A: URL_1})
    assert target.read_text() == "must stay"
    assert secrets.read() == {MAIN_A: URL_1}
