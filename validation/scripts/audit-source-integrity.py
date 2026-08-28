from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import sys
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parent.parent.parent
SOURCE_ROOTS = (
    ROOT / "RotoWeaveClient" / "app",
    ROOT / "RotoWeaveClient" / "backend" / "app",
    ROOT / "RotoWeaveServer" / "server",
    ROOT / "RotoWeaveServer" / "worker" / "cuda_matting",
    ROOT / "RotoWeaveServer" / "server-admin" / "src",
    ROOT / "RotoWeaveContracts" / "contracts",
)
RETIRED_SOURCE_ROOTS = (
    ROOT / "RotoWeaveServer" / "worker" / "matting4090",
)
ENTRYPOINTS = (
    ROOT / "RotoWeaveClient" / "app" / "main.tsx",
    ROOT / "RotoWeaveClient" / "backend" / "app" / "main.py",
    ROOT / "RotoWeaveServer" / "server" / "api.py",
    ROOT / "RotoWeaveServer" / "server-admin" / "src" / "main.tsx",
    # Imported by release/audit CLIs rather than the application server.
    ROOT / "RotoWeaveClient" / "backend" / "app" / "mattebench.py",
    ROOT / "RotoWeaveServer" / "server" / "processor.py",
    ROOT / "RotoWeaveContracts" / "contracts" / "hardware.py",
    # Loaded by the Server-owned fixed runtime together with independent model files.
    ROOT / "RotoWeaveServer" / "worker" / "cuda_matting" / "rotoweave_adapter.py",
    ROOT / "RotoWeaveServer" / "worker" / "cuda_matting" / "__main__.py",
)
TEXT_SUFFIXES = {".py", ".ts", ".tsx", ".mjs"}
TS_SUFFIXES = (".ts", ".tsx", ".js", ".jsx", ".mjs")
MARKER_PATTERN = re.compile(r"\b(TODO|FIXME|HACK|XXX)\b", re.IGNORECASE)
APPROVED_DUPLICATE_GROUPS = {
    frozenset(
        {
            "RotoWeaveClient/backend/app/images.py",
            "RotoWeaveServer/worker/cuda_matting/images.py",
        }
    )
}
TS_IMPORT_PATTERN = re.compile(
    r"(?:\b(?:import|export)\s+(?:type\s+)?[^;]*?\s+from\s+|\bimport\s*\()"
    r"[\"']([^\"']+)[\"']"
)


