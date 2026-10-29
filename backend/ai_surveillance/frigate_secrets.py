"""Secret files holding saved devices' stream URLs for Frigate (ADR-0003).

Frigate substitutes `{FRIGATE_*}` placeholders from files in /run/secrets,
once, at startup. The api writes one file per device stream, named by
`frigate_config.secret_name`, into a directory shared only with Frigate.

- Files are created owner-only (0600) and replaced atomically: a reader
  sees the old URL or the new one, never a partial file.
- `replace_all` validates everything first and writes every new file to a
  temporary name before renaming any, so a failed write (disk full, no
  permission) leaves the visible set unchanged.
- Files for devices no longer applied are removed. Files whose names are
  not ours (anything but `FRIGATE_DEV_<id>_MAIN|SUB`) are never touched.
- URLs never appear in exceptions or logs; errors name the file only.

`read()` plus `replace_all()` is the snapshot and restore used when Frigate
does not come back after an apply.
"""

import os
import re
from collections.abc import Mapping
from pathlib import Path

_MANAGED = re.compile(r"^FRIGATE_DEV_[0-9A-F]{32}_(MAIN|SUB)$")
_TEMP_PREFIX = ".tmp-"  # Frigate reads only names starting with FRIGATE_
_URL = re.compile(r"^rtsps?://[^\s\x00-\x1f\x7f]+$")
MAX_URL_LENGTH = 2048


class SecretWriteError(Exception):
    """The secret files could not be written; the message never contains a URL."""


def _fsync_dir(directory: Path) -> None:
    """Best effort, POSIX only: make renames and removals in `directory` durable."""
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


def _validate(values: Mapping[str, str]) -> None:
    for name, url in values.items():
        if not isinstance(name, str) or not _MANAGED.match(name):
            raise ValueError("invalid secret name")
        if not isinstance(url, str) or len(url) > MAX_URL_LENGTH or not _URL.match(url):
            raise ValueError(f"{name}: the stream URL must be a single-line rtsp:// or rtsps:// URL")


class FrigateSecrets:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)

    def _managed_paths(self) -> list[Path]:
        return [
            entry
            for entry in self.directory.iterdir()
            if _MANAGED.match(entry.name) and entry.is_file() and not entry.is_symlink()
        ]

    def read(self) -> dict[str, str]:
        """The current device URLs, content stripped as Frigate does."""
        return {path.name: path.read_text(encoding="utf-8").strip() for path in self._managed_paths()}

    def remove_leftovers(self) -> int:
        """Remove temporary files of an interrupted write."""
        removed = 0
        for entry in self.directory.iterdir():
            if entry.name.startswith(_TEMP_PREFIX) and not entry.is_dir():
                entry.unlink(missing_ok=True)
                removed += 1
        return removed

    def _write_temp(self, name: str, url: str) -> Path:
        temp = self.directory / f"{_TEMP_PREFIX}{name}"
        temp.unlink(missing_ok=True)
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
        try:
            os.write(fd, url.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        return temp

    def replace_all(self, values: Mapping[str, str]) -> None:
        """Make the managed files exactly `values` ({secret name: URL})."""
        _validate(values)
        self.remove_leftovers()
        staged: list[tuple[Path, Path]] = []
        try:
            for name, url in values.items():
                staged.append((self._write_temp(name, url), self.directory / name))
        except OSError as exc:
            for temp, _ in staged:
                temp.unlink(missing_ok=True)
            (self.directory / f"{_TEMP_PREFIX}{name}").unlink(missing_ok=True)
            raise SecretWriteError(f"could not write {name}: {exc.strerror or type(exc).__name__}") from None

        try:
            for temp, path in staged:
                os.replace(temp, path)
            for path in self._managed_paths():
                if path.name not in values:
                    path.unlink(missing_ok=True)
        except OSError as exc:
            self.remove_leftovers()
            raise SecretWriteError(f"could not update the secret files: {exc.strerror or type(exc).__name__}") from None
        finally:
            _fsync_dir(self.directory)
