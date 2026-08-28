from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .persistence.connection import SQLiteConnectionManager
from .repositories.common import new_id, utc_now
from contracts.product import RUNTIME_DATABASE_SCHEMA_VERSION


RUNTIME_DATABASE_SCHEMA = RUNTIME_DATABASE_SCHEMA_VERSION
RUNTIME_DATABASE_APPLICATION_ID = 1095321173

RUNTIME_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS runtime_metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    type TEXT NOT NULL,
    source_id TEXT,
    character_id TEXT,
    status TEXT NOT NULL,
    stage TEXT NOT NULL DEFAULT 'queued',
    progress REAL NOT NULL DEFAULT 0,
    attempt INTEGER NOT NULL DEFAULT 1,
    cache_key TEXT,
    request_json TEXT NOT NULL DEFAULT '{}',
    result_json TEXT NOT NULL DEFAULT '{}',
    logs_json TEXT NOT NULL DEFAULT '[]',
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS jobs_status_idx ON jobs(status, created_at);
CREATE INDEX IF NOT EXISTS jobs_character_idx ON jobs(character_id, created_at DESC);
"""

CURRENT_TABLES = {"runtime_metadata", "jobs"}
CURRENT_JOB_COLUMNS = {
    "id", "workspace_id", "project_id", "type", "source_id", "character_id",
    "status", "stage", "progress", "attempt", "cache_key", "request_json",
    "result_json", "logs_json", "error", "created_at", "updated_at",
    "started_at", "finished_at",
}


class RuntimeRepository(SQLiteConnectionManager):
    """Disposable per-workspace job, recovery, thumbnail and cache metadata."""

    def __init__(self, path: Path, workspace_id: str):
        super().__init__(path)
        self.workspace_id = workspace_id

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        reset = False
        if self.path.is_file():
            try:
                with self.connection() as connection:
                    application_id = int(
                        connection.execute("PRAGMA application_id").fetchone()[0]
                    )
                    user_version = int(
                        connection.execute("PRAGMA user_version").fetchone()[0]
                    )
                    metadata = {
                        str(row["key"]): str(row["value"])
                        for row in connection.execute(
                            "SELECT key,value FROM runtime_metadata"
                        ).fetchall()
                    }
                    tables = {
                        str(row[0])
                        for row in connection.execute(
                            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                        ).fetchall()
                    }
                    job_columns = {
                        str(row[1])
                        for row in connection.execute("PRAGMA table_info(jobs)").fetchall()
                    }
                    reset = (
                        application_id != RUNTIME_DATABASE_APPLICATION_ID
                        or user_version != RUNTIME_DATABASE_SCHEMA
                        or metadata.get("workspaceId") != self.workspace_id
                        or metadata.get("schemaVersion") != str(RUNTIME_DATABASE_SCHEMA)
                        or tables != CURRENT_TABLES
                        or job_columns != CURRENT_JOB_COLUMNS
                    )
            except sqlite3.Error:
                reset = True
        if reset:
            self._delete_database_files()
        with self.connection() as connection:
            connection.executescript(RUNTIME_SCHEMA_SQL)
            connection.execute(
                "INSERT OR REPLACE INTO runtime_metadata(key,value) VALUES('workspaceId',?)",
                (self.workspace_id,),
            )
            connection.execute(
                "INSERT OR REPLACE INTO runtime_metadata(key,value) VALUES('schemaVersion',?)",
                (str(RUNTIME_DATABASE_SCHEMA),),
            )
            connection.execute(f"PRAGMA application_id={RUNTIME_DATABASE_APPLICATION_ID}")
            connection.execute(f"PRAGMA user_version={RUNTIME_DATABASE_SCHEMA}")
            connection.commit()

    def _delete_database_files(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            Path(str(self.path) + suffix).unlink(missing_ok=True)

    @staticmethod
    def _decode(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        for source, target, fallback in (
            ("request_json", "request", {}),
            ("result_json", "result", {}),
            ("logs_json", "logs", []),
        ):
            if source not in result:
                continue
            raw = result.pop(source)
            try:
                result[target] = json.loads(raw or json.dumps(fallback))
            except json.JSONDecodeError:
                result[target] = fallback
        return result

    def create_job(
        self,
        project_id: str,
        job_type: str,
        source_id: str | None,
        request: dict[str, Any],
        cache_key: str | None = None,
        character_id: str | None = None,
    ) -> dict[str, Any]:
        payload = dict(request)
        job_id = new_id("job")
        now = utc_now()
        with self.transaction() as connection:
            connection.execute(
                """INSERT INTO jobs(
                       id,workspace_id,project_id,type,source_id,character_id,
                       status,stage,progress,attempt,cache_key,
                       request_json,result_json,logs_json,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,'queued','queued',0,1,?,?,'{}','[]',?,?)""",
                (
                    job_id,
                    self.workspace_id,
                    project_id,
                    job_type,
                    source_id,
                    character_id,
                    cache_key,
                    json.dumps(payload, ensure_ascii=False),
                    now,
                    now,
                ),
            )
        return self.get_job(job_id) or {}

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self.connection() as connection:
            return self._decode(
                connection.execute(
                    "SELECT * FROM jobs WHERE id=? AND workspace_id=?",
                    (job_id, self.workspace_id),
                ).fetchone()
            )

    def list_jobs(
        self,
        limit: int = 100,
        character_id: str | None = None,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM jobs WHERE workspace_id=?"
        params: list[Any] = [self.workspace_id]
        if character_id:
            sql += " AND character_id=?"
            params.append(character_id)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self.connection() as connection:
            return [
                self._decode(row) or {}
                for row in connection.execute(sql, tuple(params)).fetchall()
            ]

    def update_job(self, job_id: str, **changes: Any) -> dict[str, Any] | None:
        allowed = {
            "status",
            "stage",
            "progress",
            "attempt",
            "result",
            "error",
            "started_at",
            "finished_at",
        }
        assignments: list[str] = []
        values: list[Any] = []
        for key, value in changes.items():
            if key not in allowed:
                continue
            if key == "result":
                assignments.append("result_json=?")
                values.append(json.dumps(value or {}, ensure_ascii=False))
            else:
                assignments.append(f"{key}=?")
                values.append(value)
        if not assignments:
            return self.get_job(job_id)
        assignments.append("updated_at=?")
        values.extend([utc_now(), job_id, self.workspace_id])
        with self.transaction() as connection:
            connection.execute(
                f"UPDATE jobs SET {', '.join(assignments)} WHERE id=? AND workspace_id=?",
                tuple(values),
            )
            row = connection.execute(
                "SELECT * FROM jobs WHERE id=? AND workspace_id=?",
                (job_id, self.workspace_id),
            ).fetchone()
        return self._decode(row)

    def append_job_log(self, job_id: str, level: str, message: str) -> None:
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT logs_json FROM jobs WHERE id=? AND workspace_id=?",
                (job_id, self.workspace_id),
            ).fetchone()
            if row is None:
                return
            try:
                logs = list(json.loads(str(row["logs_json"]) or "[]"))
            except (json.JSONDecodeError, TypeError):
                logs = []
            logs.append({"time": utc_now(), "level": level, "message": message})
            connection.execute(
                "UPDATE jobs SET logs_json=?,updated_at=? WHERE id=? AND workspace_id=?",
                (
                    json.dumps(logs[-500:], ensure_ascii=False),
                    utc_now(),
                    job_id,
                    self.workspace_id,
                ),
            )

    def request_cancel(self, job_id: str) -> dict[str, Any] | None:
        now = utc_now()
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT status FROM jobs WHERE id=? AND workspace_id=?",
                (job_id, self.workspace_id),
            ).fetchone()
            if row is None:
                return None
            status = str(row["status"])
            if status == "queued":
                connection.execute(
                    """UPDATE jobs SET status='cancelled',stage='cancelled',
                       error=NULL,finished_at=?,updated_at=?
                       WHERE id=? AND workspace_id=?""",
                    (now, now, job_id, self.workspace_id),
                )
            elif status == "running":
                connection.execute(
                    """UPDATE jobs SET status='cancelling',stage='cancelling',
                       updated_at=? WHERE id=? AND workspace_id=?""",
                    (now, job_id, self.workspace_id),
                )
            return self._decode(
                connection.execute(
                    "SELECT * FROM jobs WHERE id=? AND workspace_id=?",
                    (job_id, self.workspace_id),
                ).fetchone()
            )

    def recover_jobs(self) -> list[str]:
        with self.transaction() as connection:
            connection.execute(
                """UPDATE jobs SET status='cancelled',stage='cancelled',
                   finished_at=?,updated_at=?
                   WHERE workspace_id=? AND status='cancelling'""",
                (utc_now(), utc_now(), self.workspace_id),
            )
            rows = connection.execute(
                """SELECT id FROM jobs
                   WHERE workspace_id=? AND status IN ('queued','running')
                   ORDER BY created_at""",
                (self.workspace_id,),
            ).fetchall()
            ids = [str(row["id"]) for row in rows]
            if ids:
                connection.executemany(
                    """UPDATE jobs SET status='queued',stage='queued',progress=0,
                           attempt=attempt+1,started_at=NULL,finished_at=NULL,
                           updated_at=? WHERE id=? AND workspace_id=?""",
                    [(utc_now(), job_id, self.workspace_id) for job_id in ids],
                )
        return ids

    def delete_jobs_for_character(self, character_id: str) -> None:
        with self.transaction() as connection:
            connection.execute(
                "DELETE FROM jobs WHERE workspace_id=? AND character_id=?",
                (self.workspace_id, character_id),
            )
