from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any, Iterable

from .windows_file_dialog import choose_windows_folder


TERMINAL_EXPORT_STATES = {"completed", "failed", "cancelled"}


def choose_output_directory() -> str | None:
    selected = choose_windows_folder("选择 RotoWeave 部署 ZIP 输出目录")
    return str(selected) if selected else None


class DeploymentBundleManager:
    """Localhost-only coordinator for one source-checkout export at a time."""

    def __init__(self, project_root: Path, role: str) -> None:
        if role not in {"client", "server"}:
            raise ValueError("Page export role must be client or server.")
        self.project_root = project_root.resolve(strict=False)
        self.role = role
        self.bootstrap = self.project_root / "scripts" / "rotoweave_bootstrap.py"
        self._lock = threading.RLock()
        self._tokens: dict[str, tuple[Path, float]] = {}
        self._jobs: dict[str, dict[str, Any]] = {}
        self._processes: dict[str, subprocess.Popen[str]] = {}

    def _command(self, *arguments: str) -> list[str]:
        if not self.bootstrap.is_file() or getattr(sys, "frozen", False):
            raise RuntimeError("部署包导出只在完整源码工作副本中可用。")
        return [sys.executable, str(self.bootstrap), "--project-root", str(self.project_root), *arguments]

    @staticmethod
    def _environment() -> dict[str, str]:
        environment = os.environ.copy()
        environment["PYTHONIOENCODING"] = "utf-8"
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment["NPM_CONFIG_UPDATE_NOTIFIER"] = "false"
        environment["NPM_CONFIG_AUDIT"] = "false"
        environment["NPM_CONFIG_FUND"] = "false"
        return environment

    @staticmethod
    def _clean_error_output(lines: Iterable[str]) -> str:
        useful: list[str] = []
        for raw_line in lines:
            line = raw_line.strip()
            if not line:
                continue
            error_index = line.find("ERROR:")
            if error_index >= 0:
                useful.append(line[error_index:])
                continue
            if line.lower().startswith("npm notice"):
                continue
            useful.append(line)
        return "\n".join(useful[-30:])

    def plan(self) -> dict[str, Any]:
        completed = subprocess.run(
            self._command("plan", "--role", self.role, "--json"),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=self._environment(),
            check=False,
            timeout=120,
        )
        if completed.returncode != 0:
            raise RuntimeError((completed.stderr or completed.stdout or "部署包预检失败。").strip())
        result = json.loads(completed.stdout)
        result["pageExportEnabled"] = True
        result["singleActiveExport"] = True
        return result

    def select_directory(self) -> dict[str, Any]:
        selected = choose_output_directory()
        if not selected:
            return {"selectionToken": None, "displayPath": None}
        path = Path(selected).resolve(strict=True)
        if not path.is_dir() or path.is_symlink():
            raise RuntimeError("输出目标必须是普通目录。")
        token = uuid.uuid4().hex
        with self._lock:
            now = time.time()
            self._tokens = {key: value for key, value in self._tokens.items() if value[1] > now}
            self._tokens[token] = (path, now + 300)
        return {"selectionToken": token, "displayPath": str(path), "expiresInSeconds": 300}

    def _snapshot(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise KeyError(job_id)
            return dict(job)

    def start(self, selection_token: str) -> dict[str, Any]:
        with self._lock:
            now = time.time()
            selected = self._tokens.pop(selection_token, None)
            if selected is None or selected[1] <= now:
                raise ValueError("输出目录选择已失效，请重新选择。")
            if any(item["state"] not in TERMINAL_EXPORT_STATES for item in self._jobs.values()):
                raise ValueError("已有部署包导出正在执行。")
            output_directory = selected[0]
            job_id = f"deployment-export-{uuid.uuid4().hex}"
            created = time.time()
            self._jobs[job_id] = {
                "id": job_id,
                "role": self.role,
                "state": "queued",
                "stage": "queued",
                "progress": 0.0,
                "message": "等待导出",
                "cancelRequested": False,
                "outputDirectory": str(output_directory),
                "outputPath": None,
                "sha256": None,
                "bytes": None,
                "error": None,
                "createdAt": created,
                "updatedAt": created,
            }
        thread = threading.Thread(target=self._run, args=(job_id, output_directory), name=job_id, daemon=True)
        thread.start()
        return self._snapshot(job_id)

    def _update(self, job_id: str, **changes: Any) -> None:
        with self._lock:
            if job_id not in self._jobs:
                return
            self._jobs[job_id].update(changes)
            self._jobs[job_id]["updatedAt"] = time.time()

    def _run(self, job_id: str, output_directory: Path) -> None:
        before = {path.resolve(strict=False) for path in output_directory.glob(".RotoWeave-*.partial-*")}
        command = self._command(
            "export-bundle",
            "--role",
            self.role,
            "--output-directory",
            str(output_directory),
            "--json-progress",
        )
        self._update(job_id, state="running", stage="preflight", progress=0.01, message="正在启动导出")
        process: subprocess.Popen[str] | None = None
        stderr_lines: deque[str] = deque(maxlen=200)
        stderr_thread: threading.Thread | None = None
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=self._environment(),
                creationflags=(
                    getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                    | getattr(subprocess, "CREATE_NO_WINDOW", 0)
                ) if os.name == "nt" else 0,
            )
            with self._lock:
                self._processes[job_id] = process
            assert process.stderr is not None

            def drain_stderr() -> None:
                assert process is not None and process.stderr is not None
                for line in process.stderr:
                    stderr_lines.append(line)

            stderr_thread = threading.Thread(
                target=drain_stderr,
                name=f"{job_id}-stderr",
                daemon=True,
            )
            stderr_thread.start()
            assert process.stdout is not None
            for line in process.stdout:
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    self._update(job_id, message=line.strip() or "正在导出")
                    continue
                if payload.get("type") == "progress":
                    self._update(
                        job_id,
                        stage=str(payload.get("stage") or "running"),
                        progress=float(payload.get("progress") or 0),
                        message=str(payload.get("message") or "正在导出"),
                    )
                elif payload.get("type") == "result":
                    self._update(
                        job_id,
                        outputPath=payload.get("outputPath"),
                        sha256=payload.get("sha256"),
                        bytes=payload.get("bytes"),
                    )
            return_code = process.wait()
            stderr_thread.join(timeout=5)
            stderr = self._clean_error_output(stderr_lines)
            snapshot = self._snapshot(job_id)
            if snapshot["cancelRequested"]:
                self._update(job_id, state="cancelled", stage="cancelled", message="导出已取消", error=None)
            elif return_code == 0 and snapshot.get("outputPath"):
                self._update(job_id, state="completed", stage="completed", progress=1.0, message="部署 ZIP 已完成")
            else:
                self._update(job_id, state="failed", stage="failed", message="部署包导出失败", error=stderr or f"导出进程退出码 {return_code}")
        except Exception as exc:
            snapshot = self._snapshot(job_id)
            state = "cancelled" if snapshot["cancelRequested"] else "failed"
            self._update(job_id, state=state, stage=state, message="导出已取消" if state == "cancelled" else "部署包导出失败", error=None if state == "cancelled" else str(exc))
        finally:
            with self._lock:
                self._processes.pop(job_id, None)
            for path in output_directory.glob(".RotoWeave-*.partial-*"):
                resolved = path.resolve(strict=False)
                if resolved not in before and path.is_file():
                    try:
                        path.unlink()
                    except OSError:
                        pass

    def get(self, job_id: str) -> dict[str, Any]:
        return self._snapshot(job_id)

    def cancel(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise KeyError(job_id)
            if job["state"] in TERMINAL_EXPORT_STATES:
                return dict(job)
            job["cancelRequested"] = True
            job["updatedAt"] = time.time()
            process = self._processes.get(job_id)
        if process is not None and process.poll() is None:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                    capture_output=True,
                    check=False,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            else:
                process.terminate()
        return self._snapshot(job_id)

    def reveal(self, job_id: str) -> dict[str, Any]:
        snapshot = self._snapshot(job_id)
        raw = snapshot.get("outputPath")
        if not raw:
            raise ValueError("导出结果目录尚不可用。")
        path = Path(str(raw)).resolve(strict=True)
        if not path.is_file() or os.name != "nt":
            raise ValueError("仅 Windows 可在资源管理器中打开已完成部署包。")
        os.startfile(str(path.parent))
        return {"opened": True}
