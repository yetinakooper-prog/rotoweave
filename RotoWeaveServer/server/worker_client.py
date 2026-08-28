from __future__ import annotations

import json
import logging
import os
import queue
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from contextlib import contextmanager
from pathlib import Path

from contracts.paths import contracts_root
from contracts.hardware import probe_cuda_hardware
from typing import Any, Callable, Protocol


class WorkerSettings(Protocol):
    runtime_root: Path
    cuda_matting_exchange_root: Path
    model_configuration_path: Path | None
    runtime_profile: str | None
    cuda_matting_worker_command: tuple[str, ...]
    selected_gpu_uuid: str | None
    memory_mode: str | None


LOGGER = logging.getLogger(__name__)
HEARTBEAT_INTERVAL_SECONDS = 5.0
HEARTBEAT_TIMEOUT_SECONDS = 16.0
CANCEL_GRACE_SECONDS = 30.0
WINDOWS_EXCHANGE_ROOT_MAX_CHARS = 140
WORKER_PROTOCOL_VERSION = 2
_WINDOWS_DLL_DIRECTORY_LOCK = threading.Lock()


@contextmanager
def external_runtime_dll_scope():
    """Prevent a frozen launcher's Python DLL directory leaking to a child runtime."""

    bundle_root = getattr(sys, "_MEIPASS", None)
    if os.name != "nt" or not getattr(sys, "frozen", False) or not bundle_root:
        yield
        return
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    set_dll_directory = kernel32.SetDllDirectoryW
    set_dll_directory.argtypes = [ctypes.c_wchar_p]
    set_dll_directory.restype = ctypes.c_int
    with _WINDOWS_DLL_DIRECTORY_LOCK:
        if not set_dll_directory(None):
            raise OSError(ctypes.get_last_error(), "Unable to clear frozen DLL directory")
        try:
            yield
        finally:
            if not set_dll_directory(str(bundle_root)):
                LOGGER.error("Unable to restore frozen DLL directory: %s", bundle_root)


class WorkerProtocolError(RuntimeError):
    """The fixed CUDA worker failed or violated the NDJSON contract."""


def validate_worker_exchange_root(root: Path) -> Path:
    """Keep OpenCV-bound worker paths inside its Windows filename envelope."""

    resolved = root.resolve(strict=False)
    if os.name == "nt":
        raw = str(resolved)
        try:
            raw.encode("ascii")
        except UnicodeEncodeError as exc:
            raise WorkerProtocolError(
                "The CUDA worker exchange path must contain ASCII characters only on "
                "Windows; set ROTOWEAVE_CUDA_MATTING_EXCHANGE_ROOT to a short ASCII path."
            ) from exc
        if len(raw) > WINDOWS_EXCHANGE_ROOT_MAX_CHARS:
            raise WorkerProtocolError(
                "The CUDA worker exchange path is too long for signed OpenCV I/O; set "
                "ROTOWEAVE_CUDA_MATTING_EXCHANGE_ROOT to an ASCII path no longer than "
                f"{WINDOWS_EXCHANGE_ROOT_MAX_CHARS} characters."
            )
    return resolved


