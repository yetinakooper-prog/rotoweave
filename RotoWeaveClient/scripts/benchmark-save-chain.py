from __future__ import annotations

import json
import statistics
import sys
import tempfile
import time
from copy import deepcopy
from pathlib import Path
from types import MethodType

import cv2
import numpy as np

CLIENT_ROOT = Path(__file__).resolve().parents[1]
CONTRACTS_ROOT = CLIENT_ROOT.parent / "RotoWeaveContracts"
for root in (CLIENT_ROOT, CONTRACTS_ROOT):
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

import backend.app.workspace_repository as repository_module
from backend.app.config import Settings
from backend.app.workspace_session import WorkspaceSessionManager


def percentile(samples: list[float], index: int) -> float:
    return statistics.quantiles(samples, n=100, method="inclusive")[index - 1]


def frame_ref(variant: dict, index: int, duration: float) -> dict:
    return {
        "id": f"afrm-benchmark-{index:06d}",
        "variantId": variant["id"],
        "frameId": variant["frames"][0]["id"],
        "durationSeconds": duration,
        "enabled": True,
        "transform": {
            "position": {"x": 0, "y": 0},
            "scale": {"x": 1, "y": 1},
            "rotationDegrees": 0,
            "color": "#ffffff",
            "opacity": 1,
            "shadow": {
                "enabled": None,
                "color": None,
                "opacity": None,
                "offset": {"x": 0, "y": 0},
                "scale": {"x": 1, "y": 1},
            },
        },
    }


def run_count(root: Path, count: int) -> dict:
    session = WorkspaceSessionManager(Settings(data_root=root / "runtime", runtime_root=root))
    session.create(root / "workspace", f"Save benchmark {count}")
    repository = session.require_repository()
    character = repository.create_domain_character("Benchmark")
    fixture = repository.root / "fixtures"
    fixture.mkdir()
    video = fixture / "source.mp4"
    video.write_bytes(b"video")
    image_path = fixture / "frame.png"
    cv2.imwrite(str(image_path), np.full((8, 8, 4), 255, dtype=np.uint8))
    logical = image_path.relative_to(repository.root).as_posix()
    source = repository.create_material_source(
        character["id"],
        "Source",
        video.relative_to(repository.root).as_posix(),
        [logical],
        expected_revision_id=repository.workspace_domain()["revisionId"],
    )
    variant = repository.publish_material_variant(
        source["id"],
        "basic",
        [str(image_path)],
        {},
        expected_revision_id=repository.workspace_domain()["revisionId"],
    )
    action = repository.create_domain_action(
        character["id"], "Action", expected_revision_id=repository.workspace_domain()["revisionId"]
    )
    frames = [frame_ref(variant, index, 1 / 24) for index in range(count)]
    repository.replace_action_frame_refs(
        action["id"], frames, expected_revision_id=repository.workspace_domain()["revisionId"]
    )

    optimized_mutate = repository._mutate_domain
    old_snapshots: list[dict] = []

    def legacy_mutate(self, mutator, *, expected_revision_id=None):
        with self._lock:
            previous = self._domain(for_write=True)
            candidate = deepcopy(previous)
            mutator(candidate)
            preview, _ = repository_module.finalize_aggregate(candidate, previous=previous)
            repository_module.validate_workspace_domain(preview)
            saved, _ = self._write_aggregate(self._domain_path(), candidate, previous)
            self._asset_map(previous)
            self._asset_map(saved)
            old_snapshots.append(deepcopy(previous))
            if len(old_snapshots) > 10:
                old_snapshots.pop(0)
            self._discard_unreferenced_asset_state(saved)
            return saved

    phase = "setup"
    finalize_calls = {"before": 0, "after": 0}
    original_finalize = repository_module.finalize_aggregate

    def counted_finalize(*args, **kwargs):
        if phase in finalize_calls:
            finalize_calls[phase] += 1
        return original_finalize(*args, **kwargs)

    repository_module.finalize_aggregate = counted_finalize
    try:
        results: dict[str, dict] = {}
        for label, mutate in (("before", MethodType(legacy_mutate, repository)), ("after", optimized_mutate)):
            repository._mutate_domain = mutate
            samples: list[float] = []
            phase = label
            for index in range(23):
                next_frames = deepcopy(frames)
                next_frames[0]["durationSeconds"] = (1 / 24) + ((index % 2) * 0.0001)
                started = time.perf_counter()
                response = repository.replace_action_frame_refs(
                    action["id"],
                    next_frames,
                    expected_revision_id=repository.workspace_domain()["revisionId"],
                )
                elapsed = (time.perf_counter() - started) * 1000
                frames = next_frames
                if index >= 3:
                    samples.append(elapsed)
            results[label] = {
                "p50Ms": round(statistics.median(samples), 3),
                "p95Ms": round(percentile(samples, 95), 3),
                "finalizeCalls": finalize_calls[label],
                "samples": len(samples),
                "responseBytes": len(json.dumps(response, ensure_ascii=False).encode("utf-8")),
                "followupDomainGets": 1 if label == "before" else 0,
            }
        results["improvement"] = {
            "p50Percent": round((1 - results["after"]["p50Ms"] / results["before"]["p50Ms"]) * 100, 1),
            "p95Percent": round((1 - results["after"]["p95Ms"] / results["before"]["p95Ms"]) * 100, 1),
        }
        return {"frames": count, **results}
    finally:
        repository_module.finalize_aggregate = original_finalize


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="rotoweave-save-benchmark-") as temporary:
        root = Path(temporary)
        print(json.dumps({
            "schemaVersion": 1,
            "warmups": 3,
            "samples": 20,
            "results": [run_count(root / str(count), count) for count in (30, 100, 500, 2000)],
        }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
