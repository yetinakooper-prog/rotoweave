from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent.parent
PYTHON_AREAS = {
    "client": (
        ROOT / "RotoWeaveClient" / "backend",
        ROOT / "RotoWeaveClient" / "scripts",
    ),
    "server": (
        ROOT / "RotoWeaveServer" / "server",
        ROOT / "RotoWeaveServer" / "worker" / "cuda_matting",
        ROOT / "RotoWeaveServer" / "scripts",
    ),
    "contracts": (
        ROOT / "RotoWeaveContracts" / "contracts",
        ROOT / "RotoWeaveContracts" / "tools",
    ),
}
IMPORT_RE = re.compile(r"(?:from\s+|import\s*\()[\"']([^\"']+)[\"']")


def python_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


def validate_python() -> list[str]:
    issues: list[str] = []
    forbidden = {
        "client": ("server", "worker.cuda_matting"),
        "server": ("backend", "client"),
        "contracts": ("backend", "server", "worker", "client"),
    }
    for area, roots in PYTHON_AREAS.items():
        for root in roots:
            for path in root.rglob("*.py"):
                if "__pycache__" in path.parts or "tests" in path.parts:
                    continue
                for imported in python_imports(path):
                    if any(imported == value or imported.startswith(f"{value}.") for value in forbidden[area]):
                        issues.append(f"{path.relative_to(ROOT)}: {area} imports forbidden module {imported}")
    return issues


def validate_typescript() -> list[str]:
    issues: list[str] = []
    for path in (ROOT / "RotoWeaveClient" / "app").rglob("*"):
        if path.suffix not in {".ts", ".tsx"}:
            continue
        for imported in IMPORT_RE.findall(path.read_text(encoding="utf-8")):
            normalized = imported.replace("\\", "/")
            if "/server/" in normalized or normalized.startswith("server/") or "server-admin" in normalized:
                issues.append(f"{path.relative_to(ROOT)}: client imports forbidden path {imported}")
    return issues


def validate_manifests() -> list[str]:
    issues: list[str] = []
    for path in (
        ROOT / "RotoWeaveClient" / "project.json",
        ROOT / "RotoWeaveServer" / "project.json",
        ROOT / "RotoWeaveContracts" / "project.json",
    ):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            issues.append(f"{path.relative_to(ROOT)}: invalid project manifest: {exc}")
            continue
        shared = value.get("sharedDependency")
        if value.get("productVersion") != "4.0.0" or (
            path.parent.name != "RotoWeaveContracts" and shared != "../RotoWeaveContracts"
        ):
            issues.append(f"{path.relative_to(ROOT)}: incompatible product or shared contract")
    return issues


def validate_root_layout() -> list[str]:
    forbidden = (
        "app",
        "backend",
        "client",
        "contracts",
        "server",
        "server-admin",
        "worker",
        "node_modules",
        "model-packs",
    )
    return [f"root retains application path {name}" for name in forbidden if (ROOT / name).exists()]


def main() -> int:
    issues = [
        *validate_python(),
        *validate_typescript(),
        *validate_manifests(),
        *validate_root_layout(),
    ]
    if issues:
        print("Project boundary validation failed:")
        for issue in issues:
            print(f"- {issue}")
        return 1
    print("Project boundary validation passed: client -> contracts <- server; no application cross-imports.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
