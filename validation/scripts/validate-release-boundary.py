from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


WORKSPACE = Path(__file__).resolve().parent.parent.parent
PREGENERATED_BASIC_FILES = {
    "birefnet-lite-matting-selftest.npz",
    "birefnet-lite-matting.manifest.json",
    "birefnet-lite-matting.onnx",
}
FORBIDDEN_INTEGRATED_MODEL_METADATA = {
    "model-pack.json",
    "model-pack.sig.json",
    "self-test-receipt.json",
}
TWO_ROUTE_SOURCE_ROOTS = (
    "RotoWeaveClient/backend/app",
    "RotoWeaveServer/worker/cuda_matting",
    "RotoWeaveClient/app",
    "RotoWeaveClient/scripts",
    "validation/benchmarks",
)
FORBIDDEN_RETIRED_MATTE_TOKENS = (
    "complex_target",
    "complex_background",
    "MattePrompt",
    "target_constraint_required",
    "UncertaintyReason.TARGET",
    "UncertaintyFlag.TARGET",
)


class ReleaseBoundaryError(RuntimeError):
    pass


def _text(relative: str) -> str:
    return (WORKSPACE / relative).read_text(encoding="utf-8")


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ReleaseBoundaryError(message)


def validate_sources() -> dict[str, Any]:
    product = json.loads(_text("RotoWeaveContracts/product.json"))
    contracts = product.get("contracts") or {}
    _require(product.get("version") == "4.0.0", "Release scripts require product 4.0.0.")
    _require(contracts.get("httpApi") == 4, "Release scripts require HTTP API v4.")
    _require(
        contracts.get("characterPackageShape") == "deduplicated-atlas-v3",
        "Release scripts require deduplicated-atlas-v3.",
    )

    retired_scope_hits: list[str] = []
    for relative_root in TWO_ROUTE_SOURCE_ROOTS:
        root = WORKSPACE / relative_root
        for path in root.rglob("*"):
            if path.resolve() == Path(__file__).resolve():
                continue
            if not path.is_file() or path.suffix.lower() not in {
                ".py",
                ".ts",
                ".tsx",
                ".js",
                ".mjs",
                ".json",
                ".md",
                ".ps1",
            }:
                continue
            content = path.read_text(encoding="utf-8")
            for token in FORBIDDEN_RETIRED_MATTE_TOKENS:
                if token in content:
                    retired_scope_hits.append(
                        f"{path.relative_to(WORKSPACE).as_posix()}:{token}"
                    )
    _require(
        not retired_scope_hits,
        "Production sources retain the retired matte route: "
        + ", ".join(retired_scope_hits[:10]),
    )

    spec = _text("RotoWeaveClient/RotoWeaveClient.spec")
    _require(
        '(str(release / "models"), "models")' not in spec,
        "PyInstaller spec must not collect an entire models directory.",
    )
    for filename in PREGENERATED_BASIC_FILES:
        _require(filename not in spec, f"PyInstaller spec must not embed generated Basic asset {filename}.")
    for relative in (
        "RotoWeaveContracts/basic-assets.json",
        "RotoWeaveClient/requirements-basic-export-lock.txt",
        "RotoWeaveClient/scripts/export-birefnet-onnx.py",
    ):
        _require((WORKSPACE / relative).is_file(), f"Basic source-build input is missing: {relative}.")
    basic_contract = json.loads(_text("RotoWeaveContracts/basic-assets.json"))
    _require(
        str(basic_contract.get("licenseUrl") or "").startswith("https://")
        and int(basic_contract.get("licenseBytes") or 0) > 0
        and len(str(basic_contract.get("licenseSha256") or "")) == 64,
        "Basic public contract must fetch and verify its upstream license at setup time.",
    )

    build = _text("validation/scripts/build-windows-launchers.ps1")
    for forbidden in (
        "--require-ultra",
        "$vitMatteModel",
        "$vitMatteLicense",
        "$raftModel",
        "$raftLicense",
    ):
        _require(forbidden not in build, f"Windows build retains retired contract: {forbidden}.")
    _require(
        "validate-release-boundary.py" in build,
        "Windows build must run the client/independent-model boundary gate.",
    )
    _require(
        "validate-launcher-packages.py" in build
        and "--distpath $stagingRoot" in build
        and "Move-Item -LiteralPath $stagedClientRoot" in build,
        "Windows build must validate a staged client before atomic promotion.",
    )

    sample_builder = _text("RotoWeaveClient/scripts/build-sample-character.py")
    _require(
        'f"SampleHero-{SAMPLE_PRODUCT_LINE}.rotoweave"' in sample_builder,
        "Sample character must use the current product-line filename.",
    )
    license_collector = _text("RotoWeaveClient/scripts/collect-third-party-licenses.py")
    _require(
        "LICENSE-ViTMatte.txt" not in license_collector
        and "LICENSE-RAFT.txt" not in license_collector,
        "Application license inventory still claims retired ViTMatte-S/RAFT assets.",
    )
    _require(
        "externalModelPack" not in license_collector,
        "Application license inventory must not retain integrated-model metadata.",
    )

    _require(
        not (WORKSPACE / "THIRD_PARTY_NOTICES.md").exists(),
        "The public source checkout must not expose the internal third-party notice document.",
    )
    # Setup intentionally generates Basic into the ignored local model runtime.
    # Release separation is enforced by the PyInstaller inputs above and by the
    # compiled-distribution inspection below, not by forbidding a usable local
    # installation in the working copy.
    return {
        "productVersion": product["version"],
        "applicationModelFiles": [],
        "basicArtifactPolicy": "build-from-pinned-source",
        "integratedModelArtifactsIncluded": False,
        "serverIndependentModelsBundledWithClient": False,
        "matteRoutes": ["chroma_character", "emissive_vfx"],
    }


