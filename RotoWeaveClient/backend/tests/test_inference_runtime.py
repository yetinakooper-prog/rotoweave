from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.app import inference_runtime
from backend.app.inference_runtime import (
    CPU_PROVIDER,
    CUDA_PROVIDER,
    ModelRuntime,
    ModelSpec,
    verify_model_manifest,
)


def _model_with_manifest(tmp_path: Path) -> Path:
    path = tmp_path / "model.onnx"
    path.write_bytes(b"immutable-onnx")
    manifest = {
        "modelId": "test/model",
        "revision": "fixed-revision",
        "sourceFile": "weights.pth",
        "sourceSha256": "b" * 64,
        "onnxFile": path.name,
        "onnxSha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "license": "MIT",
    }
    path.with_suffix(".manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    return path


class _SessionOptions:
    def __init__(self) -> None:
        self.enable_mem_pattern = False
        self.execution_mode = None
        self.log_severity_level = 0


def _fake_ort(
    available: list[str],
    captured: dict[str, object],
    *,
    fail_cuda: bool = False,
    version: str = "1.26.0",
) -> object:
    class Session:
        def __init__(self, path: str, *, sess_options: object, providers: list[object]):
            captured.setdefault("calls", []).append(providers)
            captured["path"] = path
            captured["providers"] = providers
            captured["options"] = sess_options
            first = providers[0]
            if fail_cuda and isinstance(first, tuple) and first[0] == CUDA_PROVIDER:
                raise RuntimeError("driver too old")
            self._providers = [first[0] if isinstance(first, tuple) else first]
            if CPU_PROVIDER not in self._providers:
                self._providers.append(CPU_PROVIDER)

        def get_providers(self) -> list[str]:
            return list(self._providers)

    return SimpleNamespace(
        __version__=version,
        get_available_providers=lambda: list(available),
        SessionOptions=_SessionOptions,
        ExecutionMode=SimpleNamespace(ORT_SEQUENTIAL="sequential"),
        InferenceSession=Session,
        preload_dlls=lambda **_kwargs: None,
    )


def test_runtime_rejects_onnxruntime_version_outside_model_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    verify_model_manifest.cache_clear()
    model = _model_with_manifest(tmp_path)
    manifest_path = model.with_suffix(".manifest.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["onnxRuntime"] = "1.26.0"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    captured: dict[str, object] = {}
    monkeypatch.setitem(
        sys.modules,
        "onnxruntime",
        _fake_ort([CPU_PROVIDER], captured, version="1.24.4"),
    )

    runtime = ModelRuntime(ModelSpec("test", model), tmp_path, 0)
    with pytest.raises(RuntimeError, match="需要 1.26.0，当前 1.24.4"):
        runtime.ensure_ready()
    assert "calls" not in captured


def test_model_manifest_rejects_changed_onnx(tmp_path: Path) -> None:
    verify_model_manifest.cache_clear()
    model = _model_with_manifest(tmp_path)
    assert verify_model_manifest(str(model))["modelId"] == "test/model"
    verify_model_manifest.cache_clear()
    model.write_bytes(b"tampered")
    with pytest.raises(RuntimeError, match="SHA-256"):
        verify_model_manifest(str(model))


def test_runtime_ignores_directml_and_reports_cpu_degraded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    verify_model_manifest.cache_clear()
    model = _model_with_manifest(tmp_path)
    captured: dict[str, object] = {}
    monkeypatch.setitem(
        sys.modules,
        "onnxruntime",
        _fake_ort(["DmlExecutionProvider", CPU_PROVIDER], captured),
    )
    runtime = ModelRuntime(ModelSpec("test", model), tmp_path, 0).ensure_ready()
    assert captured["providers"] == [CPU_PROVIDER]
    health = runtime.snapshot()
    assert health["state"] == "degraded"
    assert health["provider"] == CPU_PROVIDER
    assert "CUDAExecutionProvider" in health["fallbackReason"]


def test_runtime_uses_cuda_then_cpu_in_fixed_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    verify_model_manifest.cache_clear()
    model = _model_with_manifest(tmp_path)
    captured: dict[str, object] = {}
    monkeypatch.setitem(
        sys.modules,
        "onnxruntime",
        _fake_ort([CUDA_PROVIDER, CPU_PROVIDER], captured),
    )
    monkeypatch.setattr(
        inference_runtime,
        "_windows_cuda_driver_recommendation",
        lambda _device_id: (True, "999.0"),
    )
    runtime = ModelRuntime(ModelSpec("test", model), tmp_path, 0).ensure_ready()
    providers = captured["providers"]
    assert isinstance(providers, list)
    assert providers[0][0] == CUDA_PROVIDER
    assert providers[0][1]["cudnn_conv_algo_search"] == "HEURISTIC"
    assert providers[0][1]["cudnn_conv_use_max_workspace"] == "0"
    assert providers[1] == CPU_PROVIDER
    assert runtime.snapshot()["state"] == "ready"


def test_runtime_falls_back_to_cpu_when_listed_cuda_cannot_create_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    verify_model_manifest.cache_clear()
    model = _model_with_manifest(tmp_path)
    captured: dict[str, object] = {}
    monkeypatch.setitem(
        sys.modules,
        "onnxruntime",
        _fake_ort(
            [CUDA_PROVIDER, CPU_PROVIDER],
            captured,
            fail_cuda=True,
        ),
    )
    monkeypatch.setattr(
        inference_runtime,
        "_windows_cuda_driver_recommendation",
        lambda _device_id: (True, "999.0"),
    )
    runtime = ModelRuntime(ModelSpec("test", model), tmp_path, 0).ensure_ready()
    health = runtime.snapshot()
    assert health["state"] == "degraded"
    assert health["provider"] == CPU_PROVIDER
    assert "driver too old" in health["fallbackReason"]
    calls = captured["calls"]
    assert calls[0][0][0] == CUDA_PROVIDER
    assert calls[1] == [CPU_PROVIDER]


def test_runtime_uses_verified_cuda_when_driver_is_below_recommendation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    verify_model_manifest.cache_clear()
    model = _model_with_manifest(tmp_path)
    captured: dict[str, object] = {}
    monkeypatch.setitem(
        sys.modules,
        "onnxruntime",
        _fake_ort([CUDA_PROVIDER, CPU_PROVIDER], captured),
    )
    monkeypatch.setattr(
        inference_runtime,
        "_windows_cuda_driver_recommendation",
        lambda _device_id: (False, "536.99"),
    )
    runtime = ModelRuntime(ModelSpec("test", model), tmp_path, 0).ensure_ready()
    health = runtime.snapshot()
    providers = captured["providers"]
    assert isinstance(providers, list)
    assert providers[0][0] == CUDA_PROVIDER
    assert health["state"] == "ready"
    assert health["provider"] == CUDA_PROVIDER
    assert health["driverVersion"] == "536.99"
    assert health["minimumDriverVersion"] is None
    assert health["recommendedDriverVersion"] == "570.65"
    assert health["driverRecommendationMet"] is False
    assert health["fallbackReason"] is None
