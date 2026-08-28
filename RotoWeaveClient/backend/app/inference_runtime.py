from __future__ import annotations

from contracts.legacy_compat import compatible_environment_value

import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np


CUDA_PROVIDER = "CUDAExecutionProvider"
CPU_PROVIDER = "CPUExecutionProvider"
RECOMMENDED_WINDOWS_CUDA_DRIVER = (570, 65)


# Windows cuDNN 9 may load NVRTC lazily while selecting convolution kernels.
# Keep both DLL-directory cookies and explicitly loaded libraries alive for the
# lifetime of the process; otherwise Python closes the search registration as
# soon as the temporary handle is garbage-collected.
_dll_load_lock = threading.RLock()
_dll_directory_handles: dict[str, Any] = {}
_explicit_dll_handles: dict[str, Any] = {}


@dataclass(frozen=True, slots=True)
class ModelSpec:
    model_id: str
    path: Path
    precision: str = "fp32"


def _verify_onnxruntime_contract(manifest: dict[str, Any], runtime: Any) -> str:
    """Fail closed when a signed model was validated for another ORT build."""

    actual = str(getattr(runtime, "__version__", "") or "")
    expected = str(manifest.get("onnxRuntime") or "")
    if expected and actual != expected:
        raise RuntimeError(
            "ONNX Runtime 版本与模型清单不匹配："
            f"需要 {expected}，当前 {actual or 'unknown'}。"
        )
    return actual


