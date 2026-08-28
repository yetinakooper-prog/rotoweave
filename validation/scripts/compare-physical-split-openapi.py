from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def _emit() -> None:
    from backend.app.config import Settings
    from backend.app.main import create_app
    from server.api import create_admin_app, create_remote_app
    from server.config import RemoteServerSettings
    from server.service import RemoteService

    class SchemaOnlyProcessor:
        pass

    with tempfile.TemporaryDirectory(prefix="rotoweave-openapi-") as temporary:
        root = Path(temporary)
        client = create_app(
            Settings(data_root=root / "client-data", runtime_root=root / "client-runtime")
        )
        settings = RemoteServerSettings(data_root=root / "server-data")
        service = RemoteService(settings, processor=SchemaOnlyProcessor())
        remote = create_remote_app(settings, service=service, manage_lifecycle=False)
        admin = create_admin_app(service)
        payload = {
            "client": client.openapi(),
            "remote": remote.openapi(),
            "admin": admin.openapi(),
        }
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def _load(python_path: list[Path]) -> dict[str, object]:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join(str(path) for path in python_path)
    environment["ROTOWEAVE_MODELS_ROOT"] = str(Path(tempfile.gettempdir()) / "rotoweave-models-absent")
    completed = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--emit"],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=environment,
    )
    return json.loads(completed.stdout)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--emit", action="store_true")
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--workspace", type=Path, default=Path(__file__).resolve().parent.parent.parent)
    arguments = parser.parse_args()
    if arguments.emit:
        _emit()
        return 0
    if arguments.baseline is None:
        parser.error("--baseline is required")

    baseline = _load([arguments.baseline.resolve()])
    workspace = arguments.workspace.resolve()
    current = _load(
        [
            workspace / "RotoWeaveClient",
            workspace / "RotoWeaveServer",
            workspace / "RotoWeaveContracts",
        ]
    )
    failed = False
    for name in ("client", "remote", "admin"):
        before_bytes = _canonical(baseline[name])
        after_bytes = _canonical(current[name])
        before_hash = hashlib.sha256(before_bytes).hexdigest()
        after_hash = hashlib.sha256(after_bytes).hexdigest()
        equal = before_bytes == after_bytes
        failed = failed or not equal
        print(f"{name}: equal={str(equal).lower()} before={before_hash} after={after_hash}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
