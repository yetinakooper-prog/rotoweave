from __future__ import annotations

import argparse
import json
import shutil
import stat
import sys
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WORKSPACE))
from contracts.model_runtime_recipe import runtime_recipe


ALLOWED_OUTPUT_ROOT = (WORKSPACE / "Temp").resolve()
FORBIDDEN_WEIGHT_SUFFIXES = {".pt", ".pth", ".safetensors", ".ckpt"}


def _assert_regular_tree(root: Path) -> Path:
    resolved = root.resolve(strict=True)
    if not resolved.is_dir() or resolved.is_symlink():
        raise RuntimeError(f"Runtime input is not a regular directory: {root}")
    for path in resolved.rglob("*"):
        attributes = int(getattr(path.stat(follow_symlinks=False), "st_file_attributes", 0))
        if path.is_symlink() or attributes & int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)):
            raise RuntimeError(f"Runtime input contains a link/reparse point: {path}")
    return resolved


def _copy(source: Path, target: Path) -> None:
    shutil.copytree(_assert_regular_tree(source), target, copy_function=shutil.copy2)


def _remove_development_path_files(runtime_root: Path) -> None:
    for path in runtime_root.rglob("*.pth"):
        if "site-packages" not in {part.casefold() for part in path.parts}:
            continue
        name = path.name.casefold()
        if name == "distutils-precedence.pth" or name.startswith("__editable__."):
            path.unlink()


def _remove_predecessor_product_runtime(runtime_root: Path) -> None:
    retired = (
        runtime_root / ".aiframe-matting4090-ready.json",
        runtime_root / "Lib" / "site-packages" / "aiframe_matting4090_worker",
    )
    for path in retired:
        if path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()


def prepare(output: Path) -> dict[str, object]:
    target = output.resolve(strict=False)
    try:
        target.relative_to(ALLOWED_OUTPUT_ROOT)
    except ValueError as exc:
        raise RuntimeError("Server runtime staging must remain under workspace Temp.") from exc
    if target == ALLOWED_OUTPUT_ROOT:
        raise RuntimeError("Server runtime staging cannot target the Temp root itself.")
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)

    product_runtimes = _assert_regular_tree(WORKSPACE / "server-runtimes")
    for profile in ("high", "ultra"):
        profile_root = target / profile
        source_profile = product_runtimes / profile
        _copy(source_profile / "runtime", profile_root / "runtime")
        _remove_development_path_files(profile_root / "runtime")
        _remove_predecessor_product_runtime(profile_root / "runtime")
        _copy(source_profile / "sources", profile_root / "sources")
        manifest = runtime_recipe(profile)
        (profile_root / "runtime-manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        python = profile_root / str(manifest["pythonRelativePath"])
        if not python.is_file():
            raise RuntimeError(f"{profile} fixed runtime Python is missing: {python}")

    forbidden = sorted(
        path.relative_to(target).as_posix()
        for path in target.rglob("*")
        if path.is_file() and path.suffix.casefold() in FORBIDDEN_WEIGHT_SUFFIXES
    )
    if forbidden:
        raise RuntimeError("Server runtime staging contains model weights: " + ", ".join(forbidden))
    return {
        "schemaVersion": 1,
        "root": str(target),
        "profiles": ["high", "ultra"],
        "modelWeightsIncluded": False,
        "source": "RotoWeaveServer/server-runtimes",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=WORKSPACE / "Temp" / "ServerRuntimes")
    args = parser.parse_args()
    print(json.dumps(prepare(args.output), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
