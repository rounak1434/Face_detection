"""Measure the face models on this machine: load time, latency and memory.

    python -m inference_service.benchmark [--models-dir DIR] [--runs N] [image ...]

Without images it uses the public-domain portraits in tests/data/public_domain (a source
checkout). Prints one JSON object. It measures speed and memory only; it says nothing about
recognition accuracy. Runs on the CPU (the pip OpenCV build has no CUDA).
"""

import argparse
import json
import os
import platform
import statistics
import sys
import time
from pathlib import Path

import cv2
import numpy as np

from .config import get_settings
from .imaging import decode
from .registry import READY, SFACE, YUNET, verify_file

DEFAULT_IMAGES = Path(__file__).resolve().parents[1] / "tests" / "data" / "public_domain"


def memory_mb() -> dict[str, float | None]:
    """Resident and peak memory of this process, in MB (None where the OS does not say)."""
    if sys.platform.startswith("linux"):
        values = {}
        for line in Path("/proc/self/status").read_text().splitlines():
            key, _, rest = line.partition(":")
            if key in ("VmRSS", "VmHWM"):
                values[key] = int(rest.split()[0]) / 1024  # kB
        return {"rss": values.get("VmRSS"), "peak": values.get("VmHWM")}
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                        ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]

        kernel32 = ctypes.WinDLL("kernel32")
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE  # a 64-bit pseudo-handle; the default int truncates it
        query = kernel32.K32GetProcessMemoryInfo
        query.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        query.restype = wintypes.BOOL
        counters = Counters()
        counters.cb = ctypes.sizeof(Counters)
        if query(kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
            return {"rss": counters.WorkingSetSize / 2**20, "peak": counters.PeakWorkingSetSize / 2**20}
    return {"rss": None, "peak": None}


def _stats(samples_ms: list[float]) -> dict[str, float]:
    ordered = sorted(samples_ms)
    p95 = ordered[min(len(ordered) - 1, round(0.95 * (len(ordered) - 1)))]
    return {"median_ms": round(statistics.median(ordered), 2), "p95_ms": round(p95, 2),
            "min_ms": round(ordered[0], 2), "runs": len(ordered)}


def run(models_dir: Path, images: list[Path], runs: int) -> dict:
    for spec in (YUNET, SFACE):
        status, detail = verify_file(spec, models_dir)
        if status != READY:
            raise SystemExit(f"{spec.name}: {status} ({detail}); run python -m inference_service.download first")
    from .adapters.sface import SFaceEmbedder
    from .adapters.yunet import YuNetDetector

    before = memory_mb()
    started = time.perf_counter()
    detector = YuNetDetector(models_dir / YUNET.file_name)
    yunet_load = (time.perf_counter() - started) * 1000
    started = time.perf_counter()
    embedder = SFaceEmbedder(models_dir / SFACE.file_name)
    sface_load = (time.perf_counter() - started) * 1000
    loaded = memory_mb()

    decoded = [(p.name, decode(p.read_bytes(), 10 * 1024 * 1024, 40_000_000)[0]) for p in images]
    per_image = []
    detect_all, embed_all = [], []
    for name, image in decoded:
        faces = detector.detect(image, 0.7, 10)
        for _ in range(3):  # warm-up: first calls allocate buffers
            detector.detect(image, 0.7, 10)
            for face in faces:
                embedder.embed(image, face)
        detect_ms, embed_ms = [], []
        for _ in range(runs):
            t = time.perf_counter()
            detector.detect(image, 0.7, 10)
            detect_ms.append((time.perf_counter() - t) * 1000)
            for face in faces:
                t = time.perf_counter()
                embedder.embed(image, face)
                embed_ms.append((time.perf_counter() - t) * 1000)
        assert all(abs(float(np.linalg.norm(embedder.embed(image, f))) - 1) < 1e-4 for f in faces)
        detect_all += detect_ms
        embed_all += embed_ms
        per_image.append({"image": name, "size": f"{image.shape[1]}x{image.shape[0]}", "faces": len(faces),
                          "detect": _stats(detect_ms), "embed_per_face": _stats(embed_ms) if embed_ms else None})
    return {
        "machine": {"os": platform.platform(), "python": platform.python_version(), "cpu": platform.processor(),
                    "logical_cpus": os.cpu_count()},
        "runtime": {"opencv": cv2.__version__, "opencv_threads": cv2.getNumThreads(),
                    "cuda_devices": cv2.cuda.getCudaEnabledDeviceCount() if hasattr(cv2, "cuda") else 0,
                    "backend": "OpenCV DNN, CPU"},
        "models": {"yunet": f"{YUNET.version}, load {yunet_load:.0f} ms",
                   "sface": f"{SFACE.version}, load {sface_load:.0f} ms, output {embedder.dimensions} values"},
        "memory_mb": {"before_models": before, "after_models": loaded, "end": memory_mb()},
        "detect": _stats(detect_all),
        "embed_per_face": _stats(embed_all) if embed_all else None,
        "images": per_image,
        "note": "Speed and memory only; not an accuracy measurement.",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="inference_service.benchmark")
    parser.add_argument("--models-dir", type=Path, default=None)
    parser.add_argument("--runs", type=int, default=30)
    parser.add_argument("images", nargs="*", type=Path)
    args = parser.parse_args(argv)
    images = args.images or sorted(DEFAULT_IMAGES.glob("*.jpg"))
    if not images:
        raise SystemExit("no images given and no test portraits found")
    result = run(args.models_dir or get_settings().models_dir, images, max(1, args.runs))
    print(json.dumps(result, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
