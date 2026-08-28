from __future__ import annotations

from contracts.legacy_compat import compatible_environment_value

import hashlib
import json
import os
import shutil
import subprocess
import threading
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

os.environ.setdefault("OPENCV_IO_ENABLE_OPENEXR", "1")
import cv2

from contracts.remote_archive import (
    canonical_sha256,
    result_payload_sha256,
)
from contracts.remote_protocol import (
    RemoteJobSubmission,
    RemoteOutputFrame,
    RemoteResultManifest,
)
from contracts.integrity import sha256_file
from contracts.model_recipe import MODEL_RECIPE_ID, RECIPE_DIGEST
from contracts.paths import contracts_root
from contracts.hardware import EXECUTION_MODES, memory_mode_plan, probe_cuda_hardware
from contracts.model_runtime_recipe import runtime_recipe

from .worker_client import CudaMattingWorkerClient, WorkerProtocolError
from .worker_io import (
    bgr_u8_to_linear_rgb,
    import_worker_candidate,
    stage_linear_inputs,
    write_linear_exr,
)


Progress = Callable[[float, str, str | None], None]
Control = Callable[[], None]


def _free_ram_mib() -> int:
    if os.name == "nt":
        import ctypes

        class MemoryStatusEx(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        status = MemoryStatusEx()
        status.dwLength = ctypes.sizeof(status)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return int(status.ullAvailPhys // 2**20)
    try:
        page_size = os.sysconf("SC_PAGE_SIZE")
        pages = os.sysconf("SC_AVPHYS_PAGES")
        return int(page_size * pages // 2**20)
    except (AttributeError, OSError, ValueError):
        return 0


@dataclass(frozen=True, slots=True)
class ServerWorkerSettings:
    runtime_root: Path
    cuda_matting_exchange_root: Path
    cuda_matting_worker_command: tuple[str, ...]
    model_configuration_path: Path | None = None
    runtime_profile: str | None = None
    selected_gpu_uuid: str | None = None
    memory_mode: str | None = None


def _worker_settings(
    data_root: Path,
    runtime_root: Path,
    *,
    profile: str | None = None,
    configuration_path: Path | None = None,
    selected_gpu_uuid: str | None = None,
    memory_mode: str | None = None,
) -> ServerWorkerSettings:
    configured_worker = compatible_environment_value("ROTOWEAVE_CUDA_MATTING_WORKER")
    if configured_worker:
        command = (str(Path(configured_worker).expanduser().resolve(strict=False)),)
    elif profile:
        configured_runtime = compatible_environment_value(f"ROTOWEAVE_{profile.upper()}_RUNTIME")
        contract = runtime_recipe(profile)
        runtime_python = (
            Path(configured_runtime).expanduser().resolve(strict=False)
            if configured_runtime
            else runtime_root / "server-runtimes" / profile / str(contract["pythonRelativePath"])
        )
        if profile == "ultra" and not configured_runtime:
            pyvenv = runtime_python.parent.parent / "pyvenv.cfg"
            high_python = runtime_root / "server-runtimes" / "high" / str(runtime_recipe("high")["pythonRelativePath"])
            if pyvenv.is_file() and high_python.is_file():
                lines = pyvenv.read_text(encoding="utf-8").splitlines()
                home = f"home = {high_python.parent}"
                replaced = False
                for index, line in enumerate(lines):
                    if line.strip().casefold().startswith("home ="):
                        lines[index] = home
                        replaced = True
                if not replaced:
                    lines.insert(0, home)
                temporary = pyvenv.with_suffix(".cfg.tmp")
                temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
                temporary.replace(pyvenv)
        command = (str(runtime_python), "-m", "worker.cuda_matting")
    else:
        contract = runtime_recipe("high")
        runtime_python = runtime_root / "server-runtimes" / "high" / str(contract["pythonRelativePath"])
        command = (str(runtime_python), "-m", "worker.cuda_matting")
    return ServerWorkerSettings(
        runtime_root=runtime_root,
        cuda_matting_exchange_root=data_root / "worker-exchange",
        cuda_matting_worker_command=command,
        model_configuration_path=configuration_path,
        runtime_profile=profile,
        selected_gpu_uuid=selected_gpu_uuid,
        memory_mode=memory_mode,
    )


class ProcessingCancelled(RuntimeError):
    pass


class RemoteProcessingError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool,
        detail: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.detail = detail or {}


class RemoteJobProcessor(Protocol):
    def warmup(self) -> dict[str, Any]: ...

    def process(
        self,
        job_id: str,
        submission: RemoteJobSubmission,
        input_archive: Path,
        job_root: Path,
        progress: Progress,
        check_control: Control,
    ) -> tuple[Path, dict[str, Any]]: ...

    def restart(self) -> None: ...

    def close(self) -> None: ...


class CudaMattingRemoteProcessor:
    """Adapter from the v1 HTTP job contract to the isolated CUDA actor."""

    def __init__(self, data_root: Path, runtime_root: Path | None = None):
        root = data_root.resolve(strict=False)
        self._data_root = root
        self._runtime_root = (
            runtime_root or Path(__file__).resolve().parents[1]
        ).resolve(strict=False)
        self.settings = _worker_settings(
            root,
            self._runtime_root,
        )
        self.settings.cuda_matting_exchange_root.mkdir(parents=True, exist_ok=True)
        self._worker = CudaMattingWorkerClient(
            self.settings,
            generation_root=root / "worker-exchange",
        )
        self._health: dict[str, Any] = {"state": "not-started"}
        self._guard = threading.RLock()
        self._active_configuration: dict[str, Any] | None = None
        self._configuration_path: Path | None = None
        self._loaded_profile: str | None = None
        self._self_test_gpu_uuid: str | None = None

    @staticmethod
    def _unavailable_detail(
        profile: str,
        warnings: list[dict[str, Any]],
        *,
        retryable: bool,
    ) -> dict[str, Any]:
        codes = [str(item.get("code")) for item in warnings if item.get("code")]
        return {
            "reason": "profile_unavailable",
            "profile": profile,
            "warningCodes": codes or ["profile_unavailable"],
            "recommendedActions": [
                str(item.get("action")) for item in warnings if item.get("action")
            ]
            or ["在模型中心重新执行 Verify → 分档 Self-test → Partial Activate。"],
            "retryable": retryable,
        }

    def _mode_receipts(self, profile: str) -> list[dict[str, Any]]:
        configuration = self._active_configuration or {}
        profiles = configuration.get("profileExecutionReceipts") or {}
        receipt = profiles.get(profile) if isinstance(profiles, dict) else None
        modes = receipt.get("executionModes") if isinstance(receipt, dict) else None
        return [dict(item) for item in modes or [] if isinstance(item, dict)]

    def memory_plan(self, profile: str, hardware: dict[str, Any] | None = None) -> dict[str, Any]:
        current = hardware or probe_cuda_hardware(
            compatible_environment_value("ROTOWEAVE_SELECTED_GPU_UUID") or None
        ).as_dict()
        selected = current.get("selectedDevice") or {}
        return memory_mode_plan(
            self._mode_receipts(profile),
            total_vram_mib=int(selected.get("vramTotalMiB") or current.get("vramTotalMiB") or 0),
            free_vram_mib=int(selected.get("vramFreeMiB") or current.get("vramFreeMiB") or 0),
            free_ram_mib=_free_ram_mib(),
        )

    def preflight_profile(self, profile: str) -> dict[str, Any]:
        if profile not in {"high", "ultra"}:
            raise ValueError("Profile must be high or ultra.")
        if self._active_configuration is None:
            warnings = [{
                "code": "profile_receipt_missing",
                "severity": "warning",
                "scope": "profile",
                "profile": profile,
                "message": "活动配置或分档自检回执不存在。",
                "action": "在模型中心重新执行 Verify → 分档 Self-test → Partial Activate。",
            }]
            raise RemoteProcessingError(
                "model_unavailable",
                f"{profile.upper()} has no active self-test receipt.",
                retryable=False,
                detail=self._unavailable_detail(profile, warnings, retryable=False),
            )
        profile_receipts = self._active_configuration.get("profileExecutionReceipts") or {}
        profile_receipt = profile_receipts.get(profile) if isinstance(profile_receipts, dict) else None
        bound_uuid = (
            str(profile_receipt.get("gpuUuid") or "")
            if isinstance(profile_receipt, dict)
            else ""
        ) or None
        hardware = probe_cuda_hardware(bound_uuid).as_dict()
        warnings = list(hardware.get("warnings") or [])
        if not hardware.get("available"):
            raise RemoteProcessingError(
                "model_unavailable",
                "No NVIDIA CUDA device is currently available.",
                retryable=False,
                detail=self._unavailable_detail(profile, warnings, retryable=False),
            )
        if bound_uuid and hardware.get("gpuUuid") != bound_uuid:
            warnings.append({
                "code": "profile_receipt_missing",
                "severity": "warning",
                "scope": "profile",
                "profile": profile,
                "message": "自检绑定的 GPU UUID 当前不可用。",
                "action": "恢复目标 GPU 或重新执行 Profile 自检。",
            })
            raise RemoteProcessingError(
                "model_unavailable",
                f"{profile.upper()} is bound to an unavailable GPU UUID.",
                retryable=False,
                detail=self._unavailable_detail(profile, warnings, retryable=False),
            )
        plan = self.memory_plan(profile, hardware)
        if plan.get("state") != "ready":
            warnings.append({
                "code": "low_vram",
                "severity": "warning",
                "scope": "profile",
                "profile": profile,
                "message": "当前空闲显存或系统内存不足以满足任何已自检模式。",
                "action": "释放临时显存/RAM 后重试；安装与服务启动不受影响。",
            })
            raise RemoteProcessingError(
                "model_unavailable",
                f"{profile.upper()} has no memory mode that fits current resources.",
                retryable=True,
                detail=self._unavailable_detail(profile, warnings, retryable=True),
            )
        return {"profile": profile, "hardware": hardware, "memoryPlan": plan, "warnings": warnings}

    @staticmethod
    def _runtime_identity(profile: str) -> dict[str, Any]:
        return runtime_recipe(profile)

    def _source_directory(self, profile: str, name: str) -> Path:
        env_name = f"ROTOWEAVE_{profile.upper()}_{name.upper()}_SOURCE"
        configured = compatible_environment_value(env_name)
        if configured:
            return Path(configured).expanduser().resolve(strict=False)
        fixed = self._runtime_root / "server-runtimes" / profile / "sources" / {
            "sam2": "SAM2Matting",
            "corridor": "CorridorKey",
            "vitmatte": "ViTMatte",
            "sam3": "SAM3",
        }[name]
        return fixed.resolve(strict=False)

    def inspect_model_candidate(
        self,
        role: str,
        path: Path,
        cancel: threading.Event,
    ) -> dict[str, Any]:
        profile = "ultra" if role == "ultra_alpha" else "high"
        settings = _worker_settings(self._data_root, self._runtime_root, profile=profile)
        python = Path(settings.cuda_matting_worker_command[0])
        if not python.is_file():
            raise RuntimeError(f"{profile.upper()} 固定产品运行时不可用。")
        environment = {
            **os.environ,
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
        }
        existing = environment.get("PYTHONPATH", "")
        environment["PYTHONPATH"] = os.pathsep.join(
            item
            for item in (str(self._runtime_root), str(contracts_root()), existing)
            if item
        )
        process = subprocess.Popen(
            [
                str(python),
                "-m",
                "worker.cuda_matting.checkpoint_inspector",
                "--role",
                role,
                "--path",
                str(path),
            ],
            cwd=self._runtime_root,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        try:
            while True:
                try:
                    # communicate() drains both pipes while the fixed runtime
                    # is still producing a potentially large tensor-key
                    # observation. Retrying after TimeoutExpired is safe and
                    # avoids stdout backpressure deadlocking the verifier.
                    stdout, stderr = process.communicate(timeout=0.2)
                    break
                except subprocess.TimeoutExpired:
                    if cancel.is_set():
                        process.kill()
                        process.communicate(timeout=10)
                        raise ProcessingCancelled("模型结构验证已取消。")
        except BaseException:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=10)
            raise
        line = next((item for item in reversed(stdout.splitlines()) if item.strip()), "")
        try:
            result = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RuntimeError((stderr or stdout or "结构验证器没有返回有效回执").strip()[-2000:]) from exc
        if process.returncode != 0 or result.get("state") != "passed":
            raise RuntimeError(str(result.get("error") or stderr or "模型结构验证失败"))
        return dict(result)

    def _write_configuration(self, payload: dict[str, Any], profile: str) -> Path:
        digest = str(payload.get("configurationDigest") or "")
        if not digest:
            raise ValueError("Model configuration has no digest.")
        profile_digest = str(
            (payload.get("profileConfigurationDigests") or {}).get(profile) or digest
        )
        target_root = self._data_root / "model-configurations"
        target_root.mkdir(parents=True, exist_ok=True)
        target = target_root / f"{profile_digest}-{profile}.json"
        document = {
            "schemaVersion": int(payload.get("schemaVersion") or 1),
            "configurationDigest": profile_digest,
            "recipeId": payload.get("recipeId") or MODEL_RECIPE_ID,
            "recipeDigest": payload.get("recipeDigest") or RECIPE_DIGEST,
            "profile": profile,
            "profileConfigurationDigest": profile_digest,
            "compatibilityPolicyDigest": payload.get("compatibilityPolicyDigest"),
            "runtime": self._runtime_identity(profile),
            "assets": payload.get("assets") or {},
            "selfTestReceipts": payload.get("selfTestReceipts") or {},
            "profileExecutionReceipts": payload.get("profileExecutionReceipts") or {},
            "sources": {
                name: str(self._source_directory(profile, name))
                for name in ("sam2", "corridor", "vitmatte", "sam3")
            },
        }
        from contracts.integrity import atomic_write_json

        atomic_write_json(target, document)
        return target

    def _replace_worker(
        self,
        profile: str,
        configuration_path: Path,
        selected_gpu_uuid: str | None = None,
        memory_mode: str | None = None,
    ) -> None:
        # Terminate and wait for the previous process before constructing the
        # next client. This is the single-GPU-worker supervisor invariant.
        self._worker.terminate()
        profile_receipts = (self._active_configuration or {}).get("profileExecutionReceipts") or {}
        profile_receipt = profile_receipts.get(profile) if isinstance(profile_receipts, dict) else None
        selected_gpu_uuid = selected_gpu_uuid or (
            (
                str(profile_receipt.get("gpuUuid") or "")
                if isinstance(profile_receipt, dict)
                else ""
            )
            or None
        )
        self.settings = _worker_settings(
            self._data_root,
            self._runtime_root,
            profile=profile,
            configuration_path=configuration_path,
            selected_gpu_uuid=selected_gpu_uuid,
            memory_mode=memory_mode,
        )
        self._worker = CudaMattingWorkerClient(
            self.settings,
            generation_root=self._data_root / "worker-exchange",
        )
        self._loaded_profile = profile
        self._configuration_path = configuration_path
        self._health = {"state": "restarting", "profile": profile}

    def configure_configuration(self, payload: dict[str, Any], profile: str = "high") -> None:
        if profile not in {"high", "ultra"}:
            raise ValueError("Profile must be high or ultra.")
        with self._guard:
            if profile == "high":
                self._self_test_gpu_uuid = None
            path = self._write_configuration(payload, profile)
            self._active_configuration = dict(payload)
            self._replace_worker(profile, path)

    def ensure_profile(self, profile: str, progress: Progress | None = None) -> dict[str, Any]:
        with self._guard:
            if self._active_configuration is None:
                return self.warmup()
            if self._loaded_profile != profile:
                if progress:
                    progress(0.02, "runtime_switch", f"Switching to {profile.upper()} Runtime.")
                path = self._write_configuration(self._active_configuration, profile)
                self._replace_worker(profile, path)
            if progress:
                progress(0.03, "runtime_warmup", f"Warming {profile.upper()} Runtime.")
            return self.warmup()

    def self_test_profile(
        self,
        profile: str,
        payload: dict[str, Any],
        cancel: threading.Event,
    ) -> dict[str, Any]:
        with self._guard:
            path = self._write_configuration(payload, profile)
            try:
                def check_cancel() -> None:
                    if cancel.is_set():
                        raise ProcessingCancelled("Profile self-test was cancelled.")

                mode_results: list[dict[str, Any]] = []
                identity: dict[str, Any] | None = None
                probe = probe_cuda_hardware()
                device_uuids = (
                    [self._self_test_gpu_uuid]
                    if self._self_test_gpu_uuid
                    else ([item.uuid for item in probe.devices] or [None])
                )
                for device_uuid in device_uuids:
                    device_results: list[dict[str, Any]] = []
                    for mode in EXECUTION_MODES:
                        check_cancel()
                        self._replace_worker(profile, path, device_uuid, mode)
                        try:
                            receipt = self._worker.request(
                                "self-test",
                                {"memoryMode": mode},
                                timeout=48 * 3600.0 if mode in {"constrained", "minimal"} else 6 * 3600.0,
                                check_control=check_cancel,
                            )
                            identity = identity or receipt
                            device_results.append({**receipt, "state": "passed", "mode": mode})
                        except Exception as exc:
                            device_results.append({
                                "state": "failed",
                                "mode": mode,
                                "deviceUuid": device_uuid,
                                "error": str(exc),
                            })
                        finally:
                            self._worker.terminate()
                            self._loaded_profile = None
                    mode_results.extend(device_results)
                    if any(item.get("state") == "passed" for item in device_results):
                        mode_results = device_results
                        break
                passed = [item for item in mode_results if item.get("state") == "passed"]
                if not passed or identity is None:
                    return {
                        "state": "failed",
                        "profile": profile,
                        "configurationDigest": payload.get("configurationDigest"),
                        "runtimeDigest": self._runtime_identity(profile).get("digest"),
                        "executionModes": mode_results,
                        "error": "No execution mode passed Profile self-test.",
                    }
                result = {
                    **identity,
                    "state": "passed",
                    "executionModes": mode_results,
                }
                self._self_test_gpu_uuid = str(result.get("gpuUuid") or "") or self._self_test_gpu_uuid
                self._health = {"state": "self-test-passed", "profile": profile, "detail": result}
                return result
            finally:
                # The next Profile must start only after CUDA/process release.
                self._worker.terminate()
                self._loaded_profile = None
                self._health = {"state": "released", "profile": profile}

    def warmup(self) -> dict[str, Any]:
        with self._guard:
            try:
                health = self._worker.health()
                hardware = health.get("hardware") or {}
                pack = health.get("modelConfiguration") or {}
                if hardware.get("cudaSmokePassed") is not True:
                    raise RemoteProcessingError(
                        "model_unavailable",
                        "The selected NVIDIA device did not pass the CUDA smoke test.",
                        retryable=False,
                        detail=self._unavailable_detail(
                            str(pack.get("profile") or self._loaded_profile or "high"),
                            hardware.get("warnings") or [],
                            retryable=False,
                        ),
                    )
                if pack.get("state") != "ready":
                    raise RemoteProcessingError(
                        "model_unavailable",
                        str(pack.get("fallbackReason") or "The model configuration is unavailable."),
                        retryable=False,
                    )
                profile = str(pack.get("profile") or self._loaded_profile or "high")
                plan = self.memory_plan(profile, hardware)
                warnings = list(health.get("warnings") or hardware.get("warnings") or [])
                self._health = {
                    **health,
                    "state": "ready-with-warnings" if warnings else "ready",
                    "memoryPlan": plan,
                    "profileModes": self._mode_receipts(profile),
                    "warnings": warnings,
                }
            except RemoteProcessingError:
                raise
            except Exception as exc:
                self._health = {"state": "error", "error": str(exc)}
                raise RemoteProcessingError(
                    "model_unavailable", str(exc), retryable=True
                ) from exc
            return dict(self._health)

    def health_snapshot(self) -> dict[str, Any]:
        return dict(self._health)

    def restart(self) -> None:
        with self._guard:
            self._worker.terminate()
            self._health = {"state": "restarting"}

    def close(self) -> None:
        with self._guard:
            self._worker.close()

    @staticmethod
    def _linearize_input(source: Path, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.suffix.lower() == ".exr":
            shutil.copyfile(source, target)
            return
        image = cv2.imread(str(source), cv2.IMREAD_COLOR)
        if image is None:
            raise RemoteProcessingError(
                "integrity_failed",
                f"Input frame cannot be decoded: {source.name}",
                retryable=False,
            )
        write_linear_exr(target, bgr_u8_to_linear_rgb(image, transfer="srgb"))

    def process(
        self,
        job_id: str,
        submission: RemoteJobSubmission,
        input_archive: Path,
        job_root: Path,
        progress: Progress,
        check_control: Control,
    ) -> tuple[Path, dict[str, Any]]:
        check_control()
        preflight = self.preflight_profile(submission.quality.value)
        health = self.ensure_profile(submission.quality.value, progress)
        progress(0.05, "preflight", f"{submission.quality.value.upper()} Runtime and GPU are ready.")
        source_root = job_root / "source"
        linear_root = job_root / "linear"
        source_root.mkdir(parents=True, exist_ok=True)
        frames: list[dict[str, Any]] = []
        with zipfile.ZipFile(input_archive, "r") as archive:
            for frame in submission.frames:
                check_control()
                raw = archive.read(frame.archivePath)
                if hashlib.sha256(raw).hexdigest() != frame.sha256:
                    raise RemoteProcessingError(
                        "integrity_failed",
                        f"Input frame hash changed: {frame.ordinal}",
                        retryable=False,
                    )
                source = source_root / Path(frame.archivePath).name
                source.write_bytes(raw)
                linear = linear_root / f"{frame.ordinal:06d}.exr"
                self._linearize_input(source, linear)
                frames.append(
                    {
                        "id": frame.frameId,
                        "frame_index": frame.ordinal,
                        "linear_source_path": str(linear),
                        "width": frame.width,
                        "height": frame.height,
                        "time_us": frame.ptsUs,
                        "source_timeline_ordinal": frame.ordinal,
                    }
                )
                progress(
                    0.05 + 0.10 * (frame.ordinal + 1) / submission.frameCount,
                    "staging",
                    None,
                )
        exchange_parent = self._worker.generation_root
        exchange_parent.mkdir(parents=True, exist_ok=True)
        exchange_root = exchange_parent / job_id
        if exchange_root.exists():
            shutil.rmtree(exchange_root)
        constraints_hash = canonical_sha256(submission.settings)
        route = (
            "emissive_vfx"
            if str(submission.settings.get("material_type") or "character") == "effect"
            else "chroma_character"
        )
        imported_root = job_root / "candidate"
        try:
            input_manifest = stage_linear_inputs(
                exchange_root,
                frames,
                route=route,
                constraints_hash=constraints_hash,
                source_sha256=submission.materialSha256,
            )
            progress(0.18, "worker", f"Running {submission.quality.value} on the selected CUDA device.")
            selected_mode = str((preflight.get("memoryPlan") or {}).get("selectedMode") or "")
            start_index = EXECUTION_MODES.index(selected_mode)
            attempted_modes: list[str] = []
            worker_result: dict[str, Any] | None = None
            last_oom: Exception | None = None
            for mode in EXECUTION_MODES[start_index:]:
                receipt = next(
                    (item for item in self._mode_receipts(submission.quality.value) if item.get("mode") == mode and item.get("state") == "passed"),
                    None,
                )
                if receipt is None:
                    continue
                check_control()
                attempted_modes.append(mode)
                candidate_root = exchange_root / "candidate"
                shutil.rmtree(candidate_root, ignore_errors=True)
                try:
                    if mode != "full":
                        if self._configuration_path is None:
                            raise RemoteProcessingError(
                                "model_unavailable",
                                "The active model configuration path is unavailable.",
                                retryable=False,
                            )
                        self._replace_worker(
                            submission.quality.value,
                            self._configuration_path,
                            memory_mode=mode,
                        )
                        self.warmup()
                    worker_result = self._worker.request(
                        "run",
                        {
                            "route": route,
                            "inputManifest": str(input_manifest),
                            "outputDirectory": str(candidate_root),
                            "profile": submission.quality.value,
                            "constraintsHash": constraints_hash,
                            "maxRoiRefinements": 1,
                            "memoryMode": mode,
                        },
                        timeout=48 * 3600.0 if mode in {"constrained", "minimal"} else 6 * 3600.0,
                        check_control=check_control,
                    )
                    break
                except ProcessingCancelled:
                    raise
                except WorkerProtocolError as exc:
                    lowered = str(exc).casefold()
                    if "cancel" in lowered:
                        raise ProcessingCancelled(str(exc)) from exc
                    if not any(marker in lowered for marker in ("out of memory", "cuda oom", "vram")):
                        raise RemoteProcessingError(
                            "model_unavailable", str(exc), retryable=True
                        ) from exc
                    last_oom = exc
                    self.restart()
                    self.ensure_profile(submission.quality.value, progress)
            if worker_result is None:
                raise RemoteProcessingError(
                    "gpu_out_of_memory",
                    str(last_oom or "All measured memory modes exhausted."),
                    retryable=False,
                    detail={
                        "reason": "profile_unavailable",
                        "profile": submission.quality.value,
                        "warningCodes": ["memory_retry_exhausted"],
                        "recommendedActions": ["释放显存或系统内存后重新提交任务。"],
                        "attemptedModes": attempted_modes,
                    },
                )
            execution = worker_result.setdefault("execution", {})
            execution["attemptCount"] = len(attempted_modes)
            execution["attemptedModes"] = attempted_modes
            check_control()
            imported = import_worker_candidate(
                worker_result,
                exchange_root=exchange_root / "candidate",
                workspace_root=imported_root,
            )
        finally:
            shutil.rmtree(exchange_root, ignore_errors=True)
        progress(0.90, "packaging", "Packaging immutable RGBA frame result.")
        candidate_by_id = {
            str(item.get("frameId") or ""): item
            for item in imported.get("frames") or []
            if isinstance(item, dict)
        }
        members: dict[str, bytes] = {}
        output_frames: list[RemoteOutputFrame] = []
        for source in submission.frames:
            candidate = candidate_by_id.get(source.frameId)
            if candidate is None:
                raise RemoteProcessingError(
                    "integrity_failed",
                    "Worker result does not cover every input frame.",
                    retryable=False,
                )
            rgba_source = Path(str(candidate.get("compatibilityRgbaPath") or ""))
            image = cv2.imread(str(rgba_source), cv2.IMREAD_UNCHANGED)
            if image is None or image.shape != (source.height, source.width, 4):
                raise RemoteProcessingError(
                    "integrity_failed",
                    f"Worker RGBA output is invalid: {source.ordinal}",
                    retryable=False,
                )
            rgba_path = f"rgba/{source.ordinal:06d}.png"
            rgba_bytes = rgba_source.read_bytes()
            members[rgba_path] = rgba_bytes
            emission_source = Path(str(candidate.get("deliveryEmissionPath") or ""))
            emission_path: str | None = None
            emission_sha256: str | None = None
            if emission_source.is_file():
                emission_path = f"emission/{source.ordinal:06d}.png"
                emission_bytes = emission_source.read_bytes()
                members[emission_path] = emission_bytes
                emission_sha256 = hashlib.sha256(emission_bytes).hexdigest()
            output_frames.append(
                RemoteOutputFrame(
                    sourceFrameId=source.frameId,
                    ordinal=source.ordinal,
                    width=source.width,
                    height=source.height,
                    rgbaPath=rgba_path,
                    rgbaSha256=hashlib.sha256(rgba_bytes).hexdigest(),
                    emissionPath=emission_path,
                    emissionSha256=emission_sha256,
                )
            )
        configuration = imported.get("modelConfiguration") or {}
        model = {
            "worker": "cuda-matting-worker",
            "modelConfiguration": configuration,
            "provenance": imported.get("provenance") or {},
            "hardware": health.get("hardware") or {},
            "precision": health.get("precision"),
            "execution": imported.get("execution") or {},
        }
        if configuration:
            # Keep the nested object for consumers introduced during the V4
            # transition, while promoting the stable traceability fields onto
            # the existing protocol-v1 `model` object.  Paths are deliberately
            # excluded: a remote result describes immutable identities, never
            # the server's local model-library layout.
            model.update(
                {
                    "configurationDigest": configuration.get("configurationDigest"),
                    "recipe": {
                        "id": configuration.get("recipeId"),
                        "digest": configuration.get("recipeDigest"),
                    },
                    "qualityProfile": configuration.get("profile"),
                    "runtimeDigest": configuration.get("runtimeDigest"),
                    "models": configuration.get("models") or [],
                    "selfTestReceiptDigest": configuration.get(
                        "selfTestReceiptDigest"
                    ),
                }
            )
        manifest = RemoteResultManifest(
            protocolVersion=1,
            jobId=job_id,
            materialId=submission.materialId,
            materialSha256=submission.materialSha256,
            quality=submission.quality,
            frameCount=len(output_frames),
            frameMappingSha256=canonical_sha256(
                [item.model_dump(mode="json") for item in output_frames]
            ),
            archiveSha256="0" * 64,
            frames=output_frames,
            model=model,
            settings=submission.settings,
        )
        content_hash = result_payload_sha256(manifest, members.__getitem__)
        manifest = manifest.model_copy(update={"archiveSha256": content_hash})
        result_path = job_root / "result.zip"
        part = result_path.with_suffix(".zip.part")
        with zipfile.ZipFile(part, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
            archive.writestr("result.json", manifest.model_dump_json())
            for name in sorted(members):
                archive.writestr(name, members[name])
        part.replace(result_path)
        progress(0.99, "ready", "Result archive verified and ready.")
        return result_path, {
            "transportSha256": sha256_file(result_path),
            "model": model,
        }