def validate_dist(dist_root: Path) -> dict[str, Any]:
    root = dist_root.resolve()
    if not root.is_dir():
        raise ReleaseBoundaryError(f"Compiled application directory is missing: {root}")
    internal = root / "_internal"
    runtime_root = internal if internal.is_dir() else root
    if internal.is_dir() and (root / "models").exists():
        raise ReleaseBoundaryError(
            "Compiled application duplicates runtime models outside PyInstaller _internal."
        )
    models = runtime_root / "models"
    actual_models = {
        path.relative_to(models).as_posix()
        for path in models.rglob("*")
        if path.is_file()
    } if models.is_dir() else set()
    if actual_models:
        raise ReleaseBoundaryError(
            "Compiled client must not embed generated Basic or Server models: "
            + ", ".join(sorted(actual_models))
        )
    ffmpeg_bin = runtime_root / "tools" / "ffmpeg" / "bin"
    ffmpeg_runtime_names = {
        path.name.lower()
        for path in ffmpeg_bin.glob("*.dll")
        if path.is_file()
    }
    duplicate_ffmpeg_runtime = sorted(
        path.name
        for path in runtime_root.glob("*.dll")
        if path.name.lower() in ffmpeg_runtime_names
    )
    if duplicate_ffmpeg_runtime:
        raise ReleaseBoundaryError(
            "Compiled application duplicates FFmpeg runtime DLLs at its runtime root: "
            + ", ".join(duplicate_ffmpeg_runtime)
        )

    forbidden_paths: list[str] = []
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        lower_parts = {part.lower() for part in path.relative_to(root).parts}
        if path.name.lower() in FORBIDDEN_INTEGRATED_MODEL_METADATA:
            forbidden_paths.append(relative)
        elif "model-packs" in lower_parts or "cuda-matting-worker" in lower_parts:
            forbidden_paths.append(relative)
        elif path.is_dir() and path.name.lower() in {"torch", "torchvision"}:
            forbidden_paths.append(relative)
        elif path.is_file() and path.suffix.lower() in {".pt", ".pth", ".safetensors"}:
            forbidden_paths.append(relative)
    if forbidden_paths:
        raise ReleaseBoundaryError(
            "Compiled client contains forbidden Server model artifacts: "
            + ", ".join(sorted(forbidden_paths)[:10])
        )
    return {
        "distRoot": str(root),
        "runtimeRoot": str(runtime_root),
        "pyInstallerLayout": "onedir-internal" if runtime_root == internal else "flat",
        "applicationModelFiles": [],
        "basicArtifactPolicy": "build-from-pinned-source",
        "duplicateFfmpegRuntimeFiles": [],
        "integratedModelArtifactsIncluded": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Validate that the RotoWeave 4.0 client embeds no generated Basic "
            "or Server model assets and keeps the pinned source-build inputs."
        )
    )
    parser.add_argument(
        "--dist-root",
        type=Path,
        help="Optionally inspect a compiled dist/RotoWeave directory.",
    )
    args = parser.parse_args()
    try:
        result: dict[str, Any] = {"sources": validate_sources()}
        if args.dist_root is not None:
            result["compiledApplication"] = validate_dist(args.dist_root)
    except (OSError, json.JSONDecodeError, ReleaseBoundaryError) as exc:
        print(f"ERROR: {exc}")
        return 1
    print(json.dumps({"passed": True, **result}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
