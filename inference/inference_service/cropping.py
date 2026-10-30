"""Face crops and simple quality metadata.

The quality values are heuristics for display and later filtering. They
are not a face-quality model and must not be read as one.
"""

import base64
from dataclasses import dataclass

import cv2
import numpy as np

from .adapters.base import DetectedFace

_SHARPNESS_HEIGHT = 112  # crops are resized to this height before measuring sharpness


@dataclass
class Crop:
    box: dict  # x, y, w, h in original-image pixels, clipped to the image
    jpeg_base64: str
    width: int
    height: int


def crop_box(face: DetectedFace, image_w: int, image_h: int, margin: float) -> dict | None:
    """Box expanded by `margin` of the face size on each side, clipped; None if empty."""
    b = face.box
    mx, my = round(b.w * margin), round(b.h * margin)
    x0, y0 = max(0, b.x - mx), max(0, b.y - my)
    x1, y1 = min(image_w, b.x + b.w + mx), min(image_h, b.y + b.h + my)
    if x1 <= x0 or y1 <= y0:
        return None
    return {"x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0}


def make_crop(image: np.ndarray, face: DetectedFace, margin: float, quality: int) -> Crop | None:
    height, width = image.shape[:2]
    box = crop_box(face, width, height, margin)
    if box is None:
        return None
    pixels = image[box["y"]:box["y"] + box["h"], box["x"]:box["x"] + box["w"]]
    if pixels.size == 0:
        return None
    ok, jpeg = cv2.imencode(".jpg", pixels, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        return None
    return Crop(box, base64.b64encode(jpeg.tobytes()).decode(), box["w"], box["h"])


def sharpness(image: np.ndarray, face: DetectedFace) -> float | None:
    """Variance of the Laplacian over the face box at a fixed scale (higher = sharper)."""
    b = face.box
    region = image[b.y:b.y + b.h, b.x:b.x + b.w]
    if region.size == 0 or b.h < 2 or b.w < 2:
        return None
    gray = cv2.cvtColor(region, cv2.COLOR_BGR2GRAY)
    scale = _SHARPNESS_HEIGHT / gray.shape[0]
    gray = cv2.resize(gray, (max(1, round(gray.shape[1] * scale)), _SHARPNESS_HEIGHT), interpolation=cv2.INTER_AREA)
    return round(float(cv2.Laplacian(gray, cv2.CV_64F).var()), 1)


def quality(image: np.ndarray, face: DetectedFace, small_face_px: int) -> dict:
    height, width = image.shape[:2]
    b = face.box
    return {
        "face_px": min(b.w, b.h),
        "small_face": min(b.w, b.h) < small_face_px,
        # The model's box reached the image border: part of the face may be cut off.
        "touches_edge": b.x <= 0 or b.y <= 0 or b.x + b.w >= width or b.y + b.h >= height,
        "sharpness": sharpness(image, face),
    }
