"""SFace face embedder (OpenCV Zoo, Apache-2.0), run through OpenCV's FaceRecognizerSF.

The face is aligned to 112x112 from YuNet's five keypoints (the order SFace
expects), then turned into a 128-value feature. Features are L2-normalised
here, so callers compare them with a plain dot product (cosine similarity).
"""

import threading
from pathlib import Path

import cv2
import numpy as np

from ..registry import SFACE
from .base import DetectedFace


class SFaceEmbedder:
    name = SFACE.name
    version = SFACE.version
    dimensions = 128

    def __init__(self, model_path: Path) -> None:
        self._recognizer = cv2.FaceRecognizerSF.create(str(model_path), "")
        self._lock = threading.Lock()  # one OpenCV net instance; calls are serialised

    def embed(self, image_bgr: np.ndarray, face: DetectedFace) -> np.ndarray:
        b = face.box
        row = np.array([[b.x, b.y, b.w, b.h, *(v for p in face.keypoints5 for v in p), face.score]], dtype=np.float32)
        with self._lock:
            aligned = self._recognizer.alignCrop(image_bgr, row)
            feature = self._recognizer.feature(aligned)
        vector = np.asarray(feature, dtype=np.float32).reshape(-1)
        norm = float(np.linalg.norm(vector))
        if vector.shape[0] != self.dimensions or not np.isfinite(norm) or norm == 0.0:
            raise ValueError("SFace returned an unusable feature")
        return vector / norm
