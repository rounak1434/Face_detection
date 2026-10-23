"""Download pinned model files and verify their checksums.

    python -m inference_service.download [--models-dir DIR]

Run once at image build time (or locally for development). After that the
service works offline. A file that fails verification is deleted.
"""

import argparse
import hashlib
import sys
import urllib.request
from pathlib import Path

from .config import get_settings
from .registry import MODELS, READY, ModelSpec, verify_file


def download(spec: ModelSpec, models_dir: Path) -> None:
    models_dir.mkdir(parents=True, exist_ok=True)
    status, _ = verify_file(spec, models_dir)
    if status == READY:
        print(f"{spec.name}: already present and verified")
        return
    target = models_dir / spec.file_name
    partial = target.with_suffix(target.suffix + ".part")
    digest = hashlib.sha256()
    size = 0
    request = urllib.request.Request(spec.url, headers={"User-Agent": "ai-surveillance-model-download"})
    with urllib.request.urlopen(request, timeout=60) as resp, partial.open("wb") as fh:
        while chunk := resp.read(1 << 16):
            size += len(chunk)
            if size > spec.size_bytes:
                break
            digest.update(chunk)
            fh.write(chunk)
    if size != spec.size_bytes or digest.hexdigest() != spec.sha256:
        partial.unlink(missing_ok=True)
        raise SystemExit(f"{spec.name}: downloaded file does not match the pinned checksum; nothing installed")
    partial.replace(target)
    print(f"{spec.name}: downloaded and verified ({size} bytes, sha256 {spec.sha256[:12]}…)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="inference_service.download")
    parser.add_argument("--models-dir", type=Path, default=None)
    args = parser.parse_args(argv)
    models_dir = args.models_dir or get_settings().models_dir
    for spec in MODELS.values():
        download(spec, models_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
