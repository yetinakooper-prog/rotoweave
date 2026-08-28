from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def _emit() -> None:
    import cv2
    import numpy as np

    from backend.app.basic_material_processor import process_basic_material
    from backend.app.config import Settings
    from backend.app.workspace_session import WorkspaceSessionManager
    from contracts.integrity import sha256_file

    with tempfile.TemporaryDirectory(prefix="rotoweave-basic-equivalence-") as temporary:
        root = Path(temporary)
        settings = Settings(data_root=root / "runtime", runtime_root=root / "runtime-source")
        session = WorkspaceSessionManager(settings)
        session.create(root / "workspace", "Physical split equivalence")
        repository = session.require_repository()
        character = repository.create_domain_character("Hero")
        image = np.full((32, 40, 3), (255, 0, 255), dtype=np.uint8)
        image[8:26, 12:30] = (0, 220, 220)
        frame = repository.root / "fixtures/000000.png"
        frame.parent.mkdir(parents=True, exist_ok=True)
        if not cv2.imwrite(str(frame), image):
            raise RuntimeError("fixture encoding failed")
        video = repository.root / "fixtures/source.mp4"
        video.write_bytes(b"physical-split-basic-equivalence")
        source = repository.create_material_source(
            character["id"],
            "character",
            video.relative_to(repository.root).as_posix(),
            [frame.relative_to(repository.root).as_posix()],
            metadata={
                "fps": 24.0,
                "durationSeconds": 1 / 24,
                "frameCount": 1,
                "width": 40,
                "height": 32,
                "color": {"transfer": "bt709", "primaries": "bt709", "matrix": "bt709", "range": "tv"},
                "warnings": [],
            },
            frame_metadata=[{
                "linearPath": frame.relative_to(repository.root).as_posix(),
                "ptsUs": 0,
                "durationUs": round(1_000_000 / 24),
                "width": 40,
                "height": 32,
            }],
            expected_revision_id=repository.workspace_domain()["revisionId"],
        )
        result = process_basic_material(
            repository,
            source["id"],
            Path(session.runtime_root) / "basic-output",
            {
                "quality": "basic",
                "material_type": "character",
                "ai_assist": False,
                "chroma": {
                    "screen_samples": [{"rgb": [255, 0, 255], "color_space": "srgb"}],
                    "key_mode": "preserve_subject_screen_color",
                },
            },
            settings,
            lambda *_: None,
            lambda: None,
            expected_revision_id=repository.workspace_domain()["revisionId"],
        )
        variant = repository.get_material_variant(result["variantId"])
        output = repository.root / variant["frames"][0]["path"]
        payload = {"sha256": sha256_file(output), "bytes": output.stat().st_size}
        session.shutdown()
        print(json.dumps(payload, sort_keys=True))


def _load(python_path: list[Path]) -> dict[str, object]:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join(str(path) for path in python_path)
    completed = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--emit"],
        capture_output=True,
        env=environment,
    )
    if completed.returncode != 0:
        diagnostic = completed.stderr.decode("utf-8", errors="replace")
        raise RuntimeError(f"Basic comparison subprocess failed:\n{diagnostic}")
    return json.loads(completed.stdout.decode("utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--emit", action="store_true")
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--workspace", type=Path, default=Path(__file__).resolve().parent.parent.parent)
    arguments = parser.parse_args()
    if arguments.emit:
        _emit()
        return 0
    if arguments.baseline is None:
        parser.error("--baseline is required")
    workspace = arguments.workspace.resolve()
    before = _load([arguments.baseline.resolve()])
    after = _load([workspace / "RotoWeaveClient", workspace / "RotoWeaveContracts"])
    equal = before == after
    print(f"basic: equal={str(equal).lower()} before={before} after={after}")
    return 0 if equal else 1


if __name__ == "__main__":
    raise SystemExit(main())
