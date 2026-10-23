"""Synthetic test images, generated in code. The only photograph in the
repository is the public-domain portrait documented in
tests/data/public_domain/README.md."""

import io
import struct
import zlib

import numpy as np
from PIL import Image


def encode(array: np.ndarray, fmt: str = "JPEG", **kwargs) -> bytes:
    buf = io.BytesIO()
    mode = "RGBA" if array.ndim == 3 and array.shape[2] == 4 else "RGB"
    Image.fromarray(array.astype(np.uint8), mode).save(buf, format=fmt, **kwargs)
    return buf.getvalue()


def solid(width=320, height=240, value=128) -> np.ndarray:
    return np.full((height, width, 3), value, dtype=np.uint8)


def noise(width=320, height=240, seed=0) -> np.ndarray:
    return np.random.default_rng(seed).integers(0, 256, (height, width, 3), dtype=np.uint8)


def shapes(width=640, height=480) -> np.ndarray:
    """Geometric shapes (no faces)."""
    img = solid(width, height, 230)
    img[50:200, 60:300] = (200, 40, 40)
    img[250:420, 350:600] = (40, 160, 40)
    yy, xx = np.ogrid[:height, :width]
    img[(yy - 300) ** 2 + (xx - 160) ** 2 < 70 ** 2] = (40, 40, 200)
    return img


def jpeg_with_orientation(array: np.ndarray, orientation: int) -> bytes:
    img = Image.fromarray(array)
    exif = Image.Exif()
    exif[0x0112] = orientation
    buf = io.BytesIO()
    img.save(buf, format="JPEG", exif=exif.tobytes())
    return buf.getvalue()


def png_header_only(width: int, height: int) -> bytes:
    """A PNG that claims huge dimensions but has (almost) no pixel data."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(b"\x00")) + chunk(b"IEND", b"")


GIF = b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;"
TIFF = b"II*\x00\x08\x00\x00\x00\x00\x00"
