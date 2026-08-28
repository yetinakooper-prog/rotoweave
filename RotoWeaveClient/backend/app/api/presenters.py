from __future__ import annotations

import re
from typing import Any


_WINDOWS_ABSOLUTE_PATH = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\)")


def _without_paths(value: dict[str, Any]) -> dict[str, Any]:
    def scrub(nested: Any) -> Any:
        if isinstance(nested, dict):
            result: dict[str, Any] = {}
            for key, item in nested.items():
                folded = key.casefold()
                if (
                    key in {"processing_project_id", "processing_source_id"}
                    or folded == "path"
                    or folded.endswith("_path")
                    or key.endswith("Path")
                ):
                    continue
                result[key] = scrub(item)
            return result
        if isinstance(nested, list):
            return [scrub(item) for item in nested]
        return nested

    return scrub(value)


def _without_runtime_locations(value: Any) -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, nested in value.items():
            folded = key.casefold()
            if folded.endswith("_directory") or folded in {"screen_models", "ai_masks"}:
                continue
            result[key] = _without_runtime_locations(nested)
        return result
    if isinstance(value, list):
        return [_without_runtime_locations(item) for item in value]
    if isinstance(value, str) and _WINDOWS_ABSOLUTE_PATH.match(value):
        return None
    return value


def _public_size_profile(profile: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in profile.items() if key != "normalized_name"}


def _public_job(job: dict[str, Any]) -> dict[str, Any]:
    raw_result = job.get("result") if isinstance(job.get("result"), dict) else {}
    raw_asset_hashes = raw_result.get("assetSha256ByFrame")
    result = _without_runtime_locations(_without_paths(job))
    result.pop("request", None)
    public_result = result.get("result") if isinstance(result.get("result"), dict) else None
    if isinstance(public_result, dict) and isinstance(raw_asset_hashes, dict):
        public_result["assetSha256ByFrame"] = {
            str(frame_id): {
                (str(kind)[:-5] if str(kind).casefold().endswith("_path") else str(kind)): digest
                for kind, digest in hashes.items()
            }
            for frame_id, hashes in raw_asset_hashes.items()
            if isinstance(hashes, dict)
        }
    return result
