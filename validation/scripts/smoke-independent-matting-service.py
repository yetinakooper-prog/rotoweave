from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

import cv2
import numpy as np


WORKSPACE = Path(__file__).resolve().parents[2]
sys.path[:0] = [
    str(WORKSPACE / "RotoWeaveClient"),
    str(WORKSPACE / "RotoWeaveContracts"),
]

from backend.app.config import Settings
from backend.app.remote_matting_client import (
    RemoteMattingClient,
    RemoteMattingConfig,
    prepare_remote_submission,
    publish_remote_result,
)
from backend.app.workspace_session import WorkspaceSessionManager


def _source(root: Path) -> tuple[object, dict[str, object]]:
    settings = Settings(data_root=root / "client-state", runtime_root=WORKSPACE / "RotoWeaveClient")
    session = WorkspaceSessionManager(settings)
    session.create(root / "workspace", "Independent Model Service Smoke")
    repository = session.require_repository()
    character = repository.create_domain_character("Smoke Hero")
    fixture = repository.root / "fixtures"
    fixture.mkdir()
    video = fixture / "source.mp4"
    video.write_bytes(b"independent-model-service-smoke")
    image = np.full((384, 384, 3), (0, 255, 0), dtype=np.uint8)
    cv2.rectangle(image, (112, 64), (272, 336), (40, 90, 220), thickness=-1)
    cv2.circle(image, (192, 90), 42, (60, 140, 245), thickness=-1)
    frame = fixture / "000000.png"
    if not cv2.imwrite(str(frame), image):
        raise RuntimeError("Unable to write the smoke input frame.")
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
        "Independent Model Smoke",
        video.relative_to(repository.root).as_posix(),
        [logical],
        metadata={
            "fps": 24.0,
            "durationSeconds": 1 / 24,
            "frameCount": 1,
            "width": 384,
            "height": 384,
            "color": {"transfer": "bt709", "primaries": "bt709", "matrix": "bt709", "range": "tv"},
            "warnings": [],
        },
        frame_metadata=metadata,
        expected_revision_id=repository.workspace_domain()["revisionId"],
    )
    return repository, source


async def run(
    endpoint: str, root: Path, timeout: float, quality: str
) -> dict[str, object]:
    repository, source = _source(root)
    upload = root / "client-state" / "upload.zip"
    prepared = prepare_remote_submission(
        repository,
        source["id"],
        quality,
        {"material_type": "character", "screen_color": "green"},
        upload,
        frame_indexes=[0],
    )
    deadline = time.monotonic() + timeout
    async with RemoteMattingClient(RemoteMattingConfig(endpoint)) as client:
        while True:
            status = await client.probe()
            if status.ready:
                break
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Service did not become ready: {status.model_dump(mode='json')}")
            await asyncio.sleep(0.5)
        submitted = await client.submit(prepared)
        while True:
            job = await client.status(submitted.jobId)
            if job.state.value in {"completed", "failed", "cancelled"}:
                break
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Matting job timed out: {submitted.jobId}")
            await asyncio.sleep(0.5)
        if job.state.value != "completed":
            raise RuntimeError(f"Matting job failed: {job.model_dump(mode='json')}")
        downloaded = await client.download_result(submitted.jobId, root / "client-state" / "result.zip")
    variant = publish_remote_result(
        repository,
        source["id"],
        downloaded,
        root / "client-state" / "publish-staging",
        expected_revision_id=repository.workspace_domain()["revisionId"],
        expected_source_frame_ids=[str(source["frames"][0]["id"])],
    )
    model = downloaded.manifest.model
    return {
        "schemaVersion": 1,
        "endpoint": endpoint,
        "jobId": submitted.jobId,
        "state": job.state.value,
        "quality": downloaded.manifest.quality.value,
        "frameCount": downloaded.manifest.frameCount,
        "archiveSha256": downloaded.manifest.archiveSha256,
        "configurationDigest": model.get("configurationDigest"),
        "recipe": model.get("recipe"),
        "runtimeDigest": model.get("runtimeDigest"),
        "models": model.get("models"),
        "result": str(root / "client-state" / "result.zip"),
        "publishedVariantId": variant["id"],
        "publishedQuality": variant["kind"],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--endpoint", default="http://127.0.0.1:18443")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=1800.0)
    parser.add_argument("--quality", choices=("high", "ultra"), required=True)
    args = parser.parse_args()
    root = args.root.resolve(strict=False)
    temp_root = (WORKSPACE / "Temp").resolve(strict=True)
    try:
        root.relative_to(temp_root)
    except ValueError as exc:
        raise RuntimeError("Smoke root must remain under workspace Temp.") from exc
    root.mkdir(parents=True, exist_ok=False)
    print(
        json.dumps(
            asyncio.run(run(args.endpoint, root, args.timeout, args.quality)),
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
