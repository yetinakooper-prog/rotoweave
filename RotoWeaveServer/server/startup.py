from __future__ import annotations

import threading
from datetime import UTC, datetime
from typing import Any


STAGES = (
    ("configuration", "配置读取"),
    ("storage", "数据目录与数据库"),
    ("network_boundary", "可信局域网与本机管理边界"),
    ("model_catalog", "模型目录"),
    ("model_verification", "独立模型与文件校验"),
    ("runtime", "GPU 与运行时检测"),
    ("self_test", "self-test 与预热"),
    ("ready", "Worker 与 API ready"),
)


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class StartupTracker:
    def __init__(self) -> None:
        self._guard = threading.RLock()
        self.started_at = _now()
        self.completed_at: str | None = None
        self._stages = [
            {"id": key, "label": label, "state": "pending", "startedAt": None,
             "completedAt": None, "detail": None, "error": None,
             "filesVerified": 0, "filesTotal": 0}
            for key, label in STAGES
        ]

    def update(self, stage_id: str, state: str, detail: str | None = None, *,
               error: str | None = None, files_verified: int | None = None,
               files_total: int | None = None) -> None:
        with self._guard:
            stage = next(item for item in self._stages if item["id"] == stage_id)
            if stage["startedAt"] is None and state in {"running", "passed", "warning", "failed"}:
                stage["startedAt"] = _now()
            if state in {"passed", "warning", "failed"}:
                stage["completedAt"] = _now()
            stage["state"] = state
            stage["detail"] = detail
            stage["error"] = error
            if files_verified is not None:
                stage["filesVerified"] = max(0, files_verified)
            if files_total is not None:
                stage["filesTotal"] = max(0, files_total)
            if stage_id == "ready" and state in {"passed", "warning"}:
                self.completed_at = _now()

    def snapshot(self) -> dict[str, Any]:
        with self._guard:
            stages = [dict(item) for item in self._stages]
        units_total = len(stages)
        units_done = 0.0
        for stage in stages:
            if stage["state"] in {"passed", "warning"}:
                units_done += 1
            elif stage["state"] == "running" and stage["filesTotal"]:
                units_done += min(1.0, stage["filesVerified"] / stage["filesTotal"])
        failed = next((item for item in stages if item["state"] == "failed"), None)
        return {
            "startedAt": self.started_at,
            "completedAt": self.completed_at,
            "state": "failed" if failed else ("ready" if self.completed_at else "starting"),
            "progress": round(units_done / units_total, 4),
            "completedStages": sum(1 for item in stages if item["state"] in {"passed", "warning"}),
            "totalStages": units_total,
            "stages": stages,
            "repairSuggestion": (
                "查看该阶段错误详情，修正配置或模型后重启服务。" if failed else None
            ),
        }
