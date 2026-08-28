from __future__ import annotations

import asyncio
import hashlib
import json
import queue
import shutil
import threading
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable

from .birefnet import preflight_birefnet
from .basic_material_processor import process_basic_material
from .config import Settings
from .failures import build_job_failure
from .material_library import MaterialLibrary
from .remote_matting_client import (
    RemoteMattingClient,
    RemoteMattingConfig,
    prepare_remote_submission,
    publish_remote_result,
)
from .remote_protocol import RemoteJobState
from .schemas import BasicMaterialSettings, ChromaSettings
from .storage import ObjectStore, sha256_file
from .repositories.common import utc_now
from .workspace_session import WorkspaceRepositoryGateway


class JobInterrupted(RuntimeError):
    pass


class JobCancelled(RuntimeError):
    pass


class JobManager:
    def __init__(
        self,
        database: WorkspaceRepositoryGateway,
        store: ObjectStore,
        settings: Settings,
    ):
        self.database = database
        self.store = store
        self.store.bind_database(database)
        self.settings = settings
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()
        self._gpu_lock = threading.Lock()
        self._project_locks: defaultdict[str, threading.Lock] = defaultdict(threading.Lock)
        self._last_control_check: dict[str, float] = {}
        self._cancel_guard = threading.Lock()
        self._cancel_requests: set[str] = set()

    def start(self) -> None:
        if any(thread.is_alive() for thread in self._threads):
            return
        self._stop.clear()
        # A busy worker can leave a shutdown sentinel in the old queue. Reusing
        # that queue would let the next worker exit before recovered jobs run.
        self._queue = queue.Queue()
        for job_id in self.database.recover_jobs():
            self._queue.put(job_id)
        # Heavy video and AI stages run serially to avoid memory contention.
        worker_count = 1
        self._threads = [
            threading.Thread(target=self._worker, name=f"rotoweave-jobs-{index + 1}", daemon=True)
            for index in range(worker_count)
        ]
        for thread in self._threads:
            thread.start()

    def stop(self) -> None:
        self._stop.set()
        for _ in self._threads:
            self._queue.put(None)
        threads = list(self._threads)
        for thread in threads:
            thread.join(timeout=5)
        self._threads = [thread for thread in threads if thread.is_alive()]
    def submit(self, job_id: str) -> None:
        self._queue.put(job_id)

    def cancel(self, job_id: str) -> dict[str, Any] | None:
        job = self.database.cancel_job(job_id)
        if job is not None:
            with self._cancel_guard:
                self._cancel_requests.add(job_id)
            self.database.append_job_log(job_id, "warning", "用户请求取消任务。")
        return job

    def create_material_basic(
        self,
        source_id: str,
        raw_settings: dict[str, Any],
        *,
        expected_revision_id: str,
        frame_indexes: list[int],
    ) -> dict[str, Any]:
        repository = self.database.session.require_repository()
        domain = repository.workspace_domain()
        source = repository.get_material_source(source_id)
        if source is None:
            raise KeyError(source_id)
        selected_indexes = self._material_frame_indexes(source, frame_indexes)
        settings = BasicMaterialSettings.model_validate(raw_settings).model_dump(
            mode="json"
        )
        request = {
            "_workspace_id": repository.workspace_id,
            "_expected_domain_revision_id": expected_revision_id,
            "_source_sha256": source["video"]["sha256"],
            "frameIndexes": selected_indexes,
            "settings": settings,
        }
        cache_key = self._fingerprint(
            {
                "type": "material_basic",
                "sourceId": source_id,
                "sourceSha256": source["video"]["sha256"],
                "revisionId": expected_revision_id,
                "frameIndexes": selected_indexes,
                "settings": settings,
            }
        )
        for existing in repository.list_jobs(limit=100000):
            if (
                existing.get("type") == "material_basic"
                and existing.get("source_id") == source_id
                and existing.get("cache_key") == cache_key
                and existing.get("status") in {"queued", "running", "completed"}
            ):
                variant_id = str(
                    (((existing.get("result") or {}).get("basic") or {}).get("variantId"))
                    or ""
                )
                if existing.get("status") != "completed" or repository.get_material_variant(
                    variant_id
                ):
                    return existing
        if domain["revisionId"] != expected_revision_id:
            raise RuntimeError("素材工作区 revision 已变化，请刷新后重新提交 Basic。")
        job = repository.create_job(
            source_id,
            "material_basic",
            source_id,
            request,
            cache_key,
            character_id=str(source["characterId"]),
        )
        self.submit(job["id"])
        return job

    def create_material_import(
        self,
        character_id: str,
        files: list[dict[str, Any]],
        *,
        target_fps: float | None,
        expected_revision_id: str,
    ) -> dict[str, Any]:
        repository = self.database.session.require_repository()
        domain = repository.workspace_domain()
        character = next(
            (item for item in domain.get("characters") or [] if item.get("id") == character_id),
            None,
        )
        if character is None:
            raise KeyError(character_id)
        if not files:
            raise ValueError("素材导入任务至少需要一个视频文件。")
        if domain.get("revisionId") != expected_revision_id:
            raise RuntimeError("素材工作区 revision 已变化，请刷新后重新导入。")
        request = {
            "_workspace_id": repository.workspace_id,
            "_expected_domain_revision_id": expected_revision_id,
            "targetFps": target_fps,
            "files": [dict(item) for item in files],
        }
        cache_key = self._fingerprint({
            "type": "material_import",
            "characterId": character_id,
            "revisionId": expected_revision_id,
            "targetFps": target_fps,
            "files": [
                {"name": item.get("name"), "sha256": item.get("sha256")}
                for item in files
            ],
        })
        job = repository.create_job(
            character_id,
            "material_import",
            None,
            request,
            cache_key,
            character_id=character_id,
        )
        self.submit(job["id"])
        return job

    def create_material_remote(
        self,
        source_id: str,
        quality: str,
        raw_settings: dict[str, Any],
        *,
        expected_revision_id: str,
        frame_indexes: list[int],
    ) -> dict[str, Any]:
        if quality not in {"high", "ultra"}:
            raise ValueError("远程素材档位只能是 High 或 Ultra。")
        if not self.settings.remote_matting_url:
            raise RuntimeError("尚未配置远程抠图服务。请在客户端设置中启用并保存服务端地址。")
        repository = self.database.session.require_repository()
        domain = repository.workspace_domain()
        source = repository.get_material_source(source_id)
        if source is None:
            raise KeyError(source_id)
        selected_indexes = self._material_frame_indexes(source, frame_indexes)
        settings = dict(raw_settings)
        request = {
            "_workspace_id": repository.workspace_id,
            "_expected_domain_revision_id": expected_revision_id,
            "_source_sha256": source["video"]["sha256"],
            "frameIndexes": selected_indexes,
            "quality": quality,
            "settings": settings,
        }
        cache_key = self._fingerprint({
            "type": "material_remote",
            "sourceId": source_id,
            "sourceSha256": source["video"]["sha256"],
            "revisionId": expected_revision_id,
            "frameIndexes": selected_indexes,
            "quality": quality,
            "settings": settings,
        })
        for existing in repository.list_jobs(limit=100000):
            if (
                existing.get("type") == "material_remote"
                and existing.get("source_id") == source_id
                and existing.get("cache_key") == cache_key
                and existing.get("status") in {"queued", "running", "completed"}
            ):
                return existing
        if domain["revisionId"] != expected_revision_id:
            raise RuntimeError("素材工作区 revision 已变化，请刷新后重新提交远程处理。")
        job = repository.create_job(
            source_id,
            "material_remote",
            source_id,
            request,
            cache_key,
            character_id=str(source["characterId"]),
        )
        self.submit(job["id"])
        return job

    @staticmethod
    def _material_frame_indexes(
        source: dict[str, Any], frame_indexes: list[int]
    ) -> list[int]:
        frame_count = len(source.get("frames") or [])
        if frame_count < 1:
            raise ValueError("素材源没有可处理帧。")
        if not frame_indexes:
            raise ValueError("至少选择一帧后才能创建处理任务。")
        if len(frame_indexes) != len(set(frame_indexes)):
            raise ValueError("处理帧选择不能包含重复索引。")
        if any(index < 0 or index >= frame_count for index in frame_indexes):
            raise ValueError("处理帧选择包含越界索引。")
        if frame_indexes != sorted(frame_indexes):
            raise ValueError("处理帧索引必须严格升序。")
        return list(frame_indexes)

    def _validate_job_snapshot(self, job: dict[str, Any]) -> None:
        request = job.get("request") or {}
        repository = self.database.session.require_repository()
        if request.get("_workspace_id") and request["_workspace_id"] != repository.workspace_id:
            raise RuntimeError("任务所属工作区已变化，旧任务未执行。")
        if job.get("type") in {"material_basic", "material_remote", "material_import"}:
            domain = repository.workspace_domain()
            if domain.get("revisionId") != request.get("_expected_domain_revision_id"):
                raise RuntimeError("素材工作区已在处理任务排队期间变化，旧任务未执行。")
            if job.get("type") == "material_import":
                return
            source = repository.get_material_source(str(job.get("source_id") or ""))
            if (
                source is None
                or (source.get("video") or {}).get("sha256")
                != request.get("_source_sha256")
            ):
                raise RuntimeError("处理任务的素材源已变化，旧任务未执行。")
            return
        raise RuntimeError(f"当前运行库不接受任务类型：{job.get('type')!r}")

    def _worker(self) -> None:
        while not self._stop.is_set():
            try:
                job_id = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if job_id is None:
                return
            try:
                job = self.database.get_job(job_id)
                if job:
                    with self._project_locks[job["project_id"]]:
                        self._run(job_id)
            finally:
                self._last_control_check.pop(job_id, None)
                with self._cancel_guard:
                    self._cancel_requests.discard(job_id)
                self._queue.task_done()

    @staticmethod
    def _fingerprint(value: Any) -> str:
        return hashlib.sha256(
            json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    def _check_control(self, job_id: str) -> None:
        if self._stop.is_set():
            raise JobInterrupted("服务正在停止。")
        with self._cancel_guard:
            if job_id in self._cancel_requests:
                raise JobCancelled("任务已由用户取消。")
        now = time.monotonic()
        if now - self._last_control_check.get(job_id, 0.0) < 0.1:
            return
        job = self.database.get_job(job_id)
        if not job:
            raise RuntimeError("任务已不存在。")
        if job.get("status") in {"cancelling", "cancelled"}:
            raise JobCancelled("任务已由用户取消。")
        self._last_control_check[job_id] = now

    def _reporter(self, job_id: str, start: float = 0.0, end: float = 1.0) -> Callable[[str, float, str | None], None]:
        last: dict[str, Any] = {"stage": None, "progress": -1.0}

        def report(stage: str, progress: float, message: str | None = None) -> None:
            mapped = max(0.0, min(1.0, start + (end - start) * progress))
            if stage != last["stage"] or mapped - last["progress"] >= 0.01 or mapped >= end:
                self.database.update_job(job_id, stage=stage, progress=mapped)
                last["stage"] = stage
                last["progress"] = mapped
            if message:
                self.database.append_job_log(job_id, "info", message)
        return report

    def _material_basic(
        self,
        job: dict[str, Any],
        settings: dict[str, Any],
        start: float,
        end: float,
    ) -> dict[str, Any]:
        repository = self.database.session.require_repository()
        request = job.get("request") or {}
        expected_revision_id = str(
            request.get("_expected_domain_revision_id") or ""
        )
        if not expected_revision_id:
            raise RuntimeError("Basic 任务缺少素材 revision 快照。")
        raw_frame_indexes = request.get("frameIndexes")
        if not isinstance(raw_frame_indexes, list):
            raise RuntimeError("Basic 任务缺少当前 frameIndexes 契约。")
        frame_indexes = list(raw_frame_indexes)
        run_root = Path(self.store.runtime_root) / "material-basic" / str(job["id"])
        with self._gpu_lock:
            return process_basic_material(
                repository,
                str(job.get("source_id") or ""),
                run_root,
                settings,
                self.settings,
                self._reporter(job["id"], start, end),
                lambda: self._check_control(job["id"]),
                expected_revision_id=expected_revision_id,
                frame_indexes=frame_indexes,
            )

    def _material_import(
        self,
        job: dict[str, Any],
        start: float,
        end: float,
    ) -> dict[str, Any]:
        repository = self.database.session.require_repository()
        request = job.get("request") or {}
        expected_revision_id = str(request.get("_expected_domain_revision_id") or "")
        character_id = str(job.get("character_id") or "")
        raw_files = request.get("files") or []
        if not expected_revision_id or not character_id or not isinstance(raw_files, list) or not raw_files:
            raise RuntimeError("素材导入任务缺少工作区、角色或文件快照。")
        library = MaterialLibrary(repository, self.settings, Path(self.store.runtime_root))
        imported: list[dict[str, Any]] = []
        revision = expected_revision_id
        total = len(raw_files)
        reporter = self._reporter(str(job["id"]), start, end)
        for index, raw in enumerate(raw_files):
            self._check_control(str(job["id"]))
            try:
                incoming = self.store.resolve_incoming(raw.get("path"))
            except (OSError, ValueError) as exc:
                raise RuntimeError("素材导入任务的受管上传文件已丢失。") from exc
            expected_sha = str(raw.get("sha256") or "")
            if not expected_sha or sha256_file(incoming) != expected_sha:
                raise RuntimeError("素材导入任务的上传文件完整性校验失败。")
            segment_start = index / total
            segment_end = (index + 1) / total

            def report(stage: str, progress: float, message: str | None) -> None:
                reporter(
                    f"import_{stage}",
                    segment_start + max(0.0, min(1.0, progress)) * (segment_end - segment_start),
                    message or f"正在导入第 {index + 1}/{total} 个视频。",
                )

            result = library.import_video(
                character_id,
                incoming,
                str(raw.get("displayName") or Path(str(raw.get("name") or "video")).stem),
                target_fps=request.get("targetFps"),
                expected_revision_id=revision,
                report=report,
                check_control=lambda: self._check_control(str(job["id"])),
            )
            imported.append(result)
            revision = str(repository.workspace_domain()["revisionId"])
            reporter(
                "import_published",
                segment_end,
                f"已发布第 {index + 1}/{total} 个视频。",
            )
        return {"imported": imported, "revisionId": revision}

    def _material_remote(
        self,
        job: dict[str, Any],
        settings: dict[str, Any],
        start: float,
        end: float,
    ) -> dict[str, Any]:
        repository = self.database.session.require_repository()
        request = job.get("request") or {}
        expected_revision_id = str(request.get("_expected_domain_revision_id") or "")
        quality = str(request.get("quality") or "")
        if not expected_revision_id or quality not in {"high", "ultra"}:
            raise RuntimeError("远程素材任务缺少有效的 revision 或质量档位。")
        service_url = self.settings.remote_matting_url
        if not service_url:
            raise RuntimeError("远程抠图服务配置缺失。")
        run_root = Path(self.store.runtime_root) / "material-remote" / str(job["id"])
        run_root.mkdir(parents=True, exist_ok=True)
        raw_frame_indexes = request.get("frameIndexes")
        if not isinstance(raw_frame_indexes, list):
            raise RuntimeError("远程素材任务缺少当前 frameIndexes 契约。")
        frame_indexes = list(raw_frame_indexes)
        source = repository.get_material_source(str(job.get("source_id") or ""))
        if source is None:
            raise RuntimeError("远程素材任务的源素材已不存在。")
        source_frames = source.get("frames") or []
        selected_indexes = self._material_frame_indexes(source, frame_indexes)
        expected_source_frame_ids = [str(source_frames[index]["id"]) for index in selected_indexes]
        prepared = prepare_remote_submission(
            repository,
            str(job.get("source_id") or ""),
            quality,
            settings,
            run_root / "upload.zip",
            frame_indexes=frame_indexes,
        )
        reporter = self._reporter(job["id"], start, end)

        async def execute() -> Any:
            config = RemoteMattingConfig(
                service_url,
            )
            remote_job_id: str | None = None
            async with RemoteMattingClient(config) as client:
                try:
                    submitted = await client.submit(prepared)
                    remote_job_id = submitted.jobId
                    reporter("remote_queued", submitted.progress, "远程任务已提交。")
                    terminal = submitted.state
                    if terminal not in {
                        RemoteJobState.COMPLETED,
                        RemoteJobState.FAILED,
                        RemoteJobState.CANCELLED,
                    }:
                        async for event in client.events(remote_job_id):
                            self._check_control(str(job["id"]))
                            reporter(event.stage or "remote_processing", event.progress, event.message)
                            terminal = event.state
                    if terminal == RemoteJobState.CANCELLED:
                        raise JobCancelled("远程素材任务已取消。")
                    if terminal == RemoteJobState.FAILED:
                        status = await client.status(remote_job_id)
                        raise RuntimeError(
                            status.error.message if status.error else "远程素材处理失败。"
                        )
                    self._check_control(str(job["id"]))
                    reporter("remote_download", 0.98, "正在下载并校验远程结果。")
                    return await client.download_result(remote_job_id, run_root / "result.zip")
                except JobCancelled:
                    if remote_job_id:
                        try:
                            await client.cancel(remote_job_id)
                        except Exception:
                            pass
                    raise

        try:
            downloaded = asyncio.run(execute())
            variant = publish_remote_result(
                repository,
                str(job.get("source_id") or ""),
                downloaded,
                run_root / "publish",
                expected_revision_id=expected_revision_id,
                expected_source_frame_ids=expected_source_frame_ids,
            )
            reporter("remote_publish", 1.0, "远程结果已发布为不可变素材版本。")
            return {"variantId": variant["id"], "quality": quality}
        finally:
            shutil.rmtree(run_root, ignore_errors=True)

    def _run(self, job_id: str) -> None:
        job = self.database.get_job(job_id)
        if not job or job["status"] != "queued":
            return
        try:
            self._validate_job_snapshot(job)
        except Exception as exc:
            failure = build_job_failure(
                exc,
                job_type=str(job.get("type") or ""),
                failed_stage="starting",
            )
            self.database.update_job(
                job_id,
                status="failed",
                stage="failed",
                error=str(exc),
                result={"failure": failure},
                finished_at=utc_now(),
            )
            self.database.append_job_log(job_id, "error", str(exc))
            return
        self.database.update_job(
            job_id,
            status="running",
            stage="starting",
            progress=0.0,
            error=None,
            started_at=utc_now(),
            finished_at=None,
            result={},
        )
        self.database.append_job_log(job_id, "info", f"开始任务：{job['type']}")
        request = job.get("request") or {}
        settings = request.get("settings") or {}
        result: dict[str, Any] = {}
        try:
            if job["type"] == "material_basic":
                result["basic"] = self._material_basic(job, settings, 0.0, 1.0)
            elif job["type"] == "material_import":
                result["import"] = self._material_import(job, 0.0, 1.0)
            elif job["type"] == "material_remote":
                result["remote"] = self._material_remote(job, settings, 0.0, 1.0)
            else:
                raise RuntimeError(f"未知任务类型：{job['type']}")
            self.database.update_job(
                job_id,
                status="completed",
                stage="completed",
                progress=1.0,
                result=result,
                finished_at=utc_now(),
            )
            self.database.append_job_log(job_id, "success", "任务完成。")
        except JobInterrupted as exc:
            self.database.update_job(
                job_id,
                status="queued",
                stage="resuming",
                error=None,
                finished_at=None,
            )
            self.database.append_job_log(job_id, "warning", str(exc))
        except JobCancelled as exc:
            self.database.update_job(
                job_id,
                status="cancelled",
                stage="cancelled",
                error=None,
                result={},
                finished_at=utc_now(),
            )
            self.database.append_job_log(job_id, "warning", str(exc))
        except Exception as exc:
            failed_job = self.database.get_job(job_id) or job
            failure = build_job_failure(
                exc,
                job_type=str(job.get("type") or ""),
                failed_stage=str(failed_job.get("stage") or "failed"),
            )
            self.database.update_job(
                job_id,
                status="failed",
                stage="failed",
                error=str(exc),
                result={**result, "failure": failure},
                finished_at=utc_now(),
            )
            self.database.append_job_log(job_id, "error", str(exc))
