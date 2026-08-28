from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import cv2
import numpy as np


WORKSPACE = Path(__file__).resolve().parents[2]
sys.path[:0] = [
    str(WORKSPACE / "RotoWeaveClient"),
    str(WORKSPACE / "RotoWeaveContracts"),
]

from backend.app.basic_material_processor import process_basic_material
from backend.app.config import Settings
from backend.app.workspace_session import WorkspaceSessionManager


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    root = args.root.resolve(strict=False)
    temp_root = (WORKSPACE / "Temp").resolve(strict=True)
    try:
        root.relative_to(temp_root)
    except ValueError as exc:
        raise RuntimeError("Smoke root must remain under workspace Temp.") from exc
    root.mkdir(parents=True, exist_ok=False)

    settings = Settings(
        data_root=root / "client-state",
        runtime_root=WORKSPACE / "RotoWeaveClient",
    )
    session = WorkspaceSessionManager(settings)
    session.create(root / "workspace", "Basic Model Smoke")
    repository = session.require_repository()
    character = repository.create_domain_character("Basic Smoke Hero")
    fixture = repository.root / "fixtures"
    fixture.mkdir()
    video = fixture / "source.mp4"
    video.write_bytes(b"basic-model-smoke")
    image = np.full((384, 384, 3), (0, 255, 0), dtype=np.uint8)
    for row in range(3):
        for column in range(4):
            center = (72 + column * 80, 96 + row * 96)
            cv2.circle(image, center, 22, (40, 90, 220), thickness=-1)
    frame = fixture / "000000.png"
    if not cv2.imwrite(str(frame), image):
        raise RuntimeError("Unable to write the Basic smoke input frame.")
    logical = frame.relative_to(repository.root).as_posix()
    metadata = [{
        "linearPath": logical,
        "ptsUs": 0,
        "durationUs": 41_667,
        "width": 384,
        "height": 384,
    }]
    source = repository.create_material_source(
        character["id"],
        "Basic Model Smoke",
        video.relative_to(repository.root).as_posix(),
        [logical],
        metadata={
            "fps": 24.0,
            "durationSeconds": 1 / 24,
            "frameCount": 1,
            "width": 384,
            "height": 384,
            "color": {
                "transfer": "bt709",
                "primaries": "bt709",
                "matrix": "bt709",
                "range": "tv",
            },
            "warnings": [],
        },
        frame_metadata=metadata,
        expected_revision_id=repository.workspace_domain()["revisionId"],
    )
    result = process_basic_material(
        repository,
        source["id"],
        root / "generation",
        {
            "quality": "basic",
            "material_type": "character",
            "ai_assist": True,
            "chroma": {
                "screen_samples": [{"rgb": [0, 255, 0], "color_space": "srgb"}],
                "key_mode": "clean_screen",
            },
        },
        settings,
        lambda *_: None,
        lambda: None,
        expected_revision_id=repository.workspace_domain()["revisionId"],
        frame_indexes=[0],
    )
    variant = repository.get_material_variant(result["variantId"])
    if variant is None:
        raise RuntimeError("Basic smoke result was not published.")
    print(json.dumps({
        "schemaVersion": 1,
        "workspace": str(repository.root),
        "sourceId": source["id"],
        "variantId": variant["id"],
        "quality": variant["kind"],
        "frameCount": len(variant["frames"]),
        "model": result["model"],
        "warnings": result["warnings"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