@lru_cache(maxsize=8)
def _verify_model_manifest_cached(
    model_path_value: str,
    model_size: int,
    model_mtime_ns: int,
    manifest_size: int,
    manifest_mtime_ns: int,
) -> dict[str, Any]:
    """Validate the immutable model file against its adjacent manifest."""

    model_path = Path(model_path_value)
    manifest_path = model_path.with_suffix(".manifest.json")
    if not model_path.is_file():
        raise RuntimeError(f"内置模型不存在：{model_path.name}")
    if not manifest_path.is_file():
        raise RuntimeError(f"内置模型缺少校验清单：{model_path.name}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"内置模型校验清单损坏：{model_path.name}") from exc
    expected_name = str(manifest.get("onnxFile") or "")
    expected_hash = str(manifest.get("onnxSha256") or "").lower()
    if expected_name != model_path.name or len(expected_hash) != 64:
        raise RuntimeError(f"内置模型校验清单无效：{model_path.name}")
    digest = hashlib.sha256()
    try:
        with model_path.open("rb") as handle:
            while chunk := handle.read(4 * 1024 * 1024):
                digest.update(chunk)
    except OSError as exc:
        raise RuntimeError(f"无法读取内置模型：{model_path.name}") from exc
    if digest.hexdigest() != expected_hash:
        raise RuntimeError(f"内置模型 SHA-256 校验失败：{model_path.name}")
    return manifest


def verify_model_manifest(model_path_value: str) -> dict[str, Any]:
    model_path = Path(model_path_value)
    manifest_path = model_path.with_suffix(".manifest.json")
    try:
        stat = model_path.stat()
        manifest_stat = manifest_path.stat()
    except OSError as exc:
        raise RuntimeError(f"内置模型或校验清单不存在：{model_path.name}") from exc
    return _verify_model_manifest_cached(
        str(model_path.resolve()),
        int(stat.st_size),
        int(stat.st_mtime_ns),
        int(manifest_stat.st_size),
        int(manifest_stat.st_mtime_ns),
    )


verify_model_manifest.cache_clear = _verify_model_manifest_cached.cache_clear  # type: ignore[attr-defined]


def _fixed_shape(value: list[Any] | tuple[Any, ...]) -> tuple[int, ...] | None:
    if not value or not all(isinstance(item, int) and item > 0 for item in value):
        return None
    return tuple(int(item) for item in value)


def _safe_runtime_error(exc: Exception, model_path: Path, runtime_root: Path) -> str:
    detail = str(exc)
    for raw, replacement in (
        (str(model_path.resolve()), model_path.name),
        (str(runtime_root.resolve()), "<runtime>"),
    ):
        detail = detail.replace(raw, replacement).replace(
            raw.replace("\\", "/"), replacement
        )
    return detail


@lru_cache(maxsize=2)
def _gpu_name(device_id: int) -> str | None:
    executable = shutil.which("nvidia-smi")
    if not executable:
        return None
    try:
        completed = subprocess.run(
            [
                executable,
                f"--id={device_id}",
                "--query-gpu=name",
                "--format=csv,noheader",
            ],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    name = completed.stdout.strip().splitlines()
    return name[0].strip() if completed.returncode == 0 and name else None


@lru_cache(maxsize=2)
def _nvidia_driver_version(device_id: int) -> str | None:
    executable = shutil.which("nvidia-smi")
    if not executable:
        return None
    try:
        completed = subprocess.run(
            [
                executable,
                f"--id={device_id}",
                "--query-gpu=driver_version",
                "--format=csv,noheader",
            ],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    values = completed.stdout.strip().splitlines()
    return values[0].strip() if completed.returncode == 0 and values else None


def _driver_version_tuple(value: str | None) -> tuple[int, ...] | None:
    if not value:
        return None
    try:
        return tuple(int(part) for part in value.split("."))
    except ValueError:
        return None


def _windows_cuda_driver_recommendation(
    device_id: int,
) -> tuple[bool | None, str | None]:
    version = _nvidia_driver_version(device_id)
    parsed = _driver_version_tuple(version)
    if os.name != "nt" or parsed is None:
        return None, version
    return parsed >= RECOMMENDED_WINDOWS_CUDA_DRIVER, version


def _cuda_dll_directories(runtime_root: Path, ort_module: Any) -> list[Path]:
    candidates = [
        runtime_root / "cuda",
        runtime_root / "release" / "cuda",
        runtime_root / "_internal" / "cuda",
    ]
    try:
        site_packages = Path(ort_module.__file__).resolve().parent.parent
        nvidia_root = site_packages / "nvidia"
        if nvidia_root.is_dir():
            candidates.extend(
                child / "bin"
                for child in sorted(nvidia_root.iterdir(), key=lambda item: item.name)
                if (child / "bin").is_dir()
            )
    except (OSError, TypeError, AttributeError):
        pass
    # The explicit sys.prefix fallback also covers editable/embedded Python
    # layouts where the imported module path is not below site-packages.
    nvidia_root = Path(sys.prefix) / "Lib" / "site-packages" / "nvidia"
    if nvidia_root.is_dir():
        candidates.extend(
            child / "bin"
            for child in sorted(nvidia_root.iterdir(), key=lambda item: item.name)
            if (child / "bin").is_dir()
        )
    resolved: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        try:
            value = candidate.resolve()
        except OSError:
            continue
        key = os.path.normcase(str(value))
        if value.is_dir() and key not in seen:
            seen.add(key)
            resolved.append(value)
    return resolved


def _register_windows_cuda_dlls(directories: list[Path]) -> list[str]:
    if os.name != "nt":
        return []
    import ctypes

    registered: list[str] = []
    with _dll_load_lock:
        path_entries = os.environ.get("PATH", "").split(os.pathsep)
        existing = {
            os.path.normcase(os.path.normpath(item))
            for item in path_entries
            if item
        }
        prepend: list[str] = []
        for directory in directories:
            value = str(directory)
            key = os.path.normcase(os.path.normpath(value))
            if key not in _dll_directory_handles and hasattr(os, "add_dll_directory"):
                try:
                    _dll_directory_handles[key] = os.add_dll_directory(value)
                except OSError:
                    continue
            if key not in existing:
                prepend.append(value)
                existing.add(key)
            registered.append(value)
        if prepend:
            os.environ["PATH"] = os.pathsep.join([*prepend, *path_entries])

        # ONNX Runtime's Windows preload helper does not include NVRTC or
        # nvJitLink. cuDNN opens these by filename during the first real
        # convolution, so session-only checks otherwise report false health.
        patterns = (
            "nvJitLink_*.dll",
            "nvrtc-builtins64_*.dll",
            "nvrtc64_120_0.dll",
        )
        for pattern in patterns:
            for directory in directories:
                for dll_path in sorted(directory.glob(pattern)):
                    key = os.path.normcase(str(dll_path.resolve()))
                    if key in _explicit_dll_handles:
                        continue
                    try:
                        _explicit_dll_handles[key] = ctypes.WinDLL(str(dll_path))
                    except OSError:
                        continue
                    registered.append(dll_path.name)
    return registered


def preload_cuda_runtime(runtime_root: Path) -> list[str]:
    """Load packaged CUDA/cuDNN DLLs before ONNX Runtime creates a session."""

    try:
        import onnxruntime as ort
    except ImportError as exc:  # pragma: no cover - packaged smoke gate
        raise RuntimeError("内置 ONNX Runtime 未安装。") from exc
    loaded: list[str] = []
    directories = _cuda_dll_directories(runtime_root, ort)
    loaded.extend(_register_windows_cuda_dlls(directories))
    preload = getattr(ort, "preload_dlls", None)
    if preload is None:
        return loaded
    try:
        preload(directory="")
        loaded.append("site-packages:nvidia")
    except TypeError:
        try:
            preload(cuda=True, cudnn=True, msvc=True, directory="")
            loaded.append("site-packages:nvidia")
        except Exception:
            pass
    except Exception:
        pass
    for raw_candidate in (
        runtime_root / "cuda",
        runtime_root / "release" / "cuda",
        runtime_root / "_internal" / "cuda",
    ):
        try:
            candidate = raw_candidate.resolve()
        except OSError:
            continue
        if not candidate.is_dir():
            continue
        try:
            preload(directory=str(candidate))
        except TypeError:
            preload(cuda=True, cudnn=True, msvc=True, directory=str(candidate))
        except Exception:
            continue
        loaded.append(str(candidate.resolve()))
    return loaded


class ModelRuntime:
    """Serial, cached ONNX session with CUDA-first fallback and I/O binding."""

    def __init__(self, spec: ModelSpec, runtime_root: Path, device_id: int) -> None:
        self.spec = spec
        self.runtime_root = runtime_root
        self.device_id = device_id
        self._lock = threading.RLock()
        self._state = "warming"
        self._session: Any | None = None
        self._manifest: dict[str, Any] | None = None
        self._providers: list[str] = []
        self._fallback_reason: str | None = None
        self._error: str | None = None
        self._preloaded: list[str] = []
        self._driver_version: str | None = None
        self._runtime_version: str | None = None
        try:
            configured_limit = int(
                compatible_environment_value("ROTOWEAVE_ONNX_CUDA_MEMORY_LIMIT_MIB", "5120")
            )
        except ValueError:
            configured_limit = 5120
        self._cuda_memory_limit_mib = max(1024, min(5632, configured_limit))
        self._input_buffers: dict[tuple[str, tuple[int, ...], str], Any] = {}
        self._output_buffers: dict[tuple[str, tuple[int, ...], str], Any] = {}

    def _run_self_test(self) -> None:
        assert self._session is not None
        manifest = self._manifest or {}
        specification = manifest.get("selfTest")
        if not isinstance(specification, dict):
            return
        filename = str(specification.get("file") or "")
        expected_hash = str(specification.get("sha256") or "").lower()
        if not filename or Path(filename).name != filename or len(expected_hash) != 64:
            raise RuntimeError("模型自检向量声明无效。")
        vector_path = self.spec.path.parent / filename
        if not vector_path.is_file():
            raise RuntimeError(f"模型自检向量不存在：{filename}")
        digest = hashlib.sha256(vector_path.read_bytes()).hexdigest()
        if digest != expected_hash:
            raise RuntimeError(f"模型自检向量 SHA-256 校验失败：{filename}")
        input_map = specification.get("inputs")
        if not isinstance(input_map, dict):
            raise RuntimeError(f"模型自检输入声明无效：{filename}")
        with np.load(vector_path, allow_pickle=False) as archive:
            feeds: dict[str, np.ndarray] = {}
            for item in self._session.get_inputs():
                key = str(input_map.get(item.name) or "")
                if not key or key not in archive:
                    raise RuntimeError(f"模型自检缺少输入 {item.name}：{filename}")
                feeds[item.name] = np.ascontiguousarray(archive[key])
            output_key = str(specification.get("output") or "")
            if not output_key or output_key not in archive:
                raise RuntimeError(f"模型自检缺少期望输出：{filename}")
            expected = np.asarray(archive[output_key])
            actual = np.asarray(self._session.run(None, feeds)[-1])
        transform = str(specification.get("comparisonTransform") or "")
        if transform == "sigmoid":
            expected = 1.0 / (1.0 + np.exp(-expected))
            actual = 1.0 / (1.0 + np.exp(-actual))
        elif transform:
            raise RuntimeError(f"模型自检变换不受支持：{transform}")
        absolute = float(specification.get("atol") or 0.0)
        relative = float(specification.get("rtol") or 0.0)
        if absolute <= 0 or absolute > 0.01 or relative < 0 or relative > 0.01:
            raise RuntimeError(f"模型自检容差无效：{filename}")
        if actual.shape != expected.shape or not np.allclose(
            actual, expected, atol=absolute, rtol=relative, equal_nan=False
        ):
            maximum = (
                float(np.max(np.abs(actual - expected)))
                if actual.shape == expected.shape
                else float("inf")
            )
            raise RuntimeError(f"模型数值自检失败：{filename}，maxAbs={maximum}")

    def ensure_ready(self) -> "ModelRuntime":
        with _device_execution_lock, self._lock:
            if self._session is not None:
                return self
            try:
                import onnxruntime as ort

                self._manifest = verify_model_manifest(str(self.spec.path.resolve()))
                self._runtime_version = _verify_onnxruntime_contract(
                    self._manifest, ort
                )
                self._preloaded = preload_cuda_runtime(self.runtime_root)
                available = list(ort.get_available_providers())
                options = ort.SessionOptions()
                options.enable_mem_pattern = True
                options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
                options.log_severity_level = 3
                provider_chain: list[Any]
                _, self._driver_version = _windows_cuda_driver_recommendation(
                    self.device_id
                )
                if CUDA_PROVIDER in available:
                    provider_chain = [
                        (
                            CUDA_PROVIDER,
                            {
                                "device_id": self.device_id,
                                "arena_extend_strategy": "kSameAsRequested",
                                "cudnn_conv_algo_search": "HEURISTIC",
                                "cudnn_conv_use_max_workspace": "0",
                                "do_copy_in_default_stream": "1",
                                "gpu_mem_limit": str(
                                    self._cuda_memory_limit_mib * 1024 * 1024
                                ),
                            },
                        ),
                        CPU_PROVIDER,
                    ]
                elif CPU_PROVIDER in available:
                    provider_chain = [CPU_PROVIDER]
                    self._fallback_reason = "CUDAExecutionProvider 不可用"
                else:
                    raise RuntimeError("没有可用的 CUDA 或 CPU ONNX Provider。")
                try:
                    self._session = ort.InferenceSession(
                        str(self.spec.path.resolve()),
                        sess_options=options,
                        providers=provider_chain,
                    )
                except Exception as cuda_exc:
                    if CUDA_PROVIDER not in available or CPU_PROVIDER not in available:
                        raise
                    # A listed CUDA provider can still fail because the driver
                    # or one packaged DLL is incompatible. Preserve complete
                    # offline CPU operation and expose the real fallback cause.
                    self._fallback_reason = (
                        "CUDA 会话初始化失败，已回退 CPU："
                        + _safe_runtime_error(
                            cuda_exc, self.spec.path, self.runtime_root
                        )
                    )
                    self._session = ort.InferenceSession(
                        str(self.spec.path.resolve()),
                        sess_options=options,
                        providers=[CPU_PROVIDER],
                    )
                self._providers = list(self._session.get_providers())
                if not self._providers:
                    raise RuntimeError("ONNX 会话未启用任何 Provider。")
                using_cuda = self._providers[0] == CUDA_PROVIDER
                try:
                    self._run_self_test()
                except Exception as self_test_exc:
                    if not using_cuda or CPU_PROVIDER not in available:
                        raise
                    self._fallback_reason = (
                        "CUDA 模型自检失败，已回退 CPU："
                        + _safe_runtime_error(
                            self_test_exc, self.spec.path, self.runtime_root
                        )
                    )
                    self._session = ort.InferenceSession(
                        str(self.spec.path.resolve()),
                        sess_options=options,
                        providers=[CPU_PROVIDER],
                    )
                    self._providers = list(self._session.get_providers())
                    using_cuda = False
                    self._run_self_test()
                self._state = "ready" if using_cuda else "degraded"
                if not using_cuda and self._fallback_reason is None:
                    self._fallback_reason = "CUDA 会话初始化后回退到 CPU"
            except Exception as exc:
                self._state = "error"
                self._error = _safe_runtime_error(
                    exc, self.spec.path, self.runtime_root
                )
                raise RuntimeError(self._error) from exc
        return self

    def run(self, inputs: dict[str, np.ndarray]) -> list[np.ndarray]:
        with _device_execution_lock, self._lock:
            self.ensure_ready()
            assert self._session is not None
            normalized = {
                name: np.ascontiguousarray(value)
                for name, value in inputs.items()
            }
            if not self._providers or self._providers[0] != CUDA_PROVIDER:
                return [np.asarray(item) for item in self._session.run(None, normalized)]
            try:
                import onnxruntime as ort

                binding = self._session.io_binding()
                for name, value in normalized.items():
                    key = (name, tuple(value.shape), str(value.dtype))
                    buffer = self._input_buffers.get(key)
                    if buffer is None:
                        buffer = ort.OrtValue.ortvalue_from_shape_and_type(
                            value.shape, value.dtype, "cuda", self.device_id
                        )
                        self._input_buffers[key] = buffer
                    buffer.update_inplace(value)
                    binding.bind_ortvalue_input(name, buffer)
                for output in self._session.get_outputs():
                    shape = _fixed_shape(output.shape)
                    dtype = np.float16 if "float16" in output.type else np.float32
                    if shape is None:
                        binding.bind_output(output.name, "cuda", self.device_id)
                        continue
                    key = (output.name, shape, str(np.dtype(dtype)))
                    buffer = self._output_buffers.get(key)
                    if buffer is None:
                        buffer = ort.OrtValue.ortvalue_from_shape_and_type(
                            shape, dtype, "cuda", self.device_id
                        )
                        self._output_buffers[key] = buffer
                    binding.bind_ortvalue_output(output.name, buffer)
                self._session.run_with_iobinding(binding)
                return [np.asarray(item) for item in binding.copy_outputs_to_cpu()]
            except Exception as exc:
                raise RuntimeError(f"{self.spec.model_id} CUDA I/O Binding 失败：{exc}") from exc

    def inputs(self) -> list[Any]:
        with self._lock:
            self.ensure_ready()
            assert self._session is not None
            return list(self._session.get_inputs())

    def snapshot(self) -> dict[str, Any]:
        manifest = self._manifest or {}
        provider = self._providers[0] if self._providers else None
        return {
            "modelId": self.spec.model_id,
            "state": self._state,
            "provider": provider,
            "providers": list(self._providers),
            "deviceId": self.device_id,
            "gpuName": (
                _gpu_name(self.device_id)
                if provider == CUDA_PROVIDER or self._driver_version is not None
                else None
            ),
            "driverVersion": self._driver_version,
            "minimumDriverVersion": None,
            "recommendedDriverVersion": ".".join(
                str(item) for item in RECOMMENDED_WINDOWS_CUDA_DRIVER
            ),
            "driverRecommendationMet": _windows_cuda_driver_recommendation(
                self.device_id
            )[0],
            "precision": self.spec.precision,
            "cudaMemoryLimitMiB": self._cuda_memory_limit_mib,
            "runtimeVersion": self._runtime_version,
            "model": self.spec.path.name,
            "modelRevision": manifest.get("revision"),
            "modelSha256": manifest.get("onnxSha256"),
            "fallbackReason": self._fallback_reason,
            "error": self._error,
            # Health and task snapshots are public projections. Do not expose
            # local installation paths merely to prove that preloading ran.
            "dllPreloadCount": len(self._preloaded),
        }


_registry_lock = threading.RLock()
_device_execution_lock = threading.RLock()
_registry: dict[tuple[str, str, str, int], ModelRuntime] = {}
_warmup_threads: dict[tuple[str, str, str, int], threading.Thread] = {}


def _runtime_key(spec: ModelSpec, device_id: int) -> tuple[str, str, str, int]:
    path = str(spec.path.resolve())
    manifest = verify_model_manifest(path)
    return path, str(manifest["onnxSha256"]), spec.precision, device_id


def get_model_runtime(spec: ModelSpec, runtime_root: Path) -> ModelRuntime:
    device_id = max(0, int(compatible_environment_value("ROTOWEAVE_CUDA_DEVICE", "0")))
    key = _runtime_key(spec, device_id)
    with _registry_lock:
        runtime = _registry.get(key)
        if runtime is None:
            runtime = ModelRuntime(spec, runtime_root.resolve(), device_id)
            _registry[key] = runtime
        return runtime


def start_model_warmup(spec: ModelSpec, runtime_root: Path) -> ModelRuntime:
    runtime = get_model_runtime(spec, runtime_root)
    key = _runtime_key(spec, runtime.device_id)
    with _registry_lock:
        if runtime.snapshot()["state"] == "warming" and key not in _warmup_threads:
            def warm() -> None:
                try:
                    runtime.ensure_ready()
                except Exception:
                    pass
                finally:
                    with _registry_lock:
                        _warmup_threads.pop(key, None)

            thread = threading.Thread(
                target=warm,
                name=f"rotoweave-warm-{spec.model_id}",
                daemon=True,
            )
            _warmup_threads[key] = thread
            thread.start()
    return runtime


def clear_runtime_registry() -> None:
    """Release cached sessions; intended for controlled shutdown and tests."""

    with _registry_lock:
        _registry.clear()
        _warmup_threads.clear()
    verify_model_manifest.cache_clear()
