from __future__ import annotations

from contracts.legacy_compat import compatible_environment_value

import gc
import importlib
import inspect
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import traceback
import uuid
from pathlib import Path
from typing import Any, Callable

import numpy as np

from .linear_media import (
    UncertaintyFlag,
    read_linear_exr,
    write_compatibility_rgba_png,
    write_confidence_exr,
    write_emission_png,
    write_linear_exr,
    write_uncertainty_png,
)
from contracts.hardware import HardwareWarning, probe_cuda_hardware
from contracts.integrity import atomic_write_json, canonical_json_bytes, read_json
from contracts.model_recipe import ASSET_BY_ROLE, MODEL_RECIPE_ID, PROFILE_ROLES, RECIPE_DIGEST

from . import PROTOCOL_VERSION


HEARTBEAT_SECONDS = 5.0
BLACK_BACKGROUND_MAX_LINEAR = 0.03
BLACK_BACKGROUND_TARGET_LINEAR = 0.012
MIN_DARK_BACKGROUND_RATIO = 0.35
MIN_DARK_BORDER_RATIO = 0.65
MAX_BLACK_BORDER_RESIDUAL = 0.015
EMISSION_SIGNAL_THRESHOLD = 0.002
EMISSION_CLIP_VALUE = 0.999
MAX_CLIPPED_PIXEL_RATIO = 0.20
MAX_LUMINANCE_CLIPPED_RATIO = 0.05

_OUTPUT_LOCK = threading.Lock()
_SHUTDOWN = threading.Event()
_WORKER_INSTANCE_ID = f"{os.getpid()}-{uuid.uuid4().hex}"


class RequestCancelled(RuntimeError):
    pass


def _emit(payload: dict[str, Any]) -> None:
    line = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    with _OUTPUT_LOCK:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


def _log(message: str) -> None:
    sys.stderr.write(message.rstrip() + "\n")
    sys.stderr.flush()


def _heartbeat() -> None:
    while not _SHUTDOWN.wait(HEARTBEAT_SECONDS):
        _emit({"protocol": PROTOCOL_VERSION, "event": "heartbeat", "at": time.time()})


def _safe_generation_path(root: Path, value: object, *, must_exist: bool) -> Path:
    candidate = Path(str(value or "")).resolve(strict=False)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise RuntimeError("Worker path is outside the generation root.") from exc
    if candidate.is_symlink():
        raise RuntimeError("Worker generation paths cannot be symlinks.")
    if must_exist and not candidate.is_file():
        raise RuntimeError(f"Worker input does not exist: {candidate.name}.")
    return candidate


def _generation_root() -> Path:
    configured = compatible_environment_value("ROTOWEAVE_GENERATION_ROOT")
    if not configured:
        raise RuntimeError("ROTOWEAVE_GENERATION_ROOT is required.")
    root = Path(configured).resolve(strict=False)
    root.mkdir(parents=True, exist_ok=True)
    return root


