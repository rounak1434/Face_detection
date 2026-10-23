"""Tests with the real YuNet model. Skipped unless the verified model file
is present (python -m inference_service.download --models-dir models).

A positive check on a real face needs a photo you are allowed to use; set
FACE_FIXTURE=/path/to/photo.jpg (one clearly visible face). No photos of
people are stored in the repository.
"""

import os
from pathlib import Path

import numpy as np
import pytest

from inference_service.imaging import decode
from inference_service.registry import READY, YUNET, verify_file

from . import fixtures as fx

MODELS_DIR = Path(os.environ.get("INFERENCE_MODELS_DIR", Path(__file__).resolve().parents[1] / "models"))

pytestmark = [
    pytest.mark.model,
    pytest.mark.skipif(verify_file(YUNET, MODELS_DIR)[0] != READY, reason="verified YuNet model not present"),
]


@pytest.fixture(scope="module")
def detector():
    from inference_service.adapters.yunet import YuNetDetector

    return YuNetDetector(MODELS_DIR / YUNET.file_name, max_side=1280)


@pytest.mark.parametrize("image", [fx.solid(), fx.noise(), fx.shapes(), fx.solid(1920, 1080, 0)],
                         ids=["grey", "noise", "shapes", "black-1080p"])
def test_no_faces_in_synthetic_images(detector, image):
    assert detector.detect(image, 0.7, 50) == []


def test_handles_tiny_and_extreme_aspect_images(detector):
    # Regression: unpadded short inputs gave intermittent garbage faces
    # (about 5% of calls on 20x2000), so repeat and interleave other sizes.
    other = fx.noise(640, 360, seed=1)
    for _ in range(40):
        for shape in [(1, 1, 3), (16, 16, 3), (20, 2000, 3), (2000, 20, 3), (31, 31, 3)]:
            detector.detect(other, 0.7, 50)
            assert detector.detect(np.full(shape, 100, dtype=np.uint8), 0.7, 50) == []


@pytest.mark.skipif(not os.environ.get("FACE_FIXTURE"), reason="FACE_FIXTURE not set")
def test_detects_a_real_face():
    from inference_service.adapters.yunet import YuNetDetector

    det = YuNetDetector(MODELS_DIR / YUNET.file_name)
    image, _ = decode(Path(os.environ["FACE_FIXTURE"]).read_bytes(), 20 * 1024 * 1024, 40_000_000)
    faces = det.detect(image, 0.7, 50)
    assert len(faces) >= 1
    face = faces[0]
    h, w = image.shape[:2]
    assert 0 <= face.box.x < w and 0 <= face.box.y < h
    assert face.box.x + face.box.w <= w and face.box.y + face.box.h <= h
    assert len(face.keypoints5) == 5
    # Keypoints fall inside (or very near) the face box.
    for x, y in face.keypoints5:
        assert face.box.x - face.box.w * 0.2 <= x <= face.box.x + face.box.w * 1.2
        assert face.box.y - face.box.h * 0.2 <= y <= face.box.y + face.box.h * 1.2

    # Scaled-down detection maps back to the same place in a 3x upscaled copy.
    big = np.repeat(np.repeat(image, 3, axis=0), 3, axis=1)
    big_faces = det.detect(big, 0.7, 50)
    assert big_faces, "face lost after upscaling"
    assert abs(big_faces[0].box.x - face.box.x * 3) <= face.box.w * 3 * 0.25
