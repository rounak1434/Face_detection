"""Storage for operator-saved snapshots and clips.

Files live under MEDIA_ROOT/captures with server-generated names only, so a
client can never choose or escape the storage path.
"""

import os
import re
import shutil
from pathlib import Path

_SAFE_NAME = re.compile(r"^[a-f0-9]{32}\.(jpg|mp4)$")


class MediaStore:
    def __init__(self, root: Path, subdir: str = "captures") -> None:
        # "captures" for operator snapshots and clips, "faces" for face-event frames and crops.
        self.captures_dir = (root / subdir).resolve()
        self.captures_dir.mkdir(parents=True, exist_ok=True)

    def path_for(self, file_name: str) -> Path:
        """Resolve a stored file name, rejecting anything that is not ours."""
        if not _SAFE_NAME.match(file_name):
            raise ValueError("Invalid capture file name")
        path = (self.captures_dir / file_name).resolve()
        if path.parent != self.captures_dir:
            raise ValueError("Capture path escapes the media directory")
        return path

    def write_atomic(self, file_name: str, data: bytes) -> int:
        path = self.path_for(file_name)
        tmp = path.with_suffix(path.suffix + ".part")
        try:
            tmp.write_bytes(data)
            os.replace(tmp, path)
        except BaseException:
            tmp.unlink(missing_ok=True)  # never leave a partial file behind (e.g. disk full)
            raise
        return len(data)

    def partial_path(self, file_name: str) -> Path:
        path = self.path_for(file_name)
        return path.with_suffix(path.suffix + ".part")

    def finalize(self, file_name: str) -> int:
        path = self.path_for(file_name)
        os.replace(self.partial_path(file_name), path)
        return path.stat().st_size

    def delete(self, file_name: str) -> None:
        for path in (self.path_for(file_name), self.partial_path(file_name)):
            path.unlink(missing_ok=True)

    def free_bytes(self) -> int:
        """Free space on the filesystem holding the captures (and, in Docker, the database)."""
        return shutil.disk_usage(self.captures_dir).free

    def remove_partials(self) -> int:
        """Remove partial files of captures interrupted by a restart. Call only at startup."""
        removed = 0
        for path in self.captures_dir.glob("*.part"):
            if _SAFE_NAME.match(path.name.removesuffix(".part")):
                path.unlink(missing_ok=True)
                removed += 1
        return removed


def describe_os_error(action: str, exc: OSError) -> str:
    """A message for API responses: the reason, never the storage path."""
    return f"Could not save the {action}: {exc.strerror or type(exc).__name__}"