def _torch_health() -> dict[str, Any]:
    if compatible_environment_value("ROTOWEAVE_MEMORY_MODE") == "minimal":
        smoke_script = (
            "import json,torch;"
            "available=bool(torch.cuda.is_available());"
            "value=(torch.ones(1,device='cuda',dtype=torch.float32)+1.0) if available else None;"
            "torch.cuda.synchronize() if available else None;"
            "print(json.dumps({'available':True,'version':str(torch.__version__),'cudaAvailable':available,"
            "'cudaRuntime':str(torch.version.cuda or ''),'cudnnMajor':int(torch.backends.cudnn.version()//10000) if torch.backends.cudnn.version() else None,"
            "'device':torch.cuda.get_device_name(0) if available else None,'computeCapability':'.'.join(str(x) for x in torch.cuda.get_device_capability(0)) if available else None,"
            "'cudaSmokePassed':bool(available and float(value.item())==2.0)}))"
        )
        try:
            completed = subprocess.run(
                [sys.executable, "-c", smoke_script],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=120,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            payload = json.loads(completed.stdout.strip()) if completed.returncode == 0 else {}
            if payload.get("cudaSmokePassed") is True:
                return {**payload, "smokeIsolation": "subprocess", "warnings": []}
            raise RuntimeError(completed.stderr.strip() or "isolated CUDA smoke failed")
        except Exception as exc:
            return {
                "available": False,
                "version": None,
                "cudaAvailable": False,
                "cudaRuntime": None,
                "cudnnMajor": None,
                "device": None,
                "computeCapability": None,
                "cudaSmokePassed": False,
                "smokeError": str(exc),
                "smokeIsolation": "subprocess",
                "warnings": [
                    HardwareWarning(
                        code="cuda_arch_incompatible",
                        message="固定 PyTorch 运行时无法在所选 GPU 完成隔离 CUDA 内核冒烟。",
                        action="更新兼容运行时或驱动后重新执行 Profile 自检。",
                        scope="runtime",
                    ).as_dict()
                ],
            }
    try:
        import torch
    except ImportError:
        warning = HardwareWarning(
            code="cuda_runtime_unavailable",
            message="固定 PyTorch CUDA 运行时不可用；服务 API 仍可启动。",
            action="修复固定运行时后重新执行 Profile 自检。",
            scope="runtime",
        ).as_dict()
        return {
            "available": False,
            "version": None,
            "cudaAvailable": False,
            "cudaRuntime": None,
            "cudnnMajor": None,
            "device": None,
            "cudaSmokePassed": False,
            "warnings": [warning],
        }
    cuda_available = bool(torch.cuda.is_available())
    smoke_passed = False
    warning_records: list[dict[str, Any]] = []
    smoke_error: str | None = None
    if cuda_available:
        try:
            smoke = torch.ones(1, device="cuda", dtype=torch.float32) + 1.0
            torch.cuda.synchronize()
            smoke_passed = float(smoke.item()) == 2.0
            if not smoke_passed:
                raise RuntimeError("invalid CUDA smoke result")
        except Exception as exc:
            smoke_error = str(exc)
            warning_records.append(
                HardwareWarning(
                    code="cuda_arch_incompatible",
                    message="固定 PyTorch 运行时无法在所选 GPU 完成 CUDA 内核冒烟。",
                    action="更新兼容运行时或驱动后重新执行 Profile 自检。",
                    scope="runtime",
                ).as_dict()
            )
    else:
        warning_records.append(
            HardwareWarning(
                code="cuda_runtime_unavailable",
                message="固定 PyTorch 运行时未检测到可用 CUDA；服务 API 仍可启动。",
                action="检查 NVIDIA 驱动与固定运行时后重新执行 Profile 自检。",
                scope="runtime",
            ).as_dict()
        )
    return {
        "available": True,
        "version": str(torch.__version__),
        "cudaAvailable": cuda_available,
        "cudaRuntime": str(torch.version.cuda or ""),
        "cudnnMajor": (
            int(torch.backends.cudnn.version() // 10000)
            if torch.backends.cudnn.version()
            else None
        ),
        "device": torch.cuda.get_device_name(0) if cuda_available else None,
        "computeCapability": (
            ".".join(str(item) for item in torch.cuda.get_device_capability(0))
            if cuda_available
            else None
        ),
        "cudaSmokePassed": smoke_passed,
        "smokeError": smoke_error,
        "warnings": warning_records,
    }


def _configuration_path() -> Path | None:
    configured = str(compatible_environment_value("ROTOWEAVE_MODEL_CONFIGURATION") or "").strip()
    return Path(configured).resolve(strict=False) if configured else None


def _configuration_health() -> dict[str, Any]:
    from .model_runtime import FrozenModelLayout

    path = _configuration_path()
    if path is None:
        raise RuntimeError("No model configuration is mounted.")
    # Loading the layout independently rechecks bytes, SHA-256 and any
    # structural receipt before this process can report READY.
    FrozenModelLayout.from_configuration(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    profile = str(compatible_environment_value("ROTOWEAVE_RUNTIME_PROFILE") or payload.get("profile") or "").lower()
    if profile not in PROFILE_ROLES or payload.get("profile") != profile:
        raise RuntimeError("The runtime Profile does not match its configuration.")
    runtime = payload.get("runtime") if isinstance(payload.get("runtime"), dict) else {}
    assets = payload.get("assets") if isinstance(payload.get("assets"), dict) else {}
    profile_receipt = (
        (payload.get("profileExecutionReceipts") or {}).get(profile)
        if isinstance(payload.get("profileExecutionReceipts"), dict)
        else None
    )
    models = []
    for role in PROFILE_ROLES[profile]:
        recipe = ASSET_BY_ROLE[role]
        record = assets.get(role) if isinstance(assets.get(role), dict) else {}
        models.append(
            {
                "id": recipe.model_id,
                "role": role,
                "revision": recipe.revision,
                "sha256": record.get("sha256"),
                "verificationKind": record.get("verificationKind") or "official",
                "verificationReceiptDigest": record.get("verificationReceiptDigest"),
                "runtimeContract": recipe.runtime_contract,
                "releaseEligible": True,
            }
        )
    return {
        "state": "ready",
        "configurationDigest": payload.get("configurationDigest"),
        "recipeId": MODEL_RECIPE_ID,
        "recipeDigest": RECIPE_DIGEST,
        "profile": profile,
        "profileConfigurationDigest": payload.get("profileConfigurationDigest"),
        "qualification": (
            profile_receipt.get("qualification")
            if isinstance(profile_receipt, dict)
            else "official"
        ),
        "localCompatibleRoles": (
            profile_receipt.get("localCompatibleRoles")
            if isinstance(profile_receipt, dict)
            else []
        ),
        "runtimeDigest": runtime.get("digest"),
        "runtimeId": runtime.get("id"),
        "selfTestReceiptDigest": (
            (payload.get("selfTestReceipts") or {}).get(profile)
            if isinstance(payload.get("selfTestReceipts"), dict)
            else None
        ),
        "models": models,
        "verifiedFileCount": len(models),
    }


def _health() -> dict[str, Any]:
    try:
        configuration = _configuration_health()
    except Exception as exc:
        configuration = {
            "state": "unavailable",
            "recipeId": MODEL_RECIPE_ID,
            "models": [],
            "fallbackReason": str(exc),
        }
    hardware = probe_cuda_hardware(compatible_environment_value("ROTOWEAVE_SELECTED_GPU_UUID") or None).as_dict()
    torch_health = _torch_health()
    warnings = [*(hardware.get("warnings") or []), *(torch_health.get("warnings") or [])]
    hardware["warnings"] = warnings
    hardware["cudaSmokePassed"] = bool(torch_health.get("cudaSmokePassed"))
    hardware["compatibilityState"] = (
        "ready" if hardware.get("available") and torch_health.get("cudaSmokePassed") else "unavailable"
    )
    return {
        "protocol": PROTOCOL_VERSION,
        "worker": "cuda-matting-worker",
        "workerId": _WORKER_INSTANCE_ID,
        "python": ".".join(str(item) for item in sys.version_info[:3]),
        "torch": torch_health,
        "hardware": hardware,
        "modelConfiguration": configuration,
        "precision": "fp16",
        "warnings": warnings,
    }


def _process_ram_mib() -> float:
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        class ProcessMemoryCounters(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
                ("PrivateUsage", ctypes.c_size_t),
            ]

        counters = ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        kernel32.GetCurrentProcess.argtypes = []
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(ProcessMemoryCounters),
            wintypes.DWORD,
        ]
        psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
        process = kernel32.GetCurrentProcess()
        if not psapi.GetProcessMemoryInfo(
            process, ctypes.byref(counters), counters.cb
        ):
            error_code = ctypes.get_last_error()
            raise RuntimeError(
                "Unable to read Worker process memory telemetry "
                f"(Windows error {error_code})."
            )
        return float(counters.WorkingSetSize / 2**20)
    status = Path("/proc/self/status")
    if status.is_file():
        for line in status.read_text(encoding="utf-8").splitlines():
            if line.startswith("VmRSS:"):
                return float(line.split()[1]) / 1024.0
    raise RuntimeError("Unable to read Worker process memory telemetry.")


def _current_vram_mib() -> float:
    if compatible_environment_value("ROTOWEAVE_MEMORY_MODE") == "minimal":
        return 0.0
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.synchronize()
            return float(torch.cuda.memory_reserved() / 2**20)
    except (ImportError, RuntimeError):
        pass
    return 0.0


class _TaskTelemetry:
    def __init__(self, params: dict[str, Any]):
        self.started = time.perf_counter()
        self.start_ram_mib = _process_ram_mib()
        self.start_vram_mib = _current_vram_mib()
        self.intervals: dict[str, list[list[float]]] = {
            "cpuDecode": [],
            "cpuInference": [],
            "gpuInference": [],
        }
        input_path = Path(str(params.get("inputManifest") or ""))
        payload = read_json(input_path)
        frames = payload.get("frames")
        if not isinstance(frames, list) or not frames:
            raise RuntimeError("Telemetry input manifest contains no frames.")
        self.frames = len(frames)
        self.width = max(int(item.get("width") or 0) for item in frames if isinstance(item, dict))
        self.height = max(int(item.get("height") or 0) for item in frames if isinstance(item, dict))
        try:
            if compatible_environment_value("ROTOWEAVE_MEMORY_MODE") == "minimal":
                return
            import torch

            if torch.cuda.is_available():
                torch.cuda.reset_peak_memory_stats()
        except (ImportError, RuntimeError):
            pass

    def record(self, kind: str, started: float, ended: float) -> None:
        target = self.intervals.get(kind)
        if target is None or ended <= started:
            return
        target.append(
            [
                (started - self.started) * 1000.0,
                (ended - self.started) * 1000.0,
            ]
        )

    def finish(self, result: dict[str, Any]) -> dict[str, Any]:
        ended = time.perf_counter()
        memory = result.get("memory") if isinstance(result.get("memory"), dict) else {}
        peak_vram = max(
            self.start_vram_mib,
            float(memory.get("peakAllocatedMiB") or 0.0),
            float(memory.get("peakReservedMiB") or 0.0),
        )
        configuration = result.get("modelConfiguration") if isinstance(result.get("modelConfiguration"), dict) else {}
        configuration_digest = str(configuration.get("configurationDigest") or "")
        return {
            "frames": self.frames,
            "width": self.width,
            "height": self.height,
            "durationMs": (ended - self.started) * 1000.0,
            "peakVramMiB": peak_vram,
            "startVramMiB": self.start_vram_mib,
            "endVramMiB": _current_vram_mib(),
            "startRamMiB": self.start_ram_mib,
            "endRamMiB": _process_ram_mib(),
            "oomCount": 0,
            "nanCount": 0,
            "failureCount": 0,
            "workerId": _WORKER_INSTANCE_ID,
            "modelConfigurationDigest": configuration_digest,
            "cpuDecodeIntervalsMs": self.intervals["cpuDecode"],
            "cpuInferenceIntervalsMs": self.intervals["cpuInference"],
            "gpuInferenceIntervalsMs": self.intervals["gpuInference"],
        }


def _black_background_statistics(rgb: np.ndarray) -> dict[str, float]:
    luminance = (
        0.2126 * rgb[:, :, 0] + 0.7152 * rgb[:, :, 1] + 0.0722 * rgb[:, :, 2]
    )
    dark_ratio = float(np.mean(luminance <= BLACK_BACKGROUND_TARGET_LINEAR))
    height, width = luminance.shape
    border_width = max(1, min(height, width) // 32)
    border_mask = np.zeros((height, width), dtype=bool)
    border_mask[:border_width, :] = True
    border_mask[-border_width:, :] = True
    border_mask[:, :border_width] = True
    border_mask[:, -border_width:] = True
    border_pixels = rgb[border_mask]
    border_luminance = luminance[border_mask]
    dark_border_pixels = border_pixels[
        border_luminance <= BLACK_BACKGROUND_TARGET_LINEAR
    ]
    # Emissive strokes and particles are allowed to cross the frame boundary.
    # Estimate the canvas only from its dark support instead of treating every
    # boundary pixel as background. The global and border dark-ratio gates
    # below still reject small black patches and ordinary non-black scenes.
    background_sample = (
        dark_border_pixels if len(dark_border_pixels) else border_pixels
    )
    background_rgb = np.median(background_sample, axis=0).astype(np.float32)
    residual = np.linalg.norm(
        background_sample - background_rgb[None, :], axis=1
    )
    return {
        "darkRatio": dark_ratio,
        "borderDarkRatio": float(
            np.mean(border_luminance <= BLACK_BACKGROUND_TARGET_LINEAR)
        ),
        "backgroundR": float(background_rgb[0]),
        "backgroundG": float(background_rgb[1]),
        "backgroundB": float(background_rgb[2]),
        "backgroundMax": float(
            np.percentile(np.max(background_sample, axis=1), 95)
        ),
        "backgroundP95Residual": float(np.percentile(residual, 95)),
        "borderP95Energy": float(
            np.percentile(np.max(border_pixels, axis=1), 95)
        ),
    }


def _emission_clipping_statistics(rgb: np.ndarray) -> dict[str, float]:
    clipped_channels = rgb >= EMISSION_CLIP_VALUE
    clipped_pixels = np.any(clipped_channels, axis=2)
    luminance = (
        0.2126 * rgb[:, :, 0] + 0.7152 * rgb[:, :, 1] + 0.0722 * rgb[:, :, 2]
    )
    return {
        "clippingRatio": float(np.mean(clipped_pixels)),
        "channelClippingRatio": float(np.mean(clipped_channels)),
        "luminanceClippingRatio": float(
            np.mean(luminance >= EMISSION_CLIP_VALUE)
        ),
    }


def _is_destructively_clipped(stats: dict[str, float]) -> bool:
    return bool(
        stats["clippingRatio"] > MAX_CLIPPED_PIXEL_RATIO
        or stats["luminanceClippingRatio"] > MAX_LUMINANCE_CLIPPED_RATIO
    )


def _validated_emission_energy(rgb: np.ndarray) -> tuple[np.ndarray, dict[str, float]]:
    value = np.asarray(rgb, dtype=np.float32)
    if value.ndim != 3 or value.shape[2] != 3 or not np.isfinite(value).all():
        raise RuntimeError("emissive_vfx input must be finite linear RGB.")
    if float(value.min()) < -1e-4:
        raise RuntimeError("emissive_vfx input contains negative linear energy.")
    stats = _black_background_statistics(value)
    if (
        stats["darkRatio"] < MIN_DARK_BACKGROUND_RATIO
        or stats["borderDarkRatio"] < MIN_DARK_BORDER_RATIO
        or stats["backgroundMax"] > BLACK_BACKGROUND_TARGET_LINEAR
        or stats["backgroundP95Residual"] > MAX_BLACK_BORDER_RESIDUAL
    ):
        raise RuntimeError(
            "emissive_vfx input does not have a uniformly black boundary; reconfirm the material type or provide a true black-background source."
        )
    # The validated source is the additive energy authority.  Do not estimate
    # and subtract a black level: that destroys dim particles and glow tails.
    return np.maximum(value, 0.0).copy(), stats


def _validate_emission_sequence_energy(
    lit_pixel_count: int,
    peak_energy: float,
) -> None:
    if lit_pixel_count <= 0 or peak_energy <= EMISSION_SIGNAL_THRESHOLD:
        raise RuntimeError(
            "emissive_vfx source contains no usable emission energy; reconfirm the material type or provide a non-empty black-background effect."
        )


def _process_emissive(
    params: dict[str, Any],
    cancel: threading.Event,
    record_interval: Callable[[str, float, float], None],
) -> dict[str, Any]:
    root = _generation_root()
    manifest_path = _safe_generation_path(root, params.get("inputManifest"), must_exist=True)
    input_manifest = read_json(manifest_path)
    if input_manifest.get("schemaVersion") != 1:
        raise RuntimeError("Unsupported worker input manifest.")
    frames = input_manifest.get("frames")
    if not isinstance(frames, list) or not frames:
        raise RuntimeError("Worker input manifest contains no frames.")
    output_root = _safe_generation_path(root, params.get("outputDirectory"), must_exist=False)
    output_root.mkdir(parents=True, exist_ok=False)
    results: list[dict[str, Any]] = []
    lit_pixel_count = 0
    peak_energy = 0.0
    try:
        for ordinal, item in enumerate(frames):
            if cancel.is_set():
                raise RequestCancelled("Matting request was cancelled.")
            if not isinstance(item, dict):
                raise RuntimeError("Worker frame record is invalid.")
            source_path = _safe_generation_path(root, item.get("sourceExr"), must_exist=True)
            decode_started = time.perf_counter()
            try:
                rgb, source_alpha = read_linear_exr(source_path)
            finally:
                record_interval("cpuDecode", decode_started, time.perf_counter())
            if source_alpha is not None and float(np.min(source_alpha)) < 0.999:
                raise RuntimeError("emissive_vfx expects an opaque source video frame.")
            emission, stats = _validated_emission_energy(rgb)
            energy = np.max(emission, axis=2)
            lit_pixel_count += int(
                np.count_nonzero(energy > EMISSION_SIGNAL_THRESHOLD)
            )
            peak_energy = max(peak_energy, float(np.max(energy)))
            compatibility_alpha = np.clip(1.0 - np.exp(-4.0 * energy), 0.0, 1.0)
            compatibility_premult = emission * compatibility_alpha[:, :, None]
            clipping_stats = _emission_clipping_statistics(rgb)
            if _is_destructively_clipped(clipping_stats):
                raise RuntimeError(
                    "emissive_vfx source energy is clipped; reconfirm the material type or provide an unclipped black-background source."
                )
            background_confidence = float(
                np.clip(
                    1.0
                    - stats["backgroundMax"] / BLACK_BACKGROUND_MAX_LINEAR
                    - stats["backgroundP95Residual"] / 0.05,
                    0.0,
                    1.0,
                )
            )
            confidence = np.full(rgb.shape[:2], background_confidence, dtype=np.float32)
            flags = np.zeros(rgb.shape[:2], dtype=np.uint16)
            warnings: list[str] = []
            uncertainty_reasons: list[str] = []
            if clipping_stats["clippingRatio"] > 0.001:
                clipped_pixels = np.any(rgb >= EMISSION_CLIP_VALUE, axis=2)
                flags[clipped_pixels] |= np.uint16(int(UncertaintyFlag.SOURCE_LIMIT))
                warnings.append("source-limit-channel-saturation")
                uncertainty_reasons.append("source-limit")

            frame_root = output_root / f"{ordinal:06d}"
            frame_root.mkdir()
            matte_path = frame_root / "matte.exr"
            emission_path = frame_root / "emission.exr"
            confidence_path = frame_root / "confidence.exr"
            uncertainty_path = frame_root / "uncertainty.png"
            delivery_emission_path = frame_root / "delivery-emission.png"
            compatibility_path = frame_root / "compatibility-rgba.png"
            write_linear_exr(
                matte_path,
                np.zeros_like(rgb, dtype=np.float32),
                np.zeros(rgb.shape[:2], dtype=np.float32),
            )
            write_linear_exr(emission_path, emission)
            write_confidence_exr(confidence_path, confidence)
            write_uncertainty_png(uncertainty_path, flags)
            write_emission_png(delivery_emission_path, emission)
            write_compatibility_rgba_png(
                compatibility_path,
                compatibility_premult,
                compatibility_alpha,
            )
            results.append(
                {
                    "frameId": str(item.get("frameId") or ""),
                    "route": "emissive_vfx",
                    "blendMode": "additive",
                    "mattePath": str(matte_path),
                    "emissionPath": str(emission_path),
                    "confidencePath": str(confidence_path),
                    "uncertaintyPath": str(uncertainty_path),
                    "deliveryBasePath": None,
                    "deliveryEmissionPath": str(delivery_emission_path),
                    "compatibilityRgbaPath": str(compatibility_path),
                    "confidence": background_confidence,
                    "uncertaintyReasons": sorted(set(uncertainty_reasons)),
                    "warnings": sorted(set(warnings)),
                    "qc": {
                        **stats,
                        **clipping_stats,
                        "source-limit": bool(uncertainty_reasons),
                        "particlePixelRatio": float(
                            np.mean(energy > EMISSION_SIGNAL_THRESHOLD)
                        ),
                        "finite": True,
                    },
                }
            )
        _validate_emission_sequence_energy(lit_pixel_count, peak_energy)
        manifest = {
            "schemaVersion": 1,
            "route": "emissive_vfx",
            "blendMode": "additive",
            "qcPassed": True,
            "frames": results,
            "provenance": {
                "worker": "cuda-matting-worker",
                "protocol": PROTOCOL_VERSION,
                "algorithm": "linear-black-additive-v4",
                "blackCanvasEstimator": "dark-border-support-v2",
                "clippingPolicy": "warn-channel-saturation-block-destructive-v2",
                "modelCallsPerFrame": 0,
                "roiRefinementsPerFrame": 0,
                "inputManifestSha256": __import__("hashlib").sha256(
                    canonical_json_bytes(input_manifest)
                ).hexdigest(),
            },
        }
        output_manifest = output_root / "matte-result.manifest.json"
        atomic_write_json(output_manifest, manifest)
        return {"outputManifest": str(output_manifest), **manifest}
    except BaseException:
        # Output is a private, unpublished generation.  The app owns final
        # cleanup and will retain the previously published MatteResult.
        raise


def _load_product_adapter() -> tuple[Any, dict[str, Any]]:
    configuration = _configuration_health()
    adapter = importlib.import_module("worker.cuda_matting.rotoweave_adapter")
    if not callable(getattr(adapter, "process_route", None)):
        raise RuntimeError("Product model adapter has no process_route function.")
    return adapter, configuration


def _release_cuda() -> dict[str, float | int | None]:
    result: dict[str, float | int | None] = {
        "peakAllocatedMiB": None,
        "peakReservedMiB": None,
        "peakWorkingSetMiB": _process_ram_mib(),
    }
    try:
        if compatible_environment_value("ROTOWEAVE_MEMORY_MODE") == "minimal":
            gc.collect()
            return result
        import torch

        if torch.cuda.is_available():
            torch.cuda.synchronize()
            result["peakAllocatedMiB"] = int(torch.cuda.max_memory_allocated() / 2**20)
            result["peakReservedMiB"] = int(torch.cuda.max_memory_reserved() / 2**20)
            torch.cuda.empty_cache()
            torch.cuda.ipc_collect()
    except (ImportError, RuntimeError):
        pass
    gc.collect()
    return result


def _process_model_route(
    route: str,
    params: dict[str, Any],
    cancel: threading.Event,
    record_interval: Callable[[str, float, float], None],
) -> dict[str, Any]:
    if route != "chroma_character":
        raise RuntimeError(f"Unsupported model route: {route}.")
    adapter, pack_health = _load_product_adapter()
    profile = str(params.get("profile") or "high").strip().lower()
    if profile not in {"high", "ultra"}:
        raise RuntimeError("Production model routes require high or approved Ultra.")
    installed_models = {
        str(item.get("id") or "")
        for item in pack_health.get("models") or []
        if isinstance(item, dict)
    }
    configured_profile = str(pack_health.get("profile") or "").strip().lower()
    if configured_profile and profile != configured_profile:
        raise RuntimeError(
            f"The resident {configured_profile.upper()} Worker cannot execute {profile.upper()}."
        )
    if profile == "ultra" and "sam3" not in installed_models:
        raise RuntimeError(
            "Ultra requires the exact Recipe SAM3 asset in the fixed Ultra runtime."
        )
    call_trace: list[dict[str, Any]] = []

    def check_cancel() -> None:
        if cancel.is_set():
            raise RequestCancelled("Matting request was cancelled.")

    def record_call(frame_id: str, model_id: str, *, roi: bool = False) -> None:
        call_trace.append(
            {"frameId": str(frame_id), "modelId": str(model_id), "roi": bool(roi)}
        )

    adapter_parameters = inspect.signature(adapter.process_route).parameters
    adapter_arguments: dict[str, Any] = {
        "route": route,
        "params": dict(params),
        "check_cancel": check_cancel,
        "record_call": record_call,
    }
    if "record_interval" in adapter_parameters:
        adapter_arguments["record_interval"] = record_interval
    result = adapter.process_route(**adapter_arguments)
    if not isinstance(result, dict):
        raise RuntimeError("Signed model adapter returned a non-object result.")
    if not isinstance(result.get("qcPassed"), bool):
        raise RuntimeError("Signed model adapter must return an explicit qcPassed boolean.")
    provenance = result.get("provenance")
    if not isinstance(provenance, dict):
        raise RuntimeError("Signed model adapter returned no provenance contract.")
    if profile == "ultra":
        sam3 = next(
            (
                item
                for item in pack_health.get("models") or []
                if isinstance(item, dict) and item.get("id") == "sam3"
            ),
            None,
        )
        if sam3 is None:
            raise RuntimeError("Verified Ultra execution lost its SAM3 identity.")
        local_ultra = None
        provenance.update(
            {
                "requestedProfile": "ultra",
                "publishedCandidate": (
                    "sam3-ultra-local-unverified" if local_ultra else "sam3-ultra"
                ),
                "releaseEligible": False if local_ultra else True,
                "mainAlphaModel": {
                    "id": "sam3",
                    "revision": sam3.get("revision"),
                    "sha256": sam3.get("sha256"),
                },
                "runtimeContract": sam3.get("runtimeContract"),
                "selectionContract": sam3.get("selectionContract"),
                "selectionReportSha256": sam3.get("selectionReportSha256"),
                "localCandidateIdentity": (
                    {
                        key: local_ultra.get(key)
                        for key in (
                            "sourceRevision",
                            "modelScopeRevision",
                            "checkpointSha256",
                            "dependencyLockSha256",
                            "receiptSha256",
                        )
                    }
                    if local_ultra
                    else None
                ),
            }
        )
    if (
        provenance.get("sourceColorAuthority") != "original-linear-rec709"
        or provenance.get("temporalRgbPropagation") is not False
        or provenance.get("generativeRepaint") is not False
    ):
        raise RuntimeError(
            "Signed model adapter violated the original-color/no-generative-RGB contract."
        )
    if route == "chroma_character" and provenance.get(
        "premultipliedRgbReconstruction"
    ) != (
        "original-C-corridorkey-virtual-K-final-alpha-"
        "with-bounded-model-F-fallback"
    ):
        raise RuntimeError(
            "Chroma output must bind original C, CorridorKey virtual K, final Alpha, and its bounded model-F fallback."
        )
    frames = result.get("frames")
    if not isinstance(frames, list) or not frames:
        raise RuntimeError("Signed model adapter returned no frames.")
    frame_ids = {str(item.get("frameId") or "") for item in frames if isinstance(item, dict)}
    if "" in frame_ids or len(frame_ids) != len(frames):
        raise RuntimeError("Signed model adapter returned duplicate/empty frame ids.")
    # The adapter records calls at frame granularity.  Enforce one main call
    # and at most one ROI refinement for every frame before publication.
    counts: dict[str, tuple[int, int]] = {}
    for record in call_trace:
        frame_id = str(record.get("frameId") or "")
        main, roi = counts.get(frame_id, (0, 0))
        if record.get("roi"):
            roi += 1
        else:
            main += 1
        counts[frame_id] = (main, roi)
    if set(counts) != frame_ids or any(
        main != 1 or roi > 1 for main, roi in counts.values()
    ):
        raise RuntimeError(
            "Production call trace must contain exactly one main chain and at most one ROI per frame."
        )
    memory = _release_cuda()
    for phase_memory in provenance.get("isolatedPhaseMemory") or []:
        if not isinstance(phase_memory, dict):
            continue
        for key in ("peakAllocatedMiB", "peakReservedMiB", "peakWorkingSetMiB"):
            memory[key] = max(
                float(memory.get(key) or 0),
                float(phase_memory.get(key) or 0),
            )
    mode = str(params.get("memoryMode") or "full").strip().lower()
    cpu_stages = list(provenance.get("cpuStages") or [])
    result["callTrace"] = call_trace
    result["modelConfiguration"] = {
        "configurationDigest": pack_health.get("configurationDigest"),
        "recipeId": pack_health.get("recipeId"),
        "recipeDigest": pack_health.get("recipeDigest"),
        "profile": profile,
        "profileConfigurationDigest": pack_health.get("profileConfigurationDigest"),
        "qualification": pack_health.get("qualification"),
        "localCompatibleRoles": pack_health.get("localCompatibleRoles") or [],
        "runtimeDigest": pack_health.get("runtimeDigest"),
        "selfTestReceiptDigest": pack_health.get("selfTestReceiptDigest"),
        "models": pack_health.get("models") or [],
    }
    result["memory"] = memory
    result["execution"] = {
        "deviceUuid": compatible_environment_value("ROTOWEAVE_SELECTED_GPU_UUID")
        or (_health().get("hardware") or {}).get("gpuUuid"),
        "memoryMode": mode,
        "cpuStages": cpu_stages,
        "peakAllocatedMiB": memory.get("peakAllocatedMiB"),
        "peakReservedMiB": memory.get("peakReservedMiB"),
        "peakWorkingSetMiB": memory.get("peakWorkingSetMiB"),
        "warnings": (
            [
                HardwareWarning(
                    code="cpu_stage_active",
                    message="CorridorKey 正在使用 CPU float32 执行，耗时可能显著增加。",
                    action="保持任务运行；如需更快速度可释放 GPU 显存后重试。",
                    scope="job",
                    profile=profile,
                ).as_dict()
            ]
            if cpu_stages
            else []
        ),
    }
    return result


def _profile_self_test(params: dict[str, Any] | None = None) -> dict[str, Any]:
    health = _health()
    configuration = health.get("modelConfiguration") or {}
    hardware = health.get("hardware") or {}
    if configuration.get("state") != "ready":
        raise RuntimeError(str(configuration.get("fallbackReason") or "Configuration is unavailable."))
    if hardware.get("cudaSmokePassed") is not True:
        raise RuntimeError("Profile self-test requires a successful CUDA kernel smoke test.")
    mode = str((params or {}).get("memoryMode") or "full").strip().lower()
    if mode not in {"full", "balanced", "constrained", "minimal"}:
        raise RuntimeError("Unsupported CUDA memory mode.")
    profile = str(configuration.get("profile") or "")
    test_root = _generation_root() / f"profile-self-test-{uuid.uuid4().hex}"
    inputs = test_root / "inputs"
    inputs.mkdir(parents=True, exist_ok=False)
    image = np.zeros((384, 384, 3), dtype=np.float32)
    image[:, :, 1] = 0.45
    image[96:288, 112:272] = np.array([0.42, 0.08, 0.20], dtype=np.float32)
    source = inputs / "000000.exr"
    write_linear_exr(source, image)
    manifest_path = test_root / "worker-input.manifest.json"
    atomic_write_json(
        manifest_path,
        {
            "schemaVersion": 1,
            "route": "chroma_character",
            "sourceSha256": __import__("hashlib").sha256(source.read_bytes()).hexdigest(),
            "constraintsHash": "profile-self-test-v2",
            "cleanPlate": {"mode": "auto"},
            "frames": [
                {
                    "frameId": "profile-self-test-frame",
                    "frameIndex": 0,
                    "sourceExr": str(source),
                    "sourceSha256": __import__("hashlib").sha256(source.read_bytes()).hexdigest(),
                    "width": 384,
                    "height": 384,
                    "timeUs": 0,
                    "sourceTimelineOrdinal": 0,
                }
            ],
        },
    )
    stage_durations_ms: dict[str, float] = {}

    def record_interval(kind: str, started: float, ended: float) -> None:
        stage_durations_ms[kind] = stage_durations_ms.get(kind, 0.0) + max(
            0.0, (ended - started) * 1000.0
        )

    try:
        result = _process_model_route(
            "chroma_character",
            {
                "route": "chroma_character",
                "inputManifest": str(manifest_path),
                "outputDirectory": str(test_root / "candidate"),
                "profile": profile,
                "constraintsHash": "profile-self-test-v2",
                "maxRoiRefinements": 1,
                "memoryMode": mode,
            },
            threading.Event(),
            record_interval,
        )
        frames = result.get("frames") or []
        if len(frames) != 1:
            raise RuntimeError("Profile self-test did not return exactly one frame.")
        _premultiplied, alpha = read_linear_exr(Path(str(frames[0].get("mattePath") or "")))
        if alpha is None or alpha.shape != image.shape[:2] or not np.isfinite(alpha).all():
            raise RuntimeError("Profile self-test returned an invalid Alpha tensor.")
        memory = dict(result.get("memory") or {})
    finally:
        final_memory = _release_cuda()
        if "memory" not in locals():
            memory = final_memory
        else:
            for key, value in final_memory.items():
                if value is not None:
                    memory[key] = max(float(memory.get(key) or 0), float(value))
        shutil.rmtree(test_root, ignore_errors=True)
    selected = hardware.get("selectedDevice") or {}
    gpu_uuid = selected.get("uuid") or hardware.get("gpuUuid")
    working_set = _process_ram_mib()
    cpu_stages = ["corridorkey"] if mode in {"constrained", "minimal"} else []
    return {
        "state": "passed",
        "profile": profile,
        "configurationDigest": configuration.get("configurationDigest"),
        "runtimeDigest": configuration.get("runtimeDigest"),
        "gpuIdentity": gpu_uuid or hardware.get("gpuName"),
        "gpuUuid": gpu_uuid,
        "device": hardware.get("gpuName"),
        "driverVersion": hardware.get("driverVersion"),
        "mode": mode,
        "cpuStages": cpu_stages,
        "peakAllocatedMiB": memory.get("peakAllocatedMiB"),
        "peakReservedMiB": memory.get("peakReservedMiB"),
        "peakWorkingSetMiB": working_set,
        "stageDurationsMs": stage_durations_ms,
        "qualityParameters": {
            "resolution": 2048,
            "refiner": True,
            "roi": True,
            "profile": profile,
        },
        "alpha": {
            "shape": list(alpha.shape),
            "minimum": float(alpha.min()),
            "maximum": float(alpha.max()),
            "finite": True,
        },
        "memory": memory,
    }


def _run_request(
    request_id: str,
    params: dict[str, Any],
    cancel: threading.Event,
    on_done: Callable[[], None],
) -> None:
    try:
        telemetry = _TaskTelemetry(params)
        route = str(params.get("route") or "")
        if route == "emissive_vfx":
            result = _process_emissive(params, cancel, telemetry.record)
        else:
            result = _process_model_route(route, params, cancel, telemetry.record)
        result["telemetry"] = telemetry.finish(result)
        _emit({"protocol": PROTOCOL_VERSION, "id": request_id, "ok": True, "result": result})
    except RequestCancelled as exc:
        _emit(
            {
                "protocol": PROTOCOL_VERSION,
                "id": request_id,
                "ok": False,
                "error": {"code": "cancelled", "message": str(exc)},
            }
        )
    except BaseException as exc:
        _log(traceback.format_exc())
        _emit(
            {
                "protocol": PROTOCOL_VERSION,
                "id": request_id,
                "ok": False,
                "error": {"code": "inference-failed", "message": str(exc)},
            }
        )
    finally:
        try:
            _release_cuda()
        except RuntimeError as exc:
            _log(str(exc))
        on_done()


def main() -> int:
    threading.Thread(target=_heartbeat, name="worker-heartbeat", daemon=True).start()
    state_lock = threading.Lock()
    active: dict[str, Any] = {"id": None, "thread": None, "cancel": None}

    def clear_active() -> None:
        with state_lock:
            active.update(id=None, thread=None, cancel=None)

    for raw in sys.stdin:
        try:
            request = json.loads(raw)
            if not isinstance(request, dict) or request.get("protocol") != PROTOCOL_VERSION:
                raise RuntimeError("Unsupported worker protocol.")
            request_id = str(request.get("id") or uuid.uuid4().hex)
            method = str(request.get("method") or "")
            params = request.get("params")
            if not isinstance(params, dict):
                raise RuntimeError("Worker params must be an object.")
            if method == "health":
                _emit(
                    {
                        "protocol": PROTOCOL_VERSION,
                        "id": request_id,
                        "ok": True,
                        "result": _health(),
                    }
                )
            elif method == "self-test":
                with state_lock:
                    if active["thread"] is not None:
                        raise RuntimeError("Worker is a single GPU actor and is busy.")
                _emit(
                    {
                        "protocol": PROTOCOL_VERSION,
                        "id": request_id,
                        "ok": True,
                        "result": _profile_self_test(params),
                    }
                )
            elif method == "run":
                with state_lock:
                    if active["thread"] is not None:
                        raise RuntimeError("Worker is a single GPU actor and is busy.")
                    cancel = threading.Event()
                    thread = threading.Thread(
                        target=_run_request,
                        args=(request_id, params, cancel, clear_active),
                        name=f"cuda-matting-request-{request_id[:8]}",
                        daemon=True,
                    )
                    active.update(id=request_id, thread=thread, cancel=cancel)
                    thread.start()
            elif method == "cancel":
                target_id = str(params.get("requestId") or "")
                with state_lock:
                    cancelled = active["id"] == target_id and active["cancel"] is not None
                    if cancelled:
                        active["cancel"].set()
                _emit(
                    {
                        "protocol": PROTOCOL_VERSION,
                        "id": request_id,
                        "ok": True,
                        "result": {"cancelRequested": cancelled},
                    }
                )
            elif method == "shutdown":
                with state_lock:
                    if active["cancel"] is not None:
                        active["cancel"].set()
                    thread = active["thread"]
                if thread is not None:
                    thread.join(timeout=30)
                break
            else:
                raise RuntimeError(f"Unsupported worker method: {method}.")
        except BaseException as exc:
            request_id = (
                str(request.get("id") or "") if isinstance(locals().get("request"), dict) else ""
            )
            _emit(
                {
                    "protocol": PROTOCOL_VERSION,
                    "id": request_id,
                    "ok": False,
                    "error": {"code": "invalid-request", "message": str(exc)},
                }
            )
    _SHUTDOWN.set()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
