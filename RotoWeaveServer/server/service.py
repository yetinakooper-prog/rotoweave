from __future__ import annotations

import hashlib
import re
import shutil
import threading
import time
import uuid
import zipfile
from pathlib import Path
from typing import Any

from contracts.integrity import canonical_sha256, sha256_file
from contracts.remote_protocol import (
    RemoteError,
    RemoteErrorCode,
    RemoteJobState,
    RemoteJobStatus,
    RemoteJobSubmission,
)
from .config import RemoteServerSettings
from .model_center import ModelCenter
from .processor import (
    CudaMattingRemoteProcessor,
    ProcessingCancelled,
    RemoteJobProcessor,
    RemoteProcessingError,
)
from .repository import IdempotencyConflict, RemoteQueueRepository
from .startup import StartupTracker


TERMINAL_STATES = {"completed", "failed", "cancelled"}


class RemoteService:
    def __init__(
        self,
        settings: RemoteServerSettings,
        *,
        processor: RemoteJobProcessor | None = None,
    ) -> None:
        self.settings = settings
        self.startup = StartupTracker()
        self.startup.update("configuration", "passed", "服务器配置已读取")
        settings.ensure_directories()
        self.repository = RemoteQueueRepository(settings.database_path)
        self.startup.update(
            "storage",
            "passed",
            "运行目录与队列数据库已就绪",
        )
        self.startup.update("network_boundary", "passed", "可信私网 HTTP 与 localhost 管理边界已配置")
        self.startup.update("model_catalog", "passed", "独立模型目录已加载")
        self.processor = processor or CudaMattingRemoteProcessor(settings.data_root)
        self.model_center = ModelCenter(
            self.repository,
            Path(__file__).resolve().parents[1],
        )
        tester = getattr(self.processor, "self_test_profile", None)
        if callable(tester):
            self.model_center.set_profile_tester(tester)
        inspector = getattr(self.processor, "inspect_model_candidate", None)
        if callable(inspector):
            self.model_center.set_asset_inspector(inspector)
        self.model_center.set_self_test_lifecycle(
            self._begin_model_self_test,
            self._end_model_self_test,
        )
        self.model_center.set_activator(self._activate_configuration)
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._health: dict[str, Any] = {"state": "starting"}
        self._last_cleanup: dict[str, Any] = {"removed": 0, "at": None}
        self._switch_thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        recovered = self.repository.recover()
        if recovered:
            self.repository.log(None, "warning", "service.recovered", {"jobIds": recovered})
        self._stop.clear()
        self.startup.update("model_verification", "running", "正在验证活动独立模型配置")
        self._thread = threading.Thread(target=self._run, name="remote-matting-queue", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=10)
        self.processor.close()

    @staticmethod
    def _verify_archive(path: Path, submission: RemoteJobSubmission) -> None:
        if sha256_file(path) != submission.archiveSha256:
            raise RemoteProcessingError(
                "integrity_failed", "Input archive SHA-256 does not match submission.", retryable=False
            )
        try:
            with zipfile.ZipFile(path, "r") as archive:
                infos = [item for item in archive.infolist() if not item.is_dir()]
                names = [item.filename.replace("\\", "/") for item in infos]
                expected = [item.archivePath for item in submission.frames]
                if names != expected or len(names) != len(set(names)):
                    raise RemoteProcessingError(
                        "integrity_failed", "Input archive members do not match frame manifest.", retryable=False
                    )
                for frame in submission.frames:
                    if hashlib.sha256(archive.read(frame.archivePath)).hexdigest() != frame.sha256:
                        raise RemoteProcessingError(
                            "integrity_failed", f"Input frame hash failed: {frame.ordinal}", retryable=False
                        )
        except zipfile.BadZipFile as exc:
            raise RemoteProcessingError(
                "integrity_failed", "Input archive is not a valid ZIP.", retryable=False
            ) from exc

    def submit(
        self,
        submission: RemoteJobSubmission,
        archive_path: Path,
        idempotency_key: str,
    ) -> tuple[dict[str, Any], bool]:
        control = self.repository.queue_control()
        if control["maintenance"] or control["mode"] == "draining":
            raise RemoteProcessingError(
                "model_unavailable",
                "The server is draining or in maintenance for a model switch.",
                retryable=True,
            )
        if not re.fullmatch(r"[0-9a-f]{64}", idempotency_key):
            raise RemoteProcessingError(
                "invalid_request", "Idempotency-Key must be a lowercase SHA-256.", retryable=False
            )
        request_sha = canonical_sha256(submission.model_dump(mode="json"))
        existing = self.repository.get_by_idempotency(idempotency_key)
        if existing is not None:
            if existing["request_sha256"] != request_sha:
                raise IdempotencyConflict("Idempotency-Key was reused for another request.")
            archive_path.unlink(missing_ok=True)
            return existing, False
        active_configuration = self.model_center.active_configuration()
        preflight = getattr(self.processor, "preflight_profile", None)
        if active_configuration is not None:
            digest = str(active_configuration["configurationDigest"])
            profile_state = self.model_center.profile_states_for_configuration(digest).get(
                submission.quality.value, {}
            )
            if profile_state.get("state") != "ready":
                raise RemoteProcessingError(
                    "model_unavailable",
                    "; ".join(profile_state.get("blockers") or ["Requested Profile is not READY."]),
                    retryable=False,
                    detail={
                        "reason": "profile_unavailable",
                        "profile": submission.quality.value,
                        "warningCodes": ["profile_unavailable"],
                        "recommendedActions": profile_state.get("blockers") or [
                            "在模型中心重新执行分档自检。"
                        ],
                    },
                )
        elif callable(preflight):
            raise RemoteProcessingError(
                "model_unavailable",
                "The requested Profile has no active configuration.",
                retryable=False,
                detail={
                    "reason": "profile_unavailable",
                    "profile": submission.quality.value,
                    "warningCodes": ["profile_receipt_missing"],
                    "recommendedActions": [
                        "在模型中心执行 Verify → 分档 Self-test → Partial Activate。"
                    ],
                },
            )
        if callable(preflight):
            preflight_result = preflight(submission.quality.value)
            plan = preflight_result.get("memoryPlan") or {}
            self.repository.log(
                None,
                "info",
                "memory.plan_selected",
                {
                    "profile": submission.quality.value,
                    "memoryMode": plan.get("selectedMode"),
                    "headroomMiB": plan.get("headroomMiB"),
                },
                component="worker",
            )
        self._verify_archive(archive_path, submission)
        job_id = self.repository.new_job_id()
        job_root = self.settings.jobs_root / job_id
        job_root.mkdir(parents=True, exist_ok=False)
        target = job_root / "input.zip"
        archive_path.replace(target)
        try:
            job, created = self.repository.enqueue(
                job_id=job_id,
                idempotency_key=idempotency_key,
                request_sha256=request_sha,
                submission=submission.model_dump(mode="json"),
                input_path=str(target),
                model_configuration_digest=(
                    str(active_configuration["configurationDigest"])
                    if active_configuration
                    else None
                ),
                quality_profile=submission.quality.value,
            )
        except Exception:
            shutil.rmtree(job_root, ignore_errors=True)
            raise
        if not created:
            shutil.rmtree(job_root, ignore_errors=True)
        self._wake.set()
        return job, created

    def _run(self) -> None:
        try:
            active_configuration = self.model_center.active_configuration()
            configure = getattr(self.processor, "configure_configuration", None)
            if active_configuration is not None and callable(configure):
                states = self.model_center.profile_states_for_configuration(
                    str(active_configuration["configurationDigest"])
                )
                resident = next(
                    (profile for profile in ("high", "ultra") if states.get(profile, {}).get("state") == "ready"),
                    None,
                )
                if resident:
                    configure(active_configuration, resident)
            self.startup.update("runtime", "running", "正在检测通用 NVIDIA CUDA 与隔离运行时")
            preflight = getattr(self.processor, "preflight_profile", None)
            if active_configuration is None and callable(preflight):
                raise RemoteProcessingError(
                    "model_unavailable",
                    "No active Profile configuration is available.",
                    retryable=False,
                )
            self._health = {"state": "warming", "detail": self.processor.warmup()}
            detail = self._health["detail"]
            pack = detail.get("modelConfiguration") if isinstance(detail, dict) else None
            hardware = detail.get("hardware") if isinstance(detail, dict) else {}
            selected = (hardware or {}).get("selectedDevice") or {}
            self.repository.log(
                None,
                "info",
                "hardware.probed",
                {
                    "deviceUuid": selected.get("uuid"),
                    "gpuName": selected.get("gpuName"),
                    "driverVersion": selected.get("driverVersion"),
                    "computeCapability": selected.get("computeCapability"),
                    "vramTotalMiB": selected.get("vramTotalMiB"),
                },
                component="worker",
            )
            for warning in (hardware or {}).get("warnings") or []:
                self.repository.log(
                    None,
                    "warning",
                    "hardware.warning",
                    {key: warning.get(key) for key in ("code", "severity", "scope", "message", "action", "profile")},
                    component="worker",
                )
            file_total = int((pack or {}).get("verifiedFileCount") or 0)
            self.startup.update("model_verification", "passed", "活动独立模型配置与文件校验通过", files_verified=file_total, files_total=file_total)
            self.startup.update("runtime", "passed", "GPU 与 Worker 运行时可用")
            self.startup.update("self_test", "passed", "模型 self-test 与预热通过")
            self._health = {"state": "ready", "detail": self._health["detail"]}
            self.startup.update("ready", "passed", "Worker 与远程 API 已就绪")
        except Exception as exc:
            warning = {
                "code": "profile_unavailable",
                "severity": "warning",
                "scope": "profile",
                "message": str(exc),
                "action": "完成模型验证与分档自检后重新激活；API 与管理链保持可用。",
            }
            self._health = {
                "state": "profile-unavailable",
                "error": str(exc),
                "warnings": [warning],
            }
            self.startup.update("model_verification", "warning", "Profile 暂不可用", error=str(exc))
            self.startup.update("runtime", "warning", "CUDA Worker 暂不可运行", error=str(exc))
            self.startup.update("self_test", "warning", "无可运行 Profile", error=str(exc))
            self.startup.update("ready", "warning", "API、存储和管理链已就绪；Profile 暂不可用")
            self.repository.log(None, "warning", "profile.unavailable", {"message": str(exc)}, component="worker")
        while not self._stop.is_set():
            job = self.repository.claim_next()
            if job is None:
                self.cleanup_expired()
                self._wake.wait(0.25)
                self._wake.clear()
                continue
            self._execute(job)

    def _execute(self, job: dict[str, Any]) -> None:
        job_id = str(job["id"])
        submission = RemoteJobSubmission.model_validate(job["submission"])
        job_root = self.settings.jobs_root / job_id

        def check_control() -> None:
            current = self.repository.get(job_id)
            if self._stop.is_set() or current is None or current["state"] == "cancelled":
                raise ProcessingCancelled("Remote job was cancelled.")

        def report(progress: float, stage: str, message: str | None) -> None:
            check_control()
            self.repository.progress(job_id, progress, stage, message)

        try:
            frozen_configuration = str(job.get("model_configuration_digest") or "")
            if frozen_configuration:
                active = self.model_center.active_configuration()
                if active is None or str(active.get("configurationDigest")) != frozen_configuration:
                    raise RemoteProcessingError(
                        "model_unavailable",
                        "The job's frozen model configuration is no longer active.",
                        retryable=True,
                    )
            result_path, metadata = self.processor.process(
                job_id,
                submission,
                Path(str(job["input_path"])),
                job_root,
                report,
                check_control,
            )
            execution = ((metadata.get("model") or {}).get("execution") or {})
            if int(execution.get("attemptCount") or 1) > 1:
                self.repository.log(
                    job_id,
                    "warning",
                    "memory.retry",
                    {
                        "profile": submission.quality.value,
                        "attemptCount": execution.get("attemptCount"),
                        "attemptedModes": execution.get("attemptedModes") or [],
                    },
                    component="worker",
                )
            if execution.get("cpuStages"):
                self.repository.log(
                    job_id,
                    "warning",
                    "memory.cpu_stage",
                    {
                        "profile": submission.quality.value,
                        "memoryMode": execution.get("memoryMode"),
                        "cpuStages": execution.get("cpuStages"),
                    },
                    component="worker",
                )
            snapshot = getattr(self.processor, "health_snapshot", None)
            if callable(snapshot):
                self._health = {"state": "ready", "detail": snapshot()}
            check_control()
            digest = str(metadata.get("transportSha256") or sha256_file(result_path))
            if not self.repository.complete(job_id, str(result_path), digest, self.settings.ttl_hours):
                result_path.unlink(missing_ok=True)
        except ProcessingCancelled:
            self.processor.restart()
            self.repository.cancel(job_id, self.settings.ttl_hours)
            shutil.rmtree(job_root / "candidate", ignore_errors=True)
        except RemoteProcessingError as exc:
            if exc.code == "gpu_out_of_memory" or exc.retryable:
                self.processor.restart()
                self._health = {"state": "restarting", "error": str(exc)}
            self.repository.fail(
                job_id,
                {
                    "protocolVersion": 1,
                    "code": exc.code,
                    "message": str(exc),
                    "retryable": exc.retryable,
                    "detail": exc.detail or None,
                },
                self.settings.ttl_hours,
            )
        except Exception as exc:
            self.processor.restart()
            self._health = {"state": "restarting", "error": str(exc)}
            self.repository.fail(
                job_id,
                {
                    "protocolVersion": 1,
                    "code": "internal_error",
                    "message": str(exc),
                    "retryable": True,
                    "detail": None,
                },
                self.settings.ttl_hours,
            )

    def status(self, job_id: str) -> RemoteJobStatus | None:
        job = self.repository.get(job_id)
        if job is None:
            return None
        error = RemoteError.model_validate(job["error"]) if job.get("error") else None
        return RemoteJobStatus(
            protocolVersion=1,
            jobId=job_id,
            state=RemoteJobState(job["state"]),
            progress=float(job["progress"]),
            stage=job.get("stage"),
            error=error,
        )

    def cancel(self, job_id: str) -> RemoteJobStatus | None:
        job = self.repository.cancel(job_id, self.settings.ttl_hours)
        self._wake.set()
        return self.status(job_id) if job is not None else None

    def queue_snapshot(self, *, state: str | None = None, limit: int = 100, offset: int = 0) -> dict[str, Any]:
        return self.repository.list_jobs(state=state, limit=limit, offset=offset)

    def pause_queue(self, paused: bool, expected_revision: int | None = None) -> dict[str, Any]:
        result = self.repository.set_queue_control(paused=paused, expected_revision=expected_revision)
        self._wake.set()
        return result

    def reorder_queue(self, job_ids: list[str], expected_revision: int) -> dict[str, Any]:
        result = self.repository.reorder(job_ids, expected_revision)
        self._wake.set()
        return result

    def retry_job(self, job_id: str) -> dict[str, Any]:
        source = self.repository.get(job_id)
        if source is None:
            raise KeyError(job_id)
        source_input = Path(str(source["input_path"]))
        if not source_input.is_file():
            raise RemoteProcessingError("integrity_failed", "Original input archive is no longer available.", retryable=False)
        new_id = self.repository.new_job_id()
        target_root = self.settings.jobs_root / new_id
        target_root.mkdir(parents=True, exist_ok=False)
        target = target_root / "input.zip"
        shutil.copy2(source_input, target)
        key = hashlib.sha256(f"{job_id}:retry:{uuid.uuid4().hex}".encode("utf-8")).hexdigest()
        try:
            job = self.repository.retry(job_id, new_id, key, str(target))
        except Exception:
            shutil.rmtree(target_root, ignore_errors=True)
            raise
        self._wake.set()
        return job

    def delete_terminal_job(self, job_id: str) -> bool:
        removed = self.repository.delete(job_id, terminal_only=True)
        if removed:
            root = (self.settings.jobs_root / job_id).resolve(strict=False)
            root.relative_to(self.settings.jobs_root.resolve())
            shutil.rmtree(root, ignore_errors=True)
        return removed

    def emergency_stop(self) -> dict[str, Any]:
        running = self.repository.list_jobs(state="running", limit=1)["items"]
        job_id = str(running[0]["id"]) if running else None
        if job_id:
            self.repository.cancel(job_id, self.settings.ttl_hours)
        self.processor.restart()
        self._health = {"state": "restarting", "reason": "emergency-stop"}
        self.repository.log(job_id, "error", "worker.emergency_stop", {}, component="admin")
        self._wake.set()
        return {"stoppedJobId": job_id, "worker": "restarting"}

    def _activate_configuration(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Drain the queue and atomically prepare the requested dual Profile config."""

        def ready_profiles(configuration: dict[str, Any]) -> list[str]:
            receipts = configuration.get("profileExecutionReceipts")
            if not isinstance(receipts, dict):
                return []
            return [
                profile for profile in ("high", "ultra")
                if isinstance(receipts.get(profile), dict)
            ]

        previous_health = dict(self._health)
        switch_started = False
        self.repository.set_queue_control(paused=False, maintenance=False, mode="draining")
        try:
            while not self._stop.is_set():
                states = self.repository.stats()["states"]
                if not states.get("queued") and not states.get("running"):
                    break
                time.sleep(0.25)
            if self._stop.is_set():
                raise RuntimeError("Service stopped during configuration drain.")
            self.repository.set_queue_control(paused=True, maintenance=True, mode="switching")
            configure = getattr(self.processor, "configure_configuration", None)
            if not callable(configure):
                raise RuntimeError("The configured processor does not support dual Profile runtimes.")
            profiles = ready_profiles(payload)
            if not profiles:
                raise RuntimeError("No READY Profile receipt is available for activation.")
            resident = profiles[0]
            switch_started = True
            configure(payload, resident)
            self._health = {"state": "warming", "profile": resident}
            detail = self.processor.warmup()
            partial = len(profiles) == 1
            self._health = {
                "state": "ready-with-warnings" if partial else "ready",
                "profile": resident,
                "detail": detail,
                "warnings": (
                    [{
                        "code": "profile_unavailable",
                        "severity": "warning",
                        "scope": "profile",
                        "message": "仅一个 High/Ultra Profile 可运行。",
                        "action": "可继续使用 READY 档位；修复另一档后重新自检。",
                    }]
                    if partial
                    else []
                ),
            }
            file_total = int((detail.get("modelConfiguration") or {}).get("verifiedFileCount") or 0) if isinstance(detail, dict) else 0
            self.startup.update("model_verification", "passed", "活动独立模型配置与文件校验通过", files_verified=file_total, files_total=file_total)
            self.startup.update("runtime", "passed", "GPU 与固定 Worker 运行时可用")
            self.startup.update("self_test", "warning" if partial else "passed", "分档自检回执与预热通过")
            self.startup.update("ready", "warning" if partial else "passed", "Worker 与远程 API 已就绪")
            self.repository.set_queue_control(paused=False, maintenance=False, mode="normal")
            return {
                "configurationDigest": payload.get("configurationDigest"),
                "residentProfile": resident,
                "worker": "ready-with-warnings" if partial else "ready",
            }
        except Exception as exc:
            if not switch_started:
                self._health = previous_health
                self.repository.set_queue_control(paused=False, maintenance=False, mode="normal")
            else:
                self._health = {
                    "state": "maintenance",
                    "error": str(exc),
                    "reason": "model-configuration-switch-failed",
                }
                self.repository.set_queue_control(paused=True, maintenance=True, mode="maintenance")
            raise

    def _begin_model_self_test(self, cancel: threading.Event) -> None:
        self.repository.set_queue_control(paused=True, maintenance=True, mode="self-testing")
        try:
            while not self._stop.is_set():
                if cancel.is_set():
                    raise ProcessingCancelled("Profile self-test was cancelled while waiting for the GPU.")
                if not self.repository.stats()["states"].get("running"):
                    return
                time.sleep(0.25)
            raise RuntimeError("Service stopped before Profile self-test.")
        except Exception:
            self._end_model_self_test()
            raise

    def _end_model_self_test(self) -> None:
        control = self.repository.queue_control()
        if control["mode"] == "self-testing":
            self.repository.set_queue_control(paused=False, maintenance=False, mode="normal")

    def cleanup_expired(self) -> dict[str, Any]:
        expired = self.repository.expired()
        removed = 0
        for job in expired:
            job_id = str(job["id"])
            root = (self.settings.jobs_root / job_id).resolve(strict=False)
            try:
                root.relative_to(self.settings.jobs_root.resolve())
            except ValueError:
                self.repository.log(job_id, "error", "cleanup.unsafe_path", {"path": str(root)})
                continue
            shutil.rmtree(root, ignore_errors=True)
            self.repository.delete(job_id)
            removed += 1
        log_files_removed = 0
        cutoff = time.time() - self.settings.log_retention_days * 86400
        for path in self.settings.logs_root.glob("*"):
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
                log_files_removed += 1
        log_cleanup = self.repository.cleanup_logs(self.settings.log_retention_days, self.settings.log_max_rows)
        self._last_cleanup = {
            "removed": removed,
            "logRowsRemoved": log_cleanup["removed"],
            "logFilesRemoved": log_files_removed,
            "at": time.time(),
        }
        return dict(self._last_cleanup)

    def admin_status(self) -> dict[str, Any]:
        disk = shutil.disk_usage(self.settings.data_root)
        return {
            "service": "RotoWeave Remote Matting 4.0",
            "protocolVersion": 1,
            "network": self.network_status(),
            "worker": self._health,
            "queue": self.repository.stats(),
            "startup": self.startup.snapshot(),
            "modelCenter": self.model_center.snapshot(),
            "disk": {"totalBytes": disk.total, "usedBytes": disk.used, "freeBytes": disk.free},
            "ttlHours": self.settings.ttl_hours,
            "logRetentionDays": self.settings.log_retention_days,
            "cleanup": self._last_cleanup,
            "ownership": "short-lived-remote-jobs-only",
        }

    def network_status(self) -> dict[str, Any]:
        return self.settings.network_status()

    def save_network_settings(self, api_port: object) -> dict[str, Any]:
        result = self.settings.save_network_settings(api_port)
        self.repository.log(
            None,
            "info",
            "network.settings_saved",
            {
                "apiHost": result["configured_host"],
                "apiPort": result["configured_port"],
                "restartRequired": result["restart_required"],
            },
            component="host",
        )
        return result

    def connection_status(self) -> dict[str, Any]:
        """Return the authenticated client-safe capability summary."""

        startup_state = str(self.startup.snapshot().get("state") or "starting")
        worker_state = str(self._health.get("state") or "profile-unavailable")
        has_ready_profile = worker_state in {"ready", "ready-with-warnings"}
        return {
            "protocolVersion": 1,
            "service": "RotoWeave Remote Matting 4.0",
            "ready": startup_state == "ready" and has_ready_profile,
            "startupState": startup_state,
            "workerState": worker_state,
            "ownership": "short-lived-remote-jobs-only",
        }
