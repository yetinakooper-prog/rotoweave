from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
TEXT_SUFFIXES = {
    ".asmdef", ".bat", ".cmd", ".cs", ".css", ".html", ".ini", ".js",
    ".json", ".jsx", ".lock", ".md", ".mjs", ".ps1", ".py", ".shader",
    ".spec", ".svg", ".toml", ".ts", ".tsx", ".txt", ".xml", ".yaml", ".yml",
}
EXCLUDED_PARTS = {
    ".svn", ".venv", "node_modules", "runtime", "server-runtimes", "release",
    "dist", "Temp", "__pycache__", ".pytest_cache", ".mypy_cache",
}
HISTORICAL_PREFIXES = (
    "docs/Tasks/",
    "docs/Architecture/Decisions/",
    "docs/Evidence/",
    "docs/MATTEBENCH.md",
    "reports/",
)
COMPATIBILITY_FILES = {
    "validation/scripts/validate-brand-boundary.py",
    "RotoWeaveContracts/contracts/brand_migration.py",
    "RotoWeaveContracts/contracts/legacy_compat.py",
    "RotoWeaveContracts/contracts/remote_archive.py",
    "RotoWeaveClient/backend/app/main.py",
    "RotoWeaveClient/backend/app/workspace_format.py",
    "RotoWeaveClient/app/client-shell-v4.tsx",
    "RotoWeaveClient/Start.ps1",
    "RotoWeaveServer/Start.ps1",
    "RotoWeaveServer/Download-Models.ps1",
    "RotoWeaveServer/server/api.py",
    "RotoWeaveServer/scripts/prepare-server-runtimes.py",
    "RotoWeaveClient/unity/RotoWeave-UnityImporter/Assets/RotoWeave/Editor/RotoWeaveCharacterImporter.cs",
    "scripts/rotoweave_bootstrap.py",
    "scripts/Setup-RotoWeave.ps1",
}
OLD_BRAND = re.compile(
    r"AIFrameTools|AIFrameTool|AIFrame(?:Client|Server|Contracts|Models)|"
    r"AIFRAME_|X-AIFrame-|aiframe:|aiframe\.json|\.aifcharacter|aiframe[_-]",
    re.IGNORECASE,
)


def allowed(relative: str) -> bool:
    if relative in COMPATIBILITY_FILES:
        return True
    if any(relative.startswith(prefix) for prefix in HISTORICAL_PREFIXES):
        return True
    parts = relative.split("/")
    return "tests" in parts or any(part.startswith("test_") for part in parts)


def main() -> int:
    issues: list[str] = []
    for path in ROOT.rglob("*"):
        relative = path.relative_to(ROOT).as_posix()
        if any(part in EXCLUDED_PARTS for part in path.relative_to(ROOT).parts):
            continue
        if OLD_BRAND.search(relative) and not allowed(relative):
            issues.append(f"旧品牌路径未列入允许清单：{relative}")
        if not path.is_file() or path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        if allowed(relative):
            continue
        try:
            text = path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError):
            continue
        match = OLD_BRAND.search(text)
        if match:
            line = text.count("\n", 0, match.start()) + 1
            issues.append(f"旧品牌文本未列入允许清单：{relative}:{line}")
    if issues:
        raise SystemExit("\n".join(sorted(set(issues))))
    print("RotoWeave brand boundary: passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
