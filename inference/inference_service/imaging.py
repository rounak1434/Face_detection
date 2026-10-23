"""Safe image decoding for uploads.

Order of checks: byte size, format sniffed from the file's magic bytes (the
client's Content-Type is not trusted), dimensions from the header (before
the pixel data is decoded), then a full decode. EXIF orientation is applied
so phone photos are analysed the right way up.
"""

import io
import warnings

import numpy as np
from PIL import Image, ImageOps, UnidentifiedImageError

SUPPORTED = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}


class ImageError(Exception):
    """Base class; `code` is the API error code."""

    code = "invalid_image"
    status = 400


class TooLarge(ImageError):
    code = "too_large"
    status = 413


class UnsupportedFormat(ImageError):
    code = "unsupported_format"
    status = 415


class InvalidImage(ImageError):
    code = "invalid_image"
    status = 400


def sniff_format(data: bytes) -> str | None:
    if data[:3] == b"\xff\xd8\xff":
        return "JPEG"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "PNG"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "WEBP"
    return None


def decode(data: bytes, max_bytes: int, max_pixels: int) -> tuple[np.ndarray, str]:
    """Return (BGR uint8 array, MIME type) or raise an ImageError."""
    if not data:
        raise InvalidImage("Empty upload")
    if len(data) > max_bytes:
        raise TooLarge(f"Image is larger than {max_bytes // (1024 * 1024)} MB")
    fmt = sniff_format(data)
    if fmt is None:
        raise UnsupportedFormat("Only JPEG, PNG and WebP images are supported")

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            img = Image.open(io.BytesIO(data))
            if img.format not in SUPPORTED:
                raise UnsupportedFormat("Only JPEG, PNG and WebP images are supported")
            width, height = img.size  # header only; pixels not decoded yet
            if width * height > max_pixels:
                raise TooLarge(f"Image has {width}x{height} pixels; the limit is {max_pixels} pixels")
            if getattr(img, "n_frames", 1) > 1:
                img.seek(0)  # animated WebP/PNG: analyse the first frame only
            img = ImageOps.exif_transpose(img)
            img = img.convert("RGB")
    except ImageError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        # Pillow's own guard can fire inside Image.open, before our check.
        raise TooLarge(f"Image dimensions exceed the limit of {max_pixels} pixels") from exc
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as exc:
        raise InvalidImage("The file could not be decoded as an image") from exc

    rgb = np.asarray(img, dtype=np.uint8)
    return rgb[:, :, ::-1].copy(), SUPPORTED[fmt]
