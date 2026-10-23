"""Crop geometry and quality metadata (no model needed)."""

import base64

import numpy as np

from inference_service.adapters.base import Box, DetectedFace
from inference_service.cropping import crop_box, make_crop, quality, sharpness

from . import fixtures as fx


def face(x, y, w, h):
    return DetectedFace(Box(x, y, w, h), 0.9, [(x + 1.0, y + 1.0)] * 5)


def test_margin_is_added_on_all_sides():
    assert crop_box(face(100, 100, 50, 60), 640, 480, 0.2) == {"x": 90, "y": 88, "w": 70, "h": 84}


def test_crop_is_clipped_at_every_border():
    assert crop_box(face(0, 0, 50, 50), 640, 480, 0.2) == {"x": 0, "y": 0, "w": 60, "h": 60}
    assert crop_box(face(600, 440, 40, 40), 640, 480, 0.2) == {"x": 592, "y": 432, "w": 48, "h": 48}


def test_zero_margin_equals_box():
    assert crop_box(face(10, 20, 30, 40), 640, 480, 0.0) == {"x": 10, "y": 20, "w": 30, "h": 40}


def test_empty_crop_is_rejected():
    assert crop_box(face(700, 500, 10, 10), 640, 480, 0.0) is None
    assert make_crop(fx.solid(640, 480), face(700, 500, 10, 10), 0.0, 90) is None


def test_crop_pixels_match_the_box():
    image = np.zeros((100, 200, 3), dtype=np.uint8)
    image[20:60, 50:90] = 255
    crop = make_crop(image, face(50, 20, 40, 40), 0.0, 100)
    assert (crop.width, crop.height) == (40, 40)
    assert base64.b64decode(crop.jpeg_base64)[:3] == b"\xff\xd8\xff"


def test_quality_flags():
    image = fx.noise(640, 480)
    q = quality(image, face(0, 100, 30, 30), small_face_px=40)
    assert q["small_face"] is True and q["touches_edge"] is True and q["face_px"] == 30
    q = quality(image, face(100, 100, 120, 150), small_face_px=40)
    assert q["small_face"] is False and q["touches_edge"] is False


def test_sharpness_orders_sharp_above_blurred():
    import cv2

    sharp = fx.noise(200, 200, seed=3)
    blurred = cv2.GaussianBlur(sharp, (15, 15), 5)
    f = face(20, 20, 150, 150)
    assert sharpness(sharp, f) > sharpness(blurred, f) * 5
    assert sharpness(sharp, face(5, 5, 1, 1)) is None
