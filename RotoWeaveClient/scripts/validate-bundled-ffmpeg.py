from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path


WORKSPACE = Path(__file__).resolve().parent.parent
FFMPEG_ROOT = WORKSPACE / "runtime" / "tools" / "ffmpeg"
MANIFEST_PATH = FFMPEG_ROOT / "SOURCE-MANIFEST.json"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(4 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def fail(message: str) -> None:
    raise SystemExit(f"Bundled FFmpeg validation failed: {message}")


def validate_declared_files(
    manifest: dict[str, object],
    section: str,
    *,
    reject_undeclared: bool,
) -> None:
    raw_entries = manifest.get(section)
    if not isinstance(raw_entries, list) or not raw_entries:
        fail(f"{section} must be a non-empty list")

    declared: set[str] = set()
    for raw_entry in raw_entries:
        if not isinstance(raw_entry, dict):
            fail(f"{section} contains a non-object entry")
        relative = raw_entry.get("path")
        expected_size = raw_entry.get("bytes")
        expected_sha = raw_entry.get("sha256")
        if (
            not isinstance(relative, str)
            or not isinstance(expected_size, int)
            or not isinstance(expected_sha, str)
        ):
            fail(f"{section} contains an invalid file declaration")

        normalized = Path(relative).as_posix()
        if normalized in declared:
            fail(f"duplicate declaration: {normalized}")
        declared.add(normalized)

        path = (FFMPEG_ROOT / relative).resolve()
        try:
            path.relative_to(FFMPEG_ROOT.resolve())
        except ValueError:
            fail(f"path escapes FFmpeg root: {relative}")
        if not path.is_file():
            fail(f"missing file: {relative}")
        if path.stat().st_size != expected_size:
            fail(f"size mismatch: {relative}")
        if sha256_file(path) != expected_sha.lower():
            fail(f"SHA-256 mismatch: {relative}")

    if reject_undeclared:
        actual = {
            path.relative_to(FFMPEG_ROOT).as_posix()
            for path in (FFMPEG_ROOT / "bin").iterdir()
            if path.is_file()
        }
        if actual != declared:
            missing = sorted(declared - actual)
            extra = sorted(actual - declared)
            fail(f"binary inventory mismatch; missing={missing}, extra={extra}")


def validate_executable(
    executable: Path,
    expected_version: str,
    required_flags: list[str],
    forbidden_flags: list[str],
    *,
    allow_execution_policy_waiver: bool,
) -> None:
    try:
        completed = subprocess.run(
            [str(executable), "-version"],
            cwd=executable.parent,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
        )
    except OSError as exc:
        if allow_execution_policy_waiver and getattr(exc, "winerror", None) == 4551:
            print(
                f"WARNING: {executable.name} execution waived after Windows "
                "application-control policy block; inventory and SHA-256 remain enforced."
            )
            return
        raise
    policy_block_codes = {-1058471934, 3236495362}
    if allow_execution_policy_waiver and completed.returncode in policy_block_codes:
        print(
            f"WARNING: {executable.name} execution waived after Windows "
            "Code Integrity policy block; inventory and SHA-256 remain enforced."
        )
        return
    output = f"{completed.stdout}\n{completed.stderr}"
    if completed.returncode != 0:
        fail(f"{executable.name} -version exited with {completed.returncode}")
    if f"{executable.stem} version {expected_version}" not in output:
        fail(f"{executable.name} version does not match SOURCE-MANIFEST.json")
    for flag in required_flags:
        if flag not in output:
            fail(f"{executable.name} is missing required configure flag {flag}")
    for flag in forbidden_flags:
        if flag in output:
            fail(f"{executable.name} contains forbidden configure flag {flag}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--allow-execution-policy-waiver", action="store_true")
    args = parser.parse_args()
    if not MANIFEST_PATH.is_file():
        fail(f"missing manifest: {MANIFEST_PATH}")
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    if manifest.get("schemaVersion") != 1:
        fail("unsupported SOURCE-MANIFEST schemaVersion")
    if manifest.get("license") != "LGPL-3.0-or-later":
        fail("license must be LGPL-3.0-or-later")
    if manifest.get("distributionShape") != "shared":
        fail("only the audited shared distribution is allowed")

    validate_declared_files(manifest, "files", reject_undeclared=True)
    validate_declared_files(manifest, "licenses", reject_undeclared=False)

    configuration = manifest.get("configuration")
    if not isinstance(configuration, dict):
        fail("configuration must be an object")
    required = configuration.get("required")
    forbidden = configuration.get("forbidden")
    if (
        not isinstance(required, list)
        or not all(isinstance(item, str) for item in required)
        or not isinstance(forbidden, list)
        or not all(isinstance(item, str) for item in forbidden)
    ):
        fail("configuration flags must be string arrays")

    expected_version = manifest.get("version")
    if not isinstance(expected_version, str):
        fail("version must be a string")
    for name in ("ffmpeg.exe", "ffprobe.exe"):
        validate_executable(
            FFMPEG_ROOT / "bin" / name,
            expected_version,
            required,
            forbidden,
            allow_execution_policy_waiver=args.allow_execution_policy_waiver,
        )

    print(
        "Bundled FFmpeg validated: "
        f"{expected_version}, LGPL shared, {len(manifest['files'])} binaries"
    )


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, json.JSONDecodeError, subprocess.SubprocessError) as exc:
        fail(str(exc))
