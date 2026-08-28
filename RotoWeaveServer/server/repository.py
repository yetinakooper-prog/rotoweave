from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


TERMINAL_STATES = {"completed", "failed", "cancelled"}
SENSITIVE_KEYS = {
    "authorization", "bearer", "bearer_token", "credential", "credentials",
    "private_key", "secret", "token", "tls_private_key", "user_content",
}


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    request_sha256 TEXT NOT NULL,
    submission_json TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('queued','running','completed','failed','cancelled')),
    progress REAL NOT NULL DEFAULT 0,
    stage TEXT,
    error_json TEXT,
    input_path TEXT NOT NULL,
    result_path TEXT,
    result_sha256 TEXT,
    attempt INTEGER NOT NULL DEFAULT 0,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    expires_at TEXT,
    queue_order INTEGER NOT NULL DEFAULT 0,
    parent_job_id TEXT,
    model_configuration_digest TEXT,
    quality_profile TEXT
);
CREATE INDEX IF NOT EXISTS ix_jobs_state_order ON jobs(state, queue_order, created_at);
CREATE INDEX IF NOT EXISTS ix_jobs_expires ON jobs(expires_at);
CREATE TABLE IF NOT EXISTS events (
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    sequence INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(job_id, sequence)
);
CREATE TABLE IF NOT EXISTS logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT REFERENCES jobs(id) ON DELETE CASCADE,
    level TEXT NOT NULL,
    event TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS queue_control (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    paused INTEGER NOT NULL DEFAULT 0,
    maintenance INTEGER NOT NULL DEFAULT 0,
    mode TEXT NOT NULL DEFAULT 'normal',
    revision INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS operational_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    level TEXT NOT NULL,
    component TEXT NOT NULL,
    event TEXT NOT NULL,
    job_id TEXT,
    detail_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_operational_logs_created ON operational_logs(created_at, id);
CREATE INDEX IF NOT EXISTS ix_operational_logs_job ON operational_logs(job_id, id);
CREATE TABLE IF NOT EXISTS model_library_roots (
    id TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    path TEXT NOT NULL UNIQUE,
    priority INTEGER NOT NULL DEFAULT 0,
    enabled INTEGER NOT NULL DEFAULT 1,
    read_only INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS model_assets (
    id TEXT PRIMARY KEY,
    root_id TEXT NOT NULL REFERENCES model_library_roots(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    model_id TEXT NOT NULL,
    path TEXT NOT NULL UNIQUE,
    bytes INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    state TEXT NOT NULL,
    verification_kind TEXT,
    verification_contract_digest TEXT,
    verification_receipt_digest TEXT,
    error_text TEXT,
    verified_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_model_assets_role ON model_assets(role, state);
CREATE TABLE IF NOT EXISTS model_asset_verification_receipts (
    role TEXT NOT NULL,
    asset_sha256 TEXT NOT NULL,
    compatibility_policy_digest TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('passed','failed')),
    observation_digest TEXT,
    receipt_json TEXT NOT NULL,
    receipt_digest TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(role, asset_sha256, compatibility_policy_digest)
);
CREATE TABLE IF NOT EXISTS model_bindings (
    role TEXT PRIMARY KEY,
    asset_id TEXT NOT NULL REFERENCES model_assets(id) ON DELETE RESTRICT,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS model_configurations (
    digest TEXT PRIMARY KEY,
    recipe_id TEXT NOT NULL,
    recipe_digest TEXT NOT NULL,
    state TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 0,
    profile_states_json TEXT NOT NULL DEFAULT '{}',
    schema_version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    activated_at TEXT
);
CREATE TABLE IF NOT EXISTS model_configuration_assets (
    configuration_digest TEXT NOT NULL REFERENCES model_configurations(digest) ON DELETE CASCADE,
    role TEXT NOT NULL,
    asset_id TEXT NOT NULL REFERENCES model_assets(id) ON DELETE RESTRICT,
    PRIMARY KEY(configuration_digest, role)
);
CREATE TABLE IF NOT EXISTS model_self_test_receipts (
    configuration_digest TEXT NOT NULL,
    profile TEXT NOT NULL,
    runtime_digest TEXT NOT NULL,
    gpu_identity TEXT NOT NULL,
    driver_version TEXT NOT NULL,
    state TEXT NOT NULL,
    receipt_json TEXT NOT NULL,
    receipt_digest TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(configuration_digest, profile)
);
CREATE TABLE IF NOT EXISTS model_operations (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    state TEXT NOT NULL,
    stage TEXT NOT NULL,
    progress REAL NOT NULL DEFAULT 0,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    detail_json TEXT NOT NULL DEFAULT '{}',
    error_text TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
PRAGMA user_version=4;
"""


class IdempotencyConflict(RuntimeError):
    pass


class QueueRevisionConflict(RuntimeError):
    pass


class InvalidQueueOperation(RuntimeError):
    pass


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): ("[REDACTED]" if str(key).casefold() in SENSITIVE_KEYS else _redact(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        lowered = value.casefold()
        if "-----begin" in lowered or "bearer " in lowered:
            return "[REDACTED]"
    return value


class RemoteQueueRepository:
    def __init__(self, path: Path):
        self.path = path.resolve(strict=False)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._guard = threading.RLock()
        self._initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def _initialize(self) -> None:
        version = 0
        has_tables = False
        if self.path.is_file():
            with self.connect() as connection:
                tables = connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                ).fetchall()
                version = int(connection.execute("PRAGMA user_version").fetchone()[0])
                has_tables = bool(tables)
                if tables and version not in {3, 4}:
                    raise RuntimeError(
                        f"服务端队列数据库 schema {version} 不受支持；只允许 schema 3 或 4。"
                    )
        if has_tables and version == 3:
            self._backup_schema3_database()
            self._migrate_schema3_to_4()
        with self.connect() as connection:
            connection.executescript(SCHEMA)
            connection.execute("INSERT OR IGNORE INTO queue_control(singleton,updated_at) VALUES(1,?)", (utc_now(),))
            connection.execute(
                "UPDATE model_operations SET state='interrupted',stage='interrupted',updated_at=? "
                "WHERE state IN ('queued','running')",
                (utc_now(),),
            )
            if connection.execute("PRAGMA foreign_key_check").fetchall():
                raise RuntimeError("当前队列数据库外键校验失败。")

    def _backup_schema3_database(self) -> Path:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        backup_path = self.path.with_name(f"{self.path.name}.schema3-backup-{stamp}")
        suffix = 1
        while backup_path.exists():
            backup_path = self.path.with_name(f"{self.path.name}.schema3-backup-{stamp}-{suffix}")
            suffix += 1
        with self.connect() as source, sqlite3.connect(backup_path) as target:
            source.backup(target)
        return backup_path

    def _migrate_schema3_to_4(self) -> None:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute("ALTER TABLE model_assets ADD COLUMN verification_kind TEXT")
                connection.execute("ALTER TABLE model_assets ADD COLUMN verification_contract_digest TEXT")
                connection.execute("ALTER TABLE model_assets ADD COLUMN verification_receipt_digest TEXT")
                connection.execute("ALTER TABLE model_configurations ADD COLUMN schema_version INTEGER NOT NULL DEFAULT 1")
                connection.execute(
                    "CREATE TABLE model_asset_verification_receipts ("
                    "role TEXT NOT NULL,asset_sha256 TEXT NOT NULL,compatibility_policy_digest TEXT NOT NULL,"
                    "state TEXT NOT NULL CHECK(state IN ('passed','failed')),observation_digest TEXT,"
                    "receipt_json TEXT NOT NULL,receipt_digest TEXT NOT NULL,created_at TEXT NOT NULL,"
                    "PRIMARY KEY(role,asset_sha256,compatibility_policy_digest))"
                )
                connection.execute(
                    "UPDATE model_assets SET verification_kind='official' "
                    "WHERE state='verified' AND verification_kind IS NULL"
                )
                connection.execute(
                    "UPDATE model_assets SET state='candidate',error_text=NULL,verified_at=NULL "
                    "WHERE state='mismatch'"
                )
                connection.execute("PRAGMA user_version=4")
                if connection.execute("PRAGMA foreign_key_check").fetchall():
                    raise RuntimeError("schema 3 到 4 迁移后的外键校验失败。")
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._guard, self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except BaseException:
                connection.rollback()
                raise
            else:
                connection.commit()

    @staticmethod
    def _decode(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        result["submission"] = json.loads(result.pop("submission_json"))
        result["error"] = json.loads(result.pop("error_json")) if result.get("error_json") else None
        result["cancel_requested"] = bool(result["cancel_requested"])
        return result

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            return self._decode(connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())

    def get_by_idempotency(self, key: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            return self._decode(connection.execute("SELECT * FROM jobs WHERE idempotency_key=?", (key,)).fetchone())

    def enqueue(self, *, job_id: str, idempotency_key: str, request_sha256: str,
                submission: dict[str, Any], input_path: str,
                model_configuration_digest: str | None = None, quality_profile: str | None = None,
                parent_job_id: str | None = None) -> tuple[dict[str, Any], bool]:
        now = utc_now()
        with self.transaction() as connection:
            existing = connection.execute("SELECT * FROM jobs WHERE idempotency_key=?", (idempotency_key,)).fetchone()
            if existing is not None:
                if str(existing["request_sha256"]) != request_sha256:
                    raise IdempotencyConflict("Idempotency-Key was reused for another request.")
                return self._decode(existing) or {}, False
            order = int(connection.execute("SELECT COALESCE(MAX(queue_order),0)+1 AS value FROM jobs").fetchone()["value"])
            connection.execute(
                """INSERT INTO jobs(id,idempotency_key,request_sha256,submission_json,state,
                   progress,stage,input_path,created_at,updated_at,queue_order,parent_job_id,
                   model_configuration_digest,quality_profile)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (job_id, idempotency_key, request_sha256, json.dumps(submission, ensure_ascii=False, sort_keys=True),
                 "queued", 0.0, "queued", input_path, now, now, order, parent_job_id,
                 model_configuration_digest, quality_profile),
            )
            self._append_event(connection, job_id, "queued", 0.0, "queued", "Job accepted.")
            self._append_log(connection, job_id, "info", "job.accepted", {
                "requestSha256": request_sha256,
                "modelConfigurationDigest": model_configuration_digest,
                "qualityProfile": quality_profile,
            })
            return self._decode(connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()) or {}, True

    def _next_sequence(self, connection: sqlite3.Connection, job_id: str) -> int:
        row = connection.execute("SELECT COALESCE(MAX(sequence),-1)+1 AS value FROM events WHERE job_id=?", (job_id,)).fetchone()
        return int(row["value"])

    def _append_event(self, connection: sqlite3.Connection, job_id: str, state: str,
                      progress: float, stage: str | None, message: str | None) -> int:
        sequence = self._next_sequence(connection, job_id)
        payload = {"protocolVersion": 1, "jobId": job_id, "sequence": sequence, "state": state,
                   "progress": max(0.0, min(1.0, float(progress))), "stage": stage, "message": message}
        connection.execute("INSERT INTO events(job_id,sequence,payload_json,created_at) VALUES(?,?,?,?)",
                           (job_id, sequence, json.dumps(payload, ensure_ascii=False), utc_now()))
        return sequence

    def _append_log(self, connection: sqlite3.Connection, job_id: str | None, level: str,
                    event: str, detail: dict[str, Any], component: str | None = None) -> None:
        enriched = dict(detail)
        if job_id:
            job = connection.execute(
                "SELECT quality_profile,model_configuration_digest FROM jobs WHERE id=?",
                (job_id,),
            ).fetchone()
            if job is not None:
                enriched.setdefault("profile", job["quality_profile"])
                enriched.setdefault("configurationDigest", job["model_configuration_digest"])
        safe = _redact(enriched)
        encoded = json.dumps(safe, ensure_ascii=False, sort_keys=True)
        now = utc_now()
        connection.execute("INSERT INTO logs(job_id,level,event,detail_json,created_at) VALUES(?,?,?,?,?)",
                           (job_id, level, event, encoded, now))
        connection.execute(
            "INSERT INTO operational_logs(level,component,event,job_id,detail_json,created_at) VALUES(?,?,?,?,?,?)",
            (level, component or event.partition(".")[0] or "service", event, job_id, encoded, now),
        )

    def log(self, job_id: str | None, level: str, event: str, detail: dict[str, Any], *, component: str | None = None) -> None:
        with self.transaction() as connection:
            self._append_log(connection, job_id, level, event, detail, component)

    def recover(self) -> list[str]:
        now = utc_now()
        with self.transaction() as connection:
            rows = connection.execute("SELECT id FROM jobs WHERE state='running' ORDER BY queue_order,created_at").fetchall()
            ids = [str(row["id"]) for row in rows]
            for job_id in ids:
                connection.execute("UPDATE jobs SET state='queued',stage='recovered',progress=0,attempt=attempt+1,started_at=NULL,updated_at=? WHERE id=?", (now, job_id))
                self._append_event(connection, job_id, "queued", 0.0, "recovered", "Service restarted; job requeued.")
                self._append_log(connection, job_id, "warning", "job.recovered", {})
            return ids

    def queue_control(self) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM queue_control WHERE singleton=1").fetchone()
            return {"paused": bool(row["paused"]), "maintenance": bool(row["maintenance"]),
                    "mode": row["mode"], "revision": int(row["revision"]), "updatedAt": row["updated_at"]}

    def set_queue_control(self, *, paused: bool | None = None, maintenance: bool | None = None,
                          mode: str | None = None, expected_revision: int | None = None) -> dict[str, Any]:
        with self.transaction() as connection:
            row = connection.execute("SELECT * FROM queue_control WHERE singleton=1").fetchone()
            if expected_revision is not None and int(row["revision"]) != expected_revision:
                raise QueueRevisionConflict("Queue revision changed; refresh before applying the operation.")
            values = {"paused": int(row["paused"] if paused is None else paused),
                      "maintenance": int(row["maintenance"] if maintenance is None else maintenance),
                      "mode": str(row["mode"] if mode is None else mode)}
            connection.execute("UPDATE queue_control SET paused=?,maintenance=?,mode=?,revision=revision+1,updated_at=? WHERE singleton=1",
                               (values["paused"], values["maintenance"], values["mode"], utc_now()))
            self._append_log(connection, None, "warning", "queue.control_changed", values, "admin")
        return self.queue_control()

    def claim_next(self) -> dict[str, Any] | None:
        with self.transaction() as connection:
            control = connection.execute("SELECT paused,maintenance FROM queue_control WHERE singleton=1").fetchone()
            if bool(control["paused"]) or bool(control["maintenance"]):
                return None
            row = connection.execute("SELECT * FROM jobs WHERE state='queued' ORDER BY queue_order,created_at LIMIT 1").fetchone()
            if row is None:
                return None
            now = utc_now()
            connection.execute("UPDATE jobs SET state='running',stage='starting',started_at=?,updated_at=? WHERE id=? AND state='queued'", (now, now, row["id"]))
            self._append_event(connection, str(row["id"]), "running", 0.01, "starting", "Worker starting.")
            return self._decode(connection.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone())

    def progress(self, job_id: str, progress: float, stage: str, message: str | None = None) -> bool:
        with self.transaction() as connection:
            row = connection.execute("SELECT state FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None or row["state"] != "running":
                return False
            connection.execute("UPDATE jobs SET progress=?,stage=?,updated_at=? WHERE id=?",
                               (max(0.0, min(0.99, progress)), stage, utc_now(), job_id))
            self._append_event(connection, job_id, "running", progress, stage, message)
            return True

    def _finish(self, job_id: str, state: str, ttl_hours: float, *, error: dict[str, Any] | None = None,
                result_path: str | None = None, result_sha256: str | None = None) -> bool:
        now_dt = datetime.now(UTC)
        now = now_dt.isoformat().replace("+00:00", "Z")
        expires = (now_dt + timedelta(hours=ttl_hours)).isoformat().replace("+00:00", "Z")
        with self.transaction() as connection:
            row = connection.execute("SELECT state,progress FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None or row["state"] != "running":
                return False
            connection.execute("UPDATE jobs SET state=?,progress=?,stage=?,error_json=?,result_path=?,result_sha256=?,finished_at=?,expires_at=?,updated_at=? WHERE id=?",
                               (state, 1.0 if state != "cancelled" else float(row["progress"]), state,
                                json.dumps(error, ensure_ascii=False) if error else None, result_path, result_sha256,
                                now, expires, now, job_id))
            self._append_event(connection, job_id, state, 1.0, state, str((error or {}).get("message") or ("Result ready." if state == "completed" else "Job finished.")))
            self._append_log(connection, job_id, "error" if state == "failed" else "info", f"job.{state}", error or {"resultSha256": result_sha256})
            return True

    def complete(self, job_id: str, result_path: str, result_sha256: str, ttl_hours: float) -> bool:
        return self._finish(job_id, "completed", ttl_hours, result_path=result_path, result_sha256=result_sha256)

    def fail(self, job_id: str, error: dict[str, Any], ttl_hours: float) -> bool:
        return self._finish(job_id, "failed", ttl_hours, error=error)

    def cancel(self, job_id: str, ttl_hours: float) -> dict[str, Any] | None:
        now_dt = datetime.now(UTC)
        now = now_dt.isoformat().replace("+00:00", "Z")
        expires = (now_dt + timedelta(hours=ttl_hours)).isoformat().replace("+00:00", "Z")
        with self.transaction() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                return None
            if row["state"] in {"queued", "running"}:
                connection.execute("UPDATE jobs SET state='cancelled',stage='cancelled',cancel_requested=1,finished_at=?,expires_at=?,updated_at=? WHERE id=?", (now, expires, now, job_id))
                self._append_event(connection, job_id, "cancelled", float(row["progress"]), "cancelled", "Cancelled by operator or client.")
                self._append_log(connection, job_id, "warning", "job.cancelled", {})
            return self._decode(connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())

    def events_after(self, job_id: str, sequence: int) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute("SELECT payload_json FROM events WHERE job_id=? AND sequence>? ORDER BY sequence", (job_id, sequence)).fetchall()
            return [json.loads(str(row["payload_json"])) for row in rows]

    def list_jobs(self, *, state: str | None = None, limit: int = 100, offset: int = 0) -> dict[str, Any]:
        where = "WHERE state=?" if state else ""
        args: list[Any] = [state] if state else []
        with self.connect() as connection:
            total = int(connection.execute(f"SELECT COUNT(*) AS value FROM jobs {where}", args).fetchone()["value"])
            rows = connection.execute(f"SELECT * FROM jobs {where} ORDER BY CASE state WHEN 'running' THEN 0 WHEN 'queued' THEN 1 ELSE 2 END,queue_order,created_at DESC LIMIT ? OFFSET ?", [*args, min(max(limit, 1), 500), max(offset, 0)]).fetchall()
        return {"items": [self._decode(row) for row in rows], "total": total, "control": self.queue_control()}

    def reorder(self, job_ids: list[str], expected_revision: int) -> dict[str, Any]:
        with self.transaction() as connection:
            control = connection.execute("SELECT revision FROM queue_control WHERE singleton=1").fetchone()
            if int(control["revision"]) != expected_revision:
                raise QueueRevisionConflict("Queue revision changed; refresh before reordering.")
            queued = [str(row["id"]) for row in connection.execute("SELECT id FROM jobs WHERE state='queued' ORDER BY queue_order,created_at")]
            if set(job_ids) != set(queued) or len(job_ids) != len(queued):
                raise InvalidQueueOperation("Ordering must contain every queued job exactly once.")
            for order, job_id in enumerate(job_ids, start=1):
                connection.execute("UPDATE jobs SET queue_order=?,updated_at=? WHERE id=? AND state='queued'", (order, utc_now(), job_id))
            connection.execute("UPDATE queue_control SET revision=revision+1,updated_at=? WHERE singleton=1", (utc_now(),))
            self._append_log(connection, None, "info", "queue.reordered", {"jobIds": job_ids}, "admin")
        return self.queue_control()

    def retry(self, job_id: str, new_job_id: str, new_idempotency_key: str, new_input_path: str) -> dict[str, Any]:
        source = self.get(job_id)
        if source is None or source["state"] not in TERMINAL_STATES:
            raise InvalidQueueOperation("Only terminal jobs can be retried.")
        job, _ = self.enqueue(job_id=new_job_id, idempotency_key=new_idempotency_key,
                              request_sha256=source["request_sha256"], submission=source["submission"],
                              input_path=new_input_path,
                              model_configuration_digest=source.get("model_configuration_digest"),
                              quality_profile=source.get("quality_profile"), parent_job_id=job_id)
        self.log(new_job_id, "info", "job.retried", {"parentJobId": job_id}, component="admin")
        return job

    def expired(self, now: str | None = None) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute("SELECT * FROM jobs WHERE expires_at IS NOT NULL AND expires_at<=? AND state IN ('completed','failed','cancelled')", (now or utc_now(),)).fetchall()
            return [self._decode(row) or {} for row in rows]

    def delete(self, job_id: str, *, terminal_only: bool = False) -> bool:
        with self.transaction() as connection:
            row = connection.execute("SELECT state FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                return False
            if terminal_only and row["state"] not in TERMINAL_STATES:
                raise InvalidQueueOperation("Only terminal jobs can be deleted.")
            self._append_log(connection, job_id, "warning", "job.deleted", {"state": row["state"]}, "admin")
            connection.execute("DELETE FROM jobs WHERE id=?", (job_id,))
            return True

    def stats(self) -> dict[str, Any]:
        with self.connect() as connection:
            states = {str(row["state"]): int(row["count"]) for row in connection.execute("SELECT state,COUNT(*) AS count FROM jobs GROUP BY state")}
            errors = [{"jobId": row["job_id"], "event": row["event"], "detail": json.loads(str(row["detail_json"])), "createdAt": row["created_at"]}
                      for row in connection.execute("SELECT job_id,event,detail_json,created_at FROM operational_logs WHERE level='error' ORDER BY id DESC LIMIT 20")]
        return {"states": states, "recentErrors": errors, "control": self.queue_control()}

    def query_logs(self, *, level: str | None = None, component: str | None = None,
                   event: str | None = None, job_id: str | None = None, text: str | None = None,
                   profile: str | None = None, model_role: str | None = None,
                   configuration_digest: str | None = None, operation_id: str | None = None,
                   since: str | None = None, until: str | None = None,
                   limit: int = 200, offset: int = 0) -> dict[str, Any]:
        clauses: list[str] = []
        args: list[Any] = []
        for column, value in (("level", level), ("component", component), ("event", event), ("job_id", job_id)):
            if value:
                clauses.append(f"{column}=?")
                args.append(value)
        if text:
            clauses.append("(event LIKE ? OR detail_json LIKE ?)")
            args.extend([f"%{text}%", f"%{text}%"])
        for value in (profile, model_role, configuration_digest, operation_id):
            if value:
                clauses.append("detail_json LIKE ?")
                args.append(f"%{value}%")
        if since:
            clauses.append("created_at>=?")
            args.append(since)
        if until:
            clauses.append("created_at<=?")
            args.append(until)
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        with self.connect() as connection:
            total = int(connection.execute(f"SELECT COUNT(*) AS value FROM operational_logs {where}", args).fetchone()["value"])
            rows = connection.execute(f"SELECT * FROM operational_logs {where} ORDER BY id DESC LIMIT ? OFFSET ?", [*args, min(max(limit, 1), 1000), max(offset, 0)]).fetchall()
        return {"items": [{**dict(row), "detail": json.loads(str(row["detail_json"]))} for row in rows], "total": total}

    def cleanup_logs(self, retention_days: int, max_rows: int) -> dict[str, Any]:
        cutoff = (datetime.now(UTC) - timedelta(days=retention_days)).isoformat().replace("+00:00", "Z")
        with self.transaction() as connection:
            old = connection.execute("DELETE FROM operational_logs WHERE created_at<?", (cutoff,)).rowcount
            count = int(connection.execute("SELECT COUNT(*) AS value FROM operational_logs").fetchone()["value"])
            excess = max(0, count - max_rows)
            capped = 0
            if excess:
                capped = connection.execute("DELETE FROM operational_logs WHERE id IN (SELECT id FROM operational_logs ORDER BY id LIMIT ?)", (min(excess, 5000),)).rowcount
        return {"removed": old + capped, "cutoff": cutoff, "remainingOverLimit": max(0, excess - capped)}

    @staticmethod
    def new_job_id() -> str:
        return f"rjob-{uuid.uuid4().hex}"
