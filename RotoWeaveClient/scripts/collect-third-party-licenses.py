from __future__ import annotations

import importlib.metadata
import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath


WORKSPACE = Path(__file__).resolve().parent.parent
OUTPUT = WORKSPACE / "dist" / "RotoWeave" / "licenses"
PYTHON_LOCK = WORKSPACE / "requirements-win-lock.txt"
PACKAGE_LOCK = WORKSPACE / "package-lock.json"
LICENSE_PREFIXES = ("license", "licence", "copying", "notice")


def safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._") or "package"


def is_license_file(path: str | PurePosixPath) -> bool:
    return PurePosixPath(str(path)).name.lower().startswith(LICENSE_PREFIXES)


def reset_output() -> None:
    resolved = OUTPUT.resolve()
    expected_parent = (WORKSPACE / "dist" / "RotoWeave").resolve()
    if resolved.parent != expected_parent or resolved.name != "licenses":
        raise SystemExit(f"Refusing to replace unexpected license path: {resolved}")
    if resolved.exists():
        shutil.rmtree(resolved)
    resolved.mkdir(parents=True)


def python_requirement_names() -> list[str]:
    names: list[str] = []
    for raw_line in PYTHON_LOCK.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        name = re.split(r"[=;<>!~\[]", line, maxsplit=1)[0].strip()
        if name and name not in names:
            names.append(name)
    return names


def metadata_license(metadata: importlib.metadata.PackageMetadata) -> str | None:
    value = metadata.get("License-Expression") or metadata.get("License")
    if value and len(value.strip()) <= 300:
        return value.strip()
    classifiers = [
        item.removeprefix("License :: ").strip()
        for item in metadata.get_all("Classifier", [])
        if item.startswith("License :: ")
    ]
    return " | ".join(classifiers) or None


def collect_python() -> list[dict[str, object]]:
    inventory: list[dict[str, object]] = []
    root = OUTPUT / "python"
    root.mkdir()
    for requested_name in python_requirement_names():
        distribution = importlib.metadata.distribution(requested_name)
        metadata = distribution.metadata
        package_name = metadata.get("Name") or requested_name
        version = distribution.version
        declared_license = metadata_license(metadata)
        license_files = sorted(
            (item for item in distribution.files or [] if is_license_file(item)),
            key=lambda item: str(item).lower(),
        )
        if not declared_license and not license_files:
            raise SystemExit(
                f"Python package has neither license metadata nor license files: "
                f"{package_name} {version}"
            )

        package_root = root / f"{safe_name(package_name)}-{safe_name(version)}"
        package_root.mkdir()
        copied: list[str] = []
        for index, relative in enumerate(license_files, start=1):
            source = Path(distribution.locate_file(relative))
            if not source.is_file():
                continue
            target = package_root / f"{index:03d}-{safe_name(source.name)}"
            shutil.copy2(source, target)
            copied.append(target.relative_to(OUTPUT).as_posix())

        inventory.append(
            {
                "name": package_name,
                "version": version,
                "license": declared_license,
                "licenseFiles": copied,
                "projectUrls": metadata.get_all("Project-URL", []),
            }
        )
    return inventory


def collect_web() -> list[dict[str, object]]:
    package_lock = json.loads(PACKAGE_LOCK.read_text(encoding="utf-8"))
    packages = package_lock.get("packages", {})
    inventory: list[dict[str, object]] = []
    root = OUTPUT / "web"
    root.mkdir()
    for package_path, entry in sorted(packages.items()):
        if (
            not package_path.startswith("node_modules/")
            or not isinstance(entry, dict)
            or entry.get("dev") is True
        ):
            continue
        package_name = package_path.removeprefix("node_modules/")
        version = str(entry.get("version") or "")
        declared_license = entry.get("license")
        source_root = WORKSPACE / Path(package_path)
        license_files = sorted(
            (
                path
                for path in source_root.iterdir()
                if path.is_file() and is_license_file(path.name)
            ),
            key=lambda path: path.name.lower(),
        )
        if not declared_license or not license_files:
            raise SystemExit(
                f"Runtime web package has incomplete license evidence: "
                f"{package_name} {version}"
            )

        package_root = root / f"{safe_name(package_name)}-{safe_name(version)}"
        package_root.mkdir()
        copied: list[str] = []
        for index, source in enumerate(license_files, start=1):
            target = package_root / f"{index:03d}-{safe_name(source.name)}"
            shutil.copy2(source, target)
            copied.append(target.relative_to(OUTPUT).as_posix())

        inventory.append(
            {
                "name": package_name,
                "version": version,
                "license": str(declared_license),
                "licenseFiles": copied,
            }
        )
    return inventory


def main() -> None:
    reset_output()
    python_packages = collect_python()
    web_packages = collect_web()
    inventory = {
        "schemaVersion": 1,
        "createdAtUtc": datetime.now(timezone.utc).isoformat(),
        "scope": (
            "Python Windows lock environment and all non-dev packages included "
            "in the compiled web runtime"
        ),
        "python": python_packages,
        "web": web_packages,
        "separateNotices": [
            "models/LICENSE-BiRefNet.txt",
            "tools/ffmpeg/licenses/",
            "THIRD_PARTY_NOTICES.md",
        ],
    }
    (OUTPUT / "LICENSE-INVENTORY.json").write_text(
        json.dumps(inventory, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        "Third-party licenses collected: "
        f"{len(python_packages)} Python, {len(web_packages)} web runtime packages"
    )


if __name__ == "__main__":
    main()
