from __future__ import annotations

import shutil
import uuid
from pathlib import Path
from typing import Any, Callable

from .config import Settings
from .media import _create_thumbnail, extract_frames, probe_video
from .storage import sha256_file
from .workspace_format import (
    WorkspaceChangedError,
    WorkspaceFormatError,
    resolve_workspace_path,
)
from .workspace_repository import MEDIA_VIDEO_SUFFIXES, WorkspaceRepository


ProgressCallback = Callable[[str, float, str | None], None]
ControlCallback = Callable[[], None]


def _noop_progress(_: str, __: float, ___: str | None) -> None:
    return None


def _noop_control() -> None:
    return None


class MaterialLibrary:
    """Publish format-3 material sources without exposing staging as authority."""

    def __init__(
        self,
        repository: WorkspaceRepository,
        settings: Settings,
        runtime_root: Path,
    ) -> None:
        self.repository = repository
        self.settings = settings
        self.runtime_root = runtime_root.resolve(strict=False)

    def import_video(
        self,
        character_id: str,
        source_path: Path,
        display_name: str,
        *,
        target_fps: float | None = None,
        expected_revision_id: str | None = None,
        report: ProgressCallback = _noop_progress,
        check_control: ControlCallback = _noop_control,
    ) -> dict[str, Any]:
        source = source_path.resolve(strict=True)
        if not source.is_file() or source.suffix.lower() not in MEDIA_VIDEO_SUFFIXES:
            raise WorkspaceFormatError(
                "仅支持 MP4、MOV、AVI、WebM、MKV 或 M4V 视频素材。"
            )
        digest = sha256_file(source)
        duplicate = self.repository.find_material_source_by_content_hash(
            character_id, digest
        )
        if duplicate is not None:
            return {
                "source": duplicate,
                "duplicate": True,
                "report": {
                    "contentHash": digest,
                    "frameCount": len(duplicate.get("frames") or []),
                    "warnings": ["duplicate-content-reused"],
                },
            }

        source_id = f"msrc_{uuid.uuid4().hex}"
        stage_root = self.runtime_root / "material-import" / source_id
        stage_video = stage_root / f"source{source.suffix.lower()}"
        frame_root = stage_root / "frames"
        thumb_root = stage_root / "thumbnails"
        try:
            stage_root.mkdir(parents=True)
            shutil.copyfile(source, stage_video)
            if sha256_file(stage_video) != digest:
                raise WorkspaceChangedError("视频在导入期间发生变化，请重新导入。")
            metadata = probe_video(stage_video, self.settings)
            report("probe", 1.0, "视频元数据已验证。")
            extracted, extraction = extract_frames(
                stage_video,
                frame_root,
                thumb_root,
                metadata,
                target_fps,
                0.0,
                None,
                self.settings,
                report,
                check_control,
            )
            if sha256_file(stage_video) != digest or sha256_file(source) != digest:
                raise WorkspaceChangedError("视频在导入期间发生变化，请重新导入。")
            warnings = [str(item) for item in metadata.get("warnings", [])]
            for key in (
                "color_transfer",
                "color_primaries",
                "color_space",
                "color_range",
            ):
                if not metadata.get(key):
                    warnings.append(f"{key}-unspecified")
            first = extracted[0]
            domain_metadata = {
                "fps": float(extraction["fps"]),
                "durationSeconds": float(extraction["duration"]),
                "frameCount": len(extracted),
                "width": int(first["width"]),
                "height": int(first["height"]),
                "codec": metadata.get("codec"),
                "container": metadata.get("format"),
                "sourceFrameCount": int(metadata.get("frame_count") or 0),
                "sourceDurationSeconds": float(metadata.get("duration") or 0.0),
                "color": dict(first["source_color"]),
                "warnings": sorted(set(warnings)),
                "extractor": str(extraction["extractor"]),
                "sourceAuthority": str(extraction["source_authority"]),
            }
            frame_records = [
                {
                    "path": item["source_path"],
                    "linearPath": item["linear_source_path"],
                    "ptsUs": int(item["pts_us"]),
                    "durationUs": int(item["duration_us"]),
                    "width": int(item["width"]),
                    "height": int(item["height"]),
                }
                for item in extracted
            ]
            published = self.repository.publish_material_source(
                character_id,
                display_name or source.stem,
                str(stage_video),
                frame_records,
                domain_metadata,
                source_id=source_id,
                expected_revision_id=expected_revision_id,
            )
            concurrent_duplicate = published["id"] != source_id
            if not concurrent_duplicate:
                cache_root = self.runtime_root / "thumbnails" / "materials" / source_id
                cache_part = cache_root.with_name(
                    f".{cache_root.name}.part-{uuid.uuid4().hex}"
                )
                cache_part.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(thumb_root, cache_part)
                if cache_root.exists():
                    shutil.rmtree(cache_root)
                cache_part.replace(cache_root)
            return {
                "source": published,
                "duplicate": concurrent_duplicate,
                "report": {
                    "contentHash": digest,
                    "frameCount": len(extracted),
                    "warnings": [
                        *domain_metadata["warnings"],
                        *(["duplicate-content-reused"] if concurrent_duplicate else []),
                    ],
                    "extractor": domain_metadata["extractor"],
                },
            }
        finally:
            shutil.rmtree(stage_root, ignore_errors=True)

    def thumbnail_path(self, source_id: str, frame_index: int) -> Path:
        source = self.repository.get_material_source(source_id)
        if source is None:
            raise WorkspaceFormatError("素材源不存在。")
        frames = source.get("frames") or []
        if frame_index < 0 or frame_index >= len(frames):
            raise WorkspaceFormatError("源帧索引越界。")
        target = (
            self.runtime_root
            / "thumbnails"
            / "materials"
            / source_id
            / f"{frame_index:06d}.jpg"
        )
        if not target.is_file():
            frame_path = resolve_workspace_path(
                self.repository.root, str(frames[frame_index]["path"])
            )
            _create_thumbnail(frame_path, target)
        if not target.is_file():
            raise WorkspaceFormatError("源帧缩略图生成失败。")
        return target

    def remove_thumbnail_cache(self, source_id: str) -> None:
        target = self.runtime_root / "thumbnails" / "materials" / source_id
        shutil.rmtree(target, ignore_errors=True)
