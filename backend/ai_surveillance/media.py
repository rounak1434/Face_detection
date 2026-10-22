"""Storage for operator-saved snapshots and clips.

Files live under MEDIA_ROOT/captures with server-generated names only, so a
client can never choose or escape the storage path.
"""

import os
import re
from pathlib import Path

_SAFE_NAME = re.compile(r"^[a-f0-9]{32}\.(jpg|mp4)$")


class MediaStore:
    def __init__(self, root: Path) -> None:
        self.captures_dir = (root / "captures").resolve()
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
        tmp.write_bytes(data)
        os.replace(tmp, path)
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