def _relative(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def _source_files() -> list[Path]:
    files: list[Path] = []
    for source_root in SOURCE_ROOTS:
        files.extend(
            path
            for path in source_root.rglob("*")
            if path.is_file()
            and path.suffix.lower() in TEXT_SUFFIXES
            and "__pycache__" not in path.parts
        )
    return sorted(files)


def _resolve_typescript(source: Path, specifier: str) -> Path | None:
    if specifier.startswith("@/"):
        candidate = (
            ROOT / "RotoWeaveClient" / "app" / specifier.removeprefix("@/")
        ).resolve(strict=False)
    elif specifier.startswith("."):
        candidate = (source.parent / specifier).resolve(strict=False)
    else:
        return None
    options = [candidate]
    options.extend(candidate.with_suffix(suffix) for suffix in TS_SUFFIXES)
    options.append(candidate.with_suffix(".json"))
    options.extend(candidate / f"index{suffix}" for suffix in TS_SUFFIXES)
    return next((option for option in options if option.is_file()), None)


def _python_module(path: Path) -> tuple[str, ...]:
    parts = list(path.relative_to(ROOT).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return tuple(parts)


def _resolve_python_module(parts: Iterable[str]) -> Path | None:
    values = tuple(parts)
    if not values:
        return None
    aliases = {
        "backend": ROOT / "RotoWeaveClient" / "backend",
        "server": ROOT / "RotoWeaveServer" / "server",
        "worker": ROOT / "RotoWeaveServer" / "worker",
        "contracts": ROOT / "RotoWeaveContracts" / "contracts",
    }
    base = aliases.get(values[0])
    relative = values[1:] if base is not None else values
    root = base or ROOT
    direct = root.joinpath(*relative).with_suffix(".py")
    package = root.joinpath(*relative) / "__init__.py"
    if direct.is_file():
        return direct.resolve()
    if package.is_file():
        return package.resolve()
    return None


def _python_dependencies(path: Path, text: str) -> tuple[set[Path], list[str]]:
    dependencies: set[Path] = set()
    unresolved: list[str] = []
    module = _python_module(path)
    package = module if path.name == "__init__.py" else module[:-1]
    tree = ast.parse(text, filename=str(path))
    for node in ast.walk(tree):
        candidates: list[tuple[str, ...]] = []
        label = ""
        if isinstance(node, ast.Import):
            for alias in node.names:
                values = tuple(alias.name.split("."))
                if values and values[0] in {"backend", "server", "worker", "contracts"}:
                    candidates.append(values)
                    label = alias.name
        elif isinstance(node, ast.ImportFrom):
            imported = tuple((node.module or "").split(".")) if node.module else ()
            if node.level:
                keep = len(package) - (node.level - 1)
                base = package[: max(keep, 0)] + imported
                label = "." * node.level + (node.module or "")
            elif imported and imported[0] in {"backend", "server", "worker", "contracts"}:
                base = imported
                label = node.module or ""
            else:
                continue
            candidates.append(base)
            candidates.extend(base + (alias.name,) for alias in node.names if alias.name != "*")
        resolved_any = False
        for candidate in candidates:
            resolved = _resolve_python_module(candidate)
            if resolved:
                dependencies.add(resolved)
                resolved_any = True
        if (
            candidates
            and not resolved_any
            and not any(part == "TYPE_CHECKING" for part in candidates[0])
        ):
            unresolved.append(label or ".".join(candidates[0]))
    return dependencies, unresolved


def _reachable(graph: dict[Path, set[Path]], entries: Iterable[Path]) -> set[Path]:
    seen: set[Path] = set()
    queue = deque(path.resolve() for path in entries if path.is_file())
    while queue:
        current = queue.popleft()
        if current in seen:
            continue
        seen.add(current)
        queue.extend(graph.get(current, set()) - seen)
    return seen


def audit() -> dict[str, Any]:
    files = _source_files()
    retired_source_roots = [
        _relative(path) for path in RETIRED_SOURCE_ROOTS if path.exists()
    ]
    graph: dict[Path, set[Path]] = {path.resolve(): set() for path in files}
    unresolved: list[dict[str, str]] = []
    markers: list[dict[str, Any]] = []
    oversized: list[dict[str, Any]] = []
    hashes: dict[str, list[Path]] = defaultdict(list)
    production_packages: set[str] = set()

    for path in files:
        payload = path.read_bytes()
        text = payload.decode("utf-8")
        line_count = text.count("\n") + (0 if text.endswith("\n") else 1)
        if line_count >= 1000:
            oversized.append(
                {"path": _relative(path), "lines": line_count, "classification": "review-split"}
            )
        if len(payload) >= 32:
            hashes[hashlib.sha256(payload).hexdigest()].append(path)
        for number, line in enumerate(text.splitlines(), start=1):
            match = MARKER_PATTERN.search(line)
            if match:
                markers.append(
                    {"path": _relative(path), "line": number, "marker": match.group(1).upper()}
                )

        if path.suffix == ".py":
            dependencies, missing = _python_dependencies(path, text)
            graph[path.resolve()].update(dependencies)
            unresolved.extend(
                {"path": _relative(path), "specifier": specifier}
                for specifier in sorted(set(missing))
            )
            continue

        for specifier in TS_IMPORT_PATTERN.findall(text):
            if specifier.startswith(".") or specifier.startswith("@/"):
                dependency = _resolve_typescript(path, specifier)
                if dependency:
                    graph[path.resolve()].add(dependency.resolve())
                else:
                    unresolved.append({"path": _relative(path), "specifier": specifier})
            else:
                if specifier.startswith("@"):
                    production_packages.add("/".join(specifier.split("/")[:2]))
                else:
                    production_packages.add(specifier.split("/")[0])

    exact_duplicates = [
        {"sha256": digest, "paths": [_relative(path) for path in paths]}
        for digest, paths in sorted(hashes.items())
        if len(paths) > 1
        and frozenset(_relative(path) for path in paths) not in APPROVED_DUPLICATE_GROUPS
    ]
    reachable = _reachable(graph, ENTRYPOINTS)
    unreachable = [
        _relative(path)
        for path in files
        if path.resolve() not in reachable and path.name != "__init__.py"
    ]
    package_contracts = [
        json.loads((ROOT / "RotoWeaveClient" / "package.json").read_text(encoding="utf-8")),
        json.loads((ROOT / "RotoWeaveServer" / "server-admin" / "package.json").read_text(encoding="utf-8")),
    ]
    declared_runtime = {
        name
        for contract in package_contracts
        for name in contract.get("dependencies", {})
    }
    unused_runtime = sorted(declared_runtime - production_packages)

    return {
        "schemaVersion": 1,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "workspace": str(ROOT),
        "scope": [_relative(path) for path in SOURCE_ROOTS],
        "summary": {
            "sourceFiles": len(files),
            "sourceLines": sum(
                path.read_text(encoding="utf-8").count("\n") + 1 for path in files
            ),
            "unresolvedLocalImports": len(unresolved),
            "unreachableModules": len(unreachable),
            "exactDuplicateGroups": len(exact_duplicates),
            "maintenanceMarkers": len(markers),
            "oversizedModules": len(oversized),
            "unusedRuntimeDependencies": len(unused_runtime),
            "retiredSourceRoots": len(retired_source_roots),
        },
        "unresolvedLocalImports": unresolved,
        "unreachableModules": unreachable,
        "exactDuplicateGroups": exact_duplicates,
        "maintenanceMarkers": markers,
        "oversizedModules": oversized,
        "unusedRuntimeDependencies": unused_runtime,
        "retiredSourceRoots": retired_source_roots,
        "notes": [
            "Unreachable is entrypoint graph evidence, not automatic deletion authority.",
            "Dynamic imports or externally loaded adapters require manual classification.",
            "Unresolved local imports, exact duplicate production files, and retired source roots are hard gate failures.",
        ],
    }


def _gate_failed(report: dict[str, Any]) -> bool:
    return bool(
        report["unresolvedLocalImports"]
        or report["exactDuplicateGroups"]
        or report["retiredSourceRoots"]
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit production source integrity without modifying it.")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--gate", action="store_true")
    args = parser.parse_args()
    try:
        report = audit()
        rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        if args.output:
            target = args.output.resolve(strict=False)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(rendered, encoding="utf-8")
        print(rendered, end="")
    except (OSError, UnicodeDecodeError, SyntaxError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    if args.gate and _gate_failed(report):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
