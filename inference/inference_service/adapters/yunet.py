"""YuNet face detector (OpenCV Zoo, MIT), run through OpenCV's FaceDetectorYN.

The 2023mar model has a fixed input shape that OpenCV resets per image via
setInputSize. Large images are scaled down to `max_side` for detection and
the results are mapped back to original coordinates.
"""

import threading
from pathlib import Path

import cv2
import numpy as np

from ..registry import YUNET
from .base import Box, DetectedFace

# Internal pre-filter. The caller's threshold is applied afterwards, so it
# can be stricter but never looser than this.
_MIN_SCORE = 0.3
_NMS_THRESHOLD = 0.3
_TOP_K = 5000
_MIN_INPUT = 64


def _plausible(points: list[tuple[float, float]], x: float, y: float, w: float, h: float) -> bool:
    """Reject detections whose keypoints lie far outside their own box."""
    return all(x - 0.5 * w <= px <= x + 1.5 * w and y - 0.5 * h <= py <= y + 1.5 * h for px, py in points)


class YuNetDetector:
    name = YUNET.name
    version = YUNET.version

    def __init__(self, model_path: Path, max_side: int = 1280) -> None:
        self._max_side = max_side
        self._detector = cv2.FaceDetectorYN.create(
            str(model_path), "", (320, 320), _MIN_SCORE, _NMS_THRESHOLD, _TOP_K
        )
        # FaceDetectorYN keeps per-instance state (input size), so calls are serialised.
        self._lock = threading.Lock()

    def detect(self, image_bgr: np.ndarray, score_threshold: float, max_faces: int) -> list[DetectedFace]:
        height, width = image_bgr.shape[:2]
        scale = min(1.0, self._max_side / max(height, width))
        work = image_bgr if scale == 1.0 else cv2.resize(
            image_bgr, (max(1, round(width * scale)), max(1, round(height * scale))), interpolation=cv2.INTER_AREA
        )
        # YuNet's feature maps go down to stride 32. Inputs shorter than that
        # produced intermittent garbage detections (score 1.0, keypoints off
        # the image) in testing, so pad bottom/right to a safe multiple of 32.
        # Padding at the bottom/right leaves coordinates unchanged.
        pad_h = max(_MIN_INPUT, -(-work.shape[0] // 32) * 32) - work.shape[0]
        pad_w = max(_MIN_INPUT, -(-work.shape[1] // 32) * 32) - work.shape[1]
        if pad_h or pad_w:
            work = cv2.copyMakeBorder(work, 0, pad_h, 0, pad_w, cv2.BORDER_CONSTANT, value=(0, 0, 0))
        with self._lock:
            self._detector.setInputSize((work.shape[1], work.shape[0]))
            _, rows = self._detector.detect(work)
        if rows is None:
            return []

        faces = []
        for row in rows:
            score = float(row[14])
            if score < score_threshold:
                continue
            x, y, w, h = (float(v) / scale for v in row[:4])
            # Clip to the image; YuNet can report boxes slightly outside it.
            x0, y0 = max(0.0, x), max(0.0, y)
            x1, y1 = min(float(width), x + w), min(float(height), y + h)
            if x1 <= x0 or y1 <= y0:
                continue
            points = [(round(float(row[4 + 2 * i]) / scale, 1), round(float(row[5 + 2 * i]) / scale, 1))
                      for i in range(5)]
            if not _plausible(points, x, y, w, h):
                continue
            faces.append(DetectedFace(
                box=Box(round(x0), round(y0), round(x1 - x0), round(y1 - y0)),
                score=round(score, 4),
                keypoints5=points,
            ))
        faces.sort(key=lambda f: f.score, reverse=True)
        return faces[:max_faces]
