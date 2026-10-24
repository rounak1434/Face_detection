"""Model adapter interfaces.

The HTTP layer and the rest of the platform depend only on these types, so
a model can be replaced by writing a new adapter.
"""

from dataclasses import dataclass, field
from typing import Protocol

import numpy as np


@dataclass
class Box:
    x: int
    y: int
    w: int
    h: int


@dataclass
class DetectedFace:
    box: Box
    score: float
    # Five points in image coordinates, in OpenCV's FaceDetectorYN order:
    # right eye, left eye, nose tip, right mouth corner, left mouth corner.
    # Raw model output: for a face cut off by the border a point may lie
    # slightly outside the image. Boxes are clipped; keypoints are not.
    keypoints5: list[tuple[float, float]] = field(default_factory=list)


class FaceDetector(Protocol):
    name: str
    version: str

    def detect(self, image_bgr: np.ndarray, score_threshold: float, max_faces: int) -> list[DetectedFace]:
        """Faces in `image_bgr`, highest score first, in original image coordinates."""
        ...
