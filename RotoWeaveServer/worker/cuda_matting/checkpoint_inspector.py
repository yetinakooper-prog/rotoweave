from __future__ import annotations

import argparse
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from contracts.integrity import canonical_sha256, sha256_file
from contracts.model_compatibility import (
    MODEL_COMPATIBILITY_POLICY_DIGEST,
    role_accepts_extension,
)


_SAFE_PRIMITIVES = (str, int, float, bool, bytes, type(None))


def _pytorch_observation(path: Path) -> list[dict[str, Any]]:
    import torch

    try:
        value = torch.load(path, map_location="cpu", weights_only=True, mmap=True)
    except TypeError:
        value = torch.load(path, map_location="cpu", weights_only=True)
    tensors: list[dict[str, Any]] = []

    def walk(item: Any, key: str) -> None:
        if isinstance(item, torch.Tensor):
            tensors.append(
                {
                    "key": key or "<root>",
                    "shape": list(item.shape),
                    "dtype": str(item.dtype),
                }
            )
            return
        if isinstance(item, Mapping):
            for child_key in sorted(item, key=lambda candidate: str(candidate)):
                if not isinstance(child_key, _SAFE_PRIMITIVES):
                    raise ValueError("checkpoint contains a non-primitive mapping key")
                walk(item[child_key], f"{key}.{child_key}" if key else str(child_key))
            return
        if isinstance(item, Sequence) and not isinstance(item, (str, bytes, bytearray)):
            for index, child in enumerate(item):
                walk(child, f"{key}[{index}]")
            return
        if isinstance(item, _SAFE_PRIMITIVES):
            return
        raise ValueError(f"checkpoint contains unsupported value type: {type(item).__name__}")

    walk(value, "")
    if not tensors:
        raise ValueError("checkpoint does not contain tensors")
    return sorted(tensors, key=lambda item: item["key"])


def _safetensors_observation(path: Path) -> list[dict[str, Any]]:
    from safetensors import safe_open

    tensors: list[dict[str, Any]] = []
    with safe_open(path, framework="pt", device="cpu") as stream:
        for key in sorted(stream.keys()):
            value = stream.get_slice(key)
            tensors.append(
                {
                    "key": key,
                    "shape": list(value.get_shape()),
                    "dtype": str(value.get_dtype()),
                }
            )
    if not tensors:
        raise ValueError("safetensors container does not contain tensors")
    return tensors


def inspect_checkpoint(role: str, path: Path) -> dict[str, Any]:
    resolved = path.resolve(strict=True)
    if not role_accepts_extension(role, resolved.suffix):
        raise ValueError("container extension is not allowed for this role")
    if resolved.suffix.casefold() == ".safetensors":
        container = "safetensors"
        tensors = _safetensors_observation(resolved)
    else:
        container = "pytorch-weights-only"
        tensors = _pytorch_observation(resolved)
    observation = {
        "container": container,
        "tensorCount": len(tensors),
        "tensors": tensors,
    }
    return {
        "state": "passed",
        "role": role,
        "path": str(resolved),
        "bytes": resolved.stat().st_size,
        "sha256": sha256_file(resolved),
        "compatibilityPolicyDigest": MODEL_COMPATIBILITY_POLICY_DIGEST,
        "observation": observation,
        "observationDigest": canonical_sha256(observation),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--role", required=True)
    parser.add_argument("--path", required=True)
    args = parser.parse_args()
    try:
        result = inspect_checkpoint(args.role, Path(args.path))
    except BaseException as exc:
        result = {
            "state": "failed",
            "role": args.role,
            "compatibilityPolicyDigest": MODEL_COMPATIBILITY_POLICY_DIGEST,
            "error": str(exc),
        }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["state"] == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