class CudaMattingWorkerClient:
    """Persistent single-actor NDJSON client.

    stdout is protocol-only.  stderr is drained independently so verbose model
    logs can never deadlock a production inference request.
    """

    def __init__(self, settings: WorkerSettings, generation_root: Path | None = None):
        self.settings = settings
        self.generation_root = (
            validate_worker_exchange_root(generation_root)
            if generation_root is not None
            else validate_worker_exchange_root(settings.cuda_matting_exchange_root)
        )
        self._process: subprocess.Popen[str] | None = None
        self._write_lock = threading.Lock()
        self._request_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._responses: dict[str, queue.Queue[dict[str, Any]]] = {}
        self._reader: threading.Thread | None = None
        self._stderr_reader: threading.Thread | None = None
        self._last_heartbeat = 0.0
        self._stderr_tail: deque[str] = deque(maxlen=80)

    @property
    def running(self) -> bool:
        process = self._process
        return process is not None and process.poll() is None

    def _worker_cwd(self, command: list[str]) -> Path:
        if getattr(sys, "frozen", False):
            bundle_root = Path(str(getattr(sys, "_MEIPASS", ""))).resolve(strict=False)
            worker_source_root = bundle_root / "worker-runtime"
            if not (worker_source_root / "worker" / "cuda_matting" / "__main__.py").is_file():
                raise WorkerProtocolError(
                    "The server package does not contain its isolated CUDA worker sources."
                )
            return worker_source_root
        return self.settings.runtime_root

    def _subprocess_environment(self) -> dict[str, str]:
        environment = {
            **os.environ,
            "PYTHONUNBUFFERED": "1",
            # Popen's ``encoding`` only controls the parent-side pipe
            # wrappers.  The embedded Python worker otherwise decodes stdin
            # with the active Windows locale (for example cp936), corrupting
            # non-ASCII workspace paths carried by the UTF-8 NDJSON protocol.
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
            # Product runtimes are immutable; never create bytecode in them or
            # consult user-site packages.
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "ROTOWEAVE_GENERATION_ROOT": str(self.generation_root),
        }
        selected_uuid = self.settings.selected_gpu_uuid
        if not selected_uuid:
            selected = probe_cuda_hardware().selected
            selected_uuid = selected.uuid if selected is not None else None
        if selected_uuid:
            environment["CUDA_VISIBLE_DEVICES"] = selected_uuid
            environment["ROTOWEAVE_SELECTED_GPU_UUID"] = selected_uuid
        else:
            environment.pop("ROTOWEAVE_SELECTED_GPU_UUID", None)
        memory_mode = str(self.settings.memory_mode or "full")
        environment["ROTOWEAVE_MEMORY_MODE"] = memory_mode
        if memory_mode != "full":
            environment["PYTORCH_CUDA_ALLOC_CONF"] = "expandable_segments:True,max_split_size_mb:128"
        if self.settings.model_configuration_path is not None:
            environment["ROTOWEAVE_MODEL_CONFIGURATION"] = str(
                self.settings.model_configuration_path
            )
            environment["ROTOWEAVE_RUNTIME_PROFILE"] = str(
                self.settings.runtime_profile or "high"
            )
        else:
            environment.pop("ROTOWEAVE_MODEL_CONFIGURATION", None)
            environment.pop("ROTOWEAVE_RUNTIME_PROFILE", None)
        source_adapter = (
            self.settings.runtime_root
            / "worker"
            / "cuda_matting"
            / "rotoweave_adapter.py"
        )
        if not getattr(sys, "frozen", False) and source_adapter.is_file():
            existing_python_path = environment.get("PYTHONPATH", "")
            environment["PYTHONPATH"] = os.pathsep.join(
                value
                for value in (
                    str(self.settings.runtime_root),
                    str(contracts_root()),
                    existing_python_path,
                )
                if value
            )
        if getattr(sys, "frozen", False):
            bundle_value = str(getattr(sys, "_MEIPASS", "")).strip()
            bundle_root = Path(bundle_value).resolve(strict=False) if bundle_value else None
            clean_path: list[str] = []
            for item in environment.get("PATH", "").split(os.pathsep):
                if not item:
                    continue
                if bundle_root is not None and Path(item).resolve(strict=False) == bundle_root:
                    continue
                clean_path.append(item)
            worker_runtime = Path(self.settings.cuda_matting_worker_command[0]).resolve().parent
            environment["PATH"] = os.pathsep.join((str(worker_runtime), *clean_path))
        return environment

    def start(self) -> None:
        with self._state_lock:
            if self.running:
                return
            self._terminate_locked()
            command = list(self.settings.cuda_matting_worker_command)
            environment = self._subprocess_environment()
            creation_flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            worker_cwd = self._worker_cwd(command)
            try:
                with external_runtime_dll_scope():
                    self._process = subprocess.Popen(
                        command,
                        cwd=str(worker_cwd),
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        bufsize=1,
                        env=environment,
                        creationflags=creation_flags,
                    )
            except OSError as exc:
                self._process = None
                raise WorkerProtocolError(
                    f"Unable to start cuda-matting-worker: {exc}"
                ) from exc
            self._last_heartbeat = time.monotonic()
            self._reader = threading.Thread(
                target=self._read_stdout,
                name="cuda-matting-stdout",
                daemon=True,
            )
            self._stderr_reader = threading.Thread(
                target=self._read_stderr,
                name="cuda-matting-stderr",
                daemon=True,
            )
            self._reader.start()
            self._stderr_reader.start()

    def _read_stdout(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return
        try:
            for raw in process.stdout:
                try:
                    message = json.loads(raw)
                except json.JSONDecodeError:
                    sample = " ".join(raw.strip().split())[:240]
                    self._fail_all(
                        "Worker wrote non-JSON data to stdout"
                        + (f": {sample}" if sample else ".")
                    )
                    # The protocol stream can no longer be trusted after one
                    # unframed line. Terminate this actor so the next request
                    # starts a clean process instead of timing out on a reader
                    # thread that already exited.
                    with self._state_lock:
                        if self._process is process:
                            self._terminate_locked()
                    return
                if not isinstance(message, dict):
                    self._fail_all("Worker wrote a non-object protocol message.")
                    with self._state_lock:
                        if self._process is process:
                            self._terminate_locked()
                    return
                if message.get("event") == "heartbeat":
                    self._last_heartbeat = time.monotonic()
                    continue
                request_id = str(message.get("id") or "")
                with self._state_lock:
                    target = self._responses.get(request_id)
                if target is not None:
                    target.put(message)
        finally:
            if process.poll() is not None:
                self._fail_all(
                    f"cuda-matting-worker exited with code {process.returncode}."
                )

    def _read_stderr(self) -> None:
        process = self._process
        if process is None or process.stderr is None:
            return
        for raw in process.stderr:
            line = raw.rstrip("\r\n")
            if line:
                self._stderr_tail.append(line)
                LOGGER.info("cuda-matting-worker: %s", line)

    def stderr_tail(self) -> list[str]:
        return list(self._stderr_tail)

    def _fail_all(self, reason: str) -> None:
        with self._state_lock:
            targets = list(self._responses.values())
        for target in targets:
            target.put({"ok": False, "error": {"code": "worker-exited", "message": reason}})

    def _send(self, payload: dict[str, Any]) -> None:
        process = self._process
        if process is None or process.poll() is not None or process.stdin is None:
            raise WorkerProtocolError("cuda-matting-worker is not running.")
        line = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        with self._write_lock:
            try:
                process.stdin.write(line + "\n")
                process.stdin.flush()
            except (OSError, BrokenPipeError) as exc:
                raise WorkerProtocolError("cuda-matting-worker pipe closed.") from exc

    def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float = 3600.0,
        check_control: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        with self._request_lock:
            self.start()
            self._stderr_tail.clear()
            request_id = uuid.uuid4().hex
            response_queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=2)
            with self._state_lock:
                self._responses[request_id] = response_queue
            started = time.monotonic()
            try:
                self._send(
                    {
                        "protocol": WORKER_PROTOCOL_VERSION,
                        "id": request_id,
                        "method": method,
                        "params": params or {},
                    }
                )
                while True:
                    if check_control is not None:
                        try:
                            check_control()
                        except BaseException:
                            self._cancel_request(request_id)
                            raise
                    elapsed = time.monotonic() - started
                    if elapsed > timeout:
                        self._cancel_request(request_id)
                        raise WorkerProtocolError(
                            f"cuda-matting-worker request timed out after {timeout:.1f}s."
                        )
                    process = self._process
                    if process is None or process.poll() is not None:
                        tail = " | ".join(self._stderr_tail)
                        raise WorkerProtocolError(
                            f"cuda-matting-worker exited unexpectedly ({tail or 'no stderr'})."
                        )
                    if time.monotonic() - self._last_heartbeat > HEARTBEAT_TIMEOUT_SECONDS:
                        self._cancel_request(request_id)
                        raise WorkerProtocolError("cuda-matting-worker heartbeat timed out.")
                    try:
                        response = response_queue.get(timeout=0.1)
                    except queue.Empty:
                        continue
                    if response.get("ok") is True:
                        result = response.get("result")
                        if not isinstance(result, dict):
                            raise WorkerProtocolError("Worker result must be an object.")
                        return result
                    error = response.get("error")
                    message = (
                        str(error.get("message") or error.get("code"))
                        if isinstance(error, dict)
                        else str(error or "unknown worker error")
                    )
                    if self._stderr_tail:
                        message += "\nWorker diagnostics:\n" + "\n".join(
                            self._stderr_tail
                        )
                    raise WorkerProtocolError(message)
            finally:
                with self._state_lock:
                    self._responses.pop(request_id, None)

    def _cancel_request(self, request_id: str) -> None:
        process = self._process
        if process is None or process.poll() is not None:
            return
        try:
            self._send(
                {
                    "protocol": WORKER_PROTOCOL_VERSION,
                    "id": uuid.uuid4().hex,
                    "method": "cancel",
                    "params": {"requestId": request_id},
                }
            )
        except WorkerProtocolError:
            pass
        with self._state_lock:
            target = self._responses.get(request_id)
        deadline = time.monotonic() + CANCEL_GRACE_SECONDS
        while process.poll() is None and time.monotonic() < deadline:
            if target is not None:
                try:
                    target.get(timeout=0.1)
                    return
                except queue.Empty:
                    continue
            time.sleep(0.1)
        if process.poll() is None:
            self.terminate()

    def health(self) -> dict[str, Any]:
        # The first request in a fresh Worker process performs a complete
        # cryptographic verification of the signed multi-GB pack. Warm probes
        # use the mutation-sensitive cache and remain fast.
        return self.request("health", timeout=120.0)

    def close(self) -> None:
        with self._state_lock:
            process = self._process
            if process is not None and process.poll() is None:
                try:
                    self._send(
                        {
                            "protocol": WORKER_PROTOCOL_VERSION,
                            "id": uuid.uuid4().hex,
                            "method": "shutdown",
                            "params": {},
                        }
                    )
                except WorkerProtocolError:
                    pass
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._terminate_locked()
            self._terminate_locked()

    def terminate(self) -> None:
        with self._state_lock:
            self._terminate_locked()

    def _terminate_locked(self) -> None:
        process = self._process
        self._process = None
        if process is None:
            return
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass

    def __enter__(self) -> "CudaMattingWorkerClient":
        self.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
