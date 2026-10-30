"""The benchmark command runs on the real models and reports sane numbers (2 runs, to stay fast)."""

import json
import os
import sys
from pathlib import Path

import pytest

from inference_service import benchmark
from inference_service.registry import READY, SFACE, YUNET, verify_file

MODELS_DIR = Path(os.environ.get("INFERENCE_MODELS_DIR", Path(__file__).resolve().parents[1] / "models"))

pytestmark = [
    pytest.mark.model,
    pytest.mark.skipif(any(verify_file(s, MODELS_DIR)[0] != READY for s in (YUNET, SFACE)),
                       reason="verified YuNet and SFace models not present"),
]


def test_benchmark_reports_latency_and_memory(capsys):
    assert benchmark.main(["--models-dir", str(MODELS_DIR), "--runs", "2"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert len(result["images"]) == 4 and all(i["faces"] == 1 for i in result["images"])
    assert result["detect"]["runs"] == 8 and result["embed_per_face"]["runs"] == 8
    assert 0 < result["detect"]["median_ms"] < 5000 and 0 < result["embed_per_face"]["median_ms"] < 5000
    assert "output 128 values" in result["models"]["sface"]
    mem = result["memory_mb"]
    if sys.platform == "win32" or sys.platform.startswith("linux"):
        assert mem["after_models"]["rss"] > mem["before_models"]["rss"] > 0
        assert mem["end"]["peak"] >= mem["end"]["rss"]


def test_missing_model_stops_with_a_message(tmp_path):
    with pytest.raises(SystemExit, match="run python -m inference_service.download"):
        benchmark.main(["--models-dir", str(tmp_path), "--runs", "1"])


def test_memory_reader_never_fails():
    mem = benchmark.memory_mb()
    assert set(mem) == {"rss", "peak"}
