from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import shutil
import stat
import threading
import uuid
import zipfile
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable

from contracts.product import (
    CANONICAL_PIXELS_PER_UNIT,
    CHARACTER_PACKAGE_FORMAT,
    CHARACTER_PACKAGE_SHAPE,
)
from .repositories.common import new_id, normalized_display_name, utc_now
from .runtime_repository import RuntimeRepository
from .workspace_format import (
    WORKSPACE_DOMAIN,
    WORKSPACE_MANIFEST,
    assert_no_case_duplicates,
    WorkspaceChangedError,
    WorkspaceFormatError,
    WorkspaceReadOnlyError,
    WorkspaceRevisionConflict,
    atomic_write_json,
    create_workspace,
    finalize_aggregate,
    is_reparse_point,
    logical_workspace_path,
    read_json,
    resolve_workspace_path,
    sha256_file,
    validate_logical_path,
    validate_aggregate,
    validate_stable_id,
    validate_workspace_domain,
    validate_workspace_manifest,
)


MAX_DELIVERY_METADATA_BYTES = 32 * 1024 * 1024
MEDIA_VIDEO_SUFFIXES = {".avi", ".m4v", ".mkv", ".mov", ".mp4", ".webm"}
LOGGER = logging.getLogger(__name__)
def validate_character_animation_timings(manifest: dict[str, Any]) -> None:
    """Fail fast when package metadata cannot reproduce the saved frame timing."""

    animations = manifest.get("animations")
    if not isinstance(animations, list) or not animations:
        raise ValueError("Unity 角色包没有可导入动画。")
    for animation in animations:
        if not isinstance(animation, dict):
            raise ValueError("Unity 角色包动画记录无效。")
        name = str(animation.get("displayName") or animation.get("id") or "未命名动画")
        frames = animation.get("frames")
        if not isinstance(frames, list) or not frames:
            raise ValueError(f"Unity 角色包动画没有启用帧：{name}。")
        try:
            duration_seconds = float(animation["durationSeconds"])
            frame_rate = float(animation["frameRate"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"Unity 角色包动画时长元数据无效：{name}。") from exc
        if (
            not math.isfinite(duration_seconds)
            or duration_seconds <= 0
            or duration_seconds > 3600
            or not math.isfinite(frame_rate)
            or frame_rate <= 0
        ):
            raise ValueError(f"Unity 角色包动画时长元数据无效：{name}。")
        frame_durations: list[float] = []
        for expected_index, frame in enumerate(frames):
            if not isinstance(frame, dict) or frame.get("index") != expected_index:
                raise ValueError(f"Unity 角色包动画帧顺序无效：{name}。")
            try:
                frame_duration = float(frame["durationSeconds"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"Unity 角色包动画帧时长无效：{name}。") from exc
            if not math.isfinite(frame_duration) or frame_duration <= 0:
                raise ValueError(f"Unity 角色包动画帧时长无效：{name}。")
            frame_durations.append(frame_duration)
        accumulated = math.fsum(frame_durations)
        if abs(accumulated - duration_seconds) > 0.0005:
            raise ValueError(f"Unity 角色包动画累计帧时长与总时长不一致：{name}。")
        resolved_frame_rate = len(frames) / duration_seconds
        if abs(frame_rate - resolved_frame_rate) > 0.001:
            raise ValueError(f"Unity 角色包动画平均帧率与总时长不一致：{name}。")


def recover_interrupted_delivery(
    root: Path, runtime_root: Path, workspace_id: str
) -> None:
    marker = runtime_root / "delivery-pending.json"
    if not marker.is_file():
        return
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
        validate_stable_id(payload.get("characterId"), "角色")
        target = resolve_workspace_path(root, payload["target"])
        character_path = resolve_workspace_path(root, payload["character"])
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError, WorkspaceFormatError):
        # Runtime recovery metadata is disposable and cannot authorize writes
        # when it is malformed. Workspace validation remains authoritative.
        marker.unlink(missing_ok=True)
        return
    if str(payload.get("workspaceId") or "") != workspace_id:
        marker.unlink(missing_ok=True)
        return
    character, _ = read_json(character_path)
    committed = (
        target.is_file()
        and str(character.get("delivery_sha256") or "") == sha256_file(target)
        and str(character.get("delivery_sha256") or "") == str(payload["newSha256"])
    )
    rollback = (
        runtime_root
        / "delivery"
        / str(payload["characterId"])
        / "latest.rollback.rotoweave"
    )
    if not committed:
        # If either filesystem operation fails, keep both the marker and any
        # rollback copy so the next open can retry without losing the previous
        # successful delivery.
        target.unlink(missing_ok=True)
        if rollback.is_file():
            rollback.replace(target)
    rollback.unlink(missing_ok=True)
    marker.unlink(missing_ok=True)


def validate_unity_delivery_archive(path: Path) -> None:
    """Verify the complete Unity delivery contract before replacing latest."""

    try:
        with zipfile.ZipFile(path, "r") as archive:
            entries = archive.infolist()
            names = [entry.filename for entry in entries]
            if len(names) != len(set(names)):
                raise ValueError("Unity 角色包包含重复文件。")
            assert_no_case_duplicates(names, "Unity 角色包")
            for entry in entries:
                name = validate_logical_path(entry.filename)
                unix_mode = (entry.external_attr >> 16) & 0xFFFF
                if (
                    entry.is_dir()
                    or entry.flag_bits & 0x1
                    or unix_mode & 0o170000 == 0o120000
                    or name != entry.filename
                ):
                    raise ValueError("Unity 角色包包含不安全条目。")
                atlas_leaf = (
                    name.removeprefix("atlases/base/")
                    if name.startswith("atlases/base/")
                    else name.removeprefix("atlases/emission/")
                    if name.startswith("atlases/emission/")
                    else ""
                )
                if name not in {"manifest.json", "checksums.json"} and not (
                    atlas_leaf.endswith(".png")
                    and atlas_leaf.removesuffix(".png").isdigit()
                ):
                    raise ValueError("Unity 角色包包含未允许文件。")
            required = {"manifest.json", "checksums.json"}
            if not required.issubset(names):
                raise ValueError("Unity 角色包缺少清单或校验文件。")
            metadata = {
                name: archive.read(name)
                for name in required
                if archive.getinfo(name).file_size <= MAX_DELIVERY_METADATA_BYTES
            }
            if set(metadata) != required:
                raise ValueError("Unity 角色包元数据过大。")
            manifest = json.loads(metadata["manifest.json"].decode("utf-8"))
            checksums = json.loads(metadata["checksums.json"].decode("utf-8"))
            if (
                not isinstance(manifest, dict)
                or manifest.get("formatVersion") != CHARACTER_PACKAGE_FORMAT
                or manifest.get("packageShape") != CHARACTER_PACKAGE_SHAPE
            ):
                raise ValueError("Unity 角色包契约不匹配。")
            character = manifest.get("character")
            design_size = character.get("designSize") if isinstance(character, dict) else None
            if not isinstance(design_size, dict):
                raise ValueError("Unity 角色包缺少活动设计尺寸。")
            try:
                ppu = float(design_size["pixelsPerUnit"])
                width_pixels = float(design_size["widthPixels"])
                height_pixels = float(design_size["heightPixels"])
                width_world = float(design_size["widthWorld"])
                height_world = float(design_size["heightWorld"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError("Unity 角色包设计尺寸无效。") from exc
            if (
                not all(math.isfinite(value) and value > 0 for value in (ppu, width_pixels, height_pixels, width_world, height_world))
                or abs(ppu - CANONICAL_PIXELS_PER_UNIT) > 0.0001
                or abs(width_pixels / ppu - width_world) > 0.0001
                or abs(height_pixels / ppu - height_world) > 0.0001
            ):
                raise ValueError("Unity 角色包像素与世界尺寸不一致。")
            validate_character_animation_timings(manifest)
            atlases = manifest.get("atlases")
            base_atlases = atlases.get("base") if isinstance(atlases, dict) else None
            emission_atlases = (
                atlases.get("emission") if isinstance(atlases, dict) else None
            )
            if not isinstance(base_atlases, list) or not base_atlases:
                raise ValueError("Unity layered package has no Base atlas list.")
            if emission_atlases is not None and not isinstance(emission_atlases, list):
                raise ValueError("Unity layered package Emission atlas list is invalid.")
            base_by_id: dict[str, dict[str, Any]] = {}
            for page in base_atlases:
                if not isinstance(page, dict):
                    raise ValueError("Unity Base atlas record is invalid.")
                atlas_id = str(page.get("id") or "")
                filename = str(page.get("file") or "")
                if (
                    not atlas_id
                    or atlas_id in base_by_id
                    or filename not in names
                    or not filename.startswith("atlases/base/")
                ):
                    raise ValueError("Unity Base atlas identity/file is invalid.")
                base_by_id[atlas_id] = page
            emission_by_id: dict[str, dict[str, Any]] = {}
            for page in emission_atlases or []:
                if not isinstance(page, dict):
                    raise ValueError("Unity Emission atlas record is invalid.")
                atlas_id = str(page.get("id") or "")
                filename = str(page.get("file") or "")
                base = base_by_id.get(atlas_id)
                if (
                    base is None
                    or atlas_id in emission_by_id
                    or filename not in names
                    or not filename.startswith("atlases/emission/")
                    or int(page.get("width") or 0) != int(base.get("width") or 0)
                    or int(page.get("height") or 0) != int(base.get("height") or 0)
                ):
                    raise ValueError("Unity Emission atlas is not paired with Base.")
                emission_by_id[atlas_id] = page
            if emission_by_id and set(emission_by_id) != set(base_by_id):
                raise ValueError("Unity layered package must pair every Base/Emission page.")
            records = checksums.get("files") if isinstance(checksums, dict) else None
            if (
                not isinstance(checksums, dict)
                or checksums.get("algorithm") != "SHA-256"
                or not isinstance(records, list)
            ):
                raise ValueError("Unity 角色包校验清单无效。")
            declared: dict[str, str] = {}
            for record in records:
                if not isinstance(record, dict):
                    raise ValueError("Unity 角色包校验项无效。")
                name = validate_logical_path(record.get("path"))
                digest = str(record.get("sha256") or "")
                if name in declared or not re.fullmatch(r"[0-9a-f]{64}", digest):
                    raise ValueError("Unity 角色包校验项重复或摘要无效。")
                declared[name] = digest
            expected_names = set(names) - {"checksums.json"}
            if set(declared) != expected_names:
                raise ValueError("Unity 角色包文件与校验清单不一致。")
            for name, expected in declared.items():
                digest = hashlib.sha256()
                with archive.open(name, "r") as source:
                    while chunk := source.read(1024 * 1024):
                        digest.update(chunk)
                if digest.hexdigest() != expected:
                    raise ValueError(f"Unity 角色包校验失败：{name}")
    except (
        OSError,
        KeyError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        zipfile.BadZipFile,
        WorkspaceFormatError,
    ) as exc:
        raise ValueError("Unity 角色包无法完整验证。") from exc


class WorkspaceRepository:
    """Latest-only Format 3 / Domain 7 workspace repository."""

    def __init__(
        self,
        root: Path,
        runtime: RuntimeRepository,
        *,
        create_if_missing: bool = False,
        workspace_name: str = "RotoWeave Workspace",
        writable: bool = True,
    ):
        self.root = root.resolve(strict=False)
        self.runtime = runtime
        self.writable = writable
        self._lock = threading.RLock()
        self._cache_lock = threading.RLock()
        self._known_file_hashes: dict[Path, str] = {}
        self._aggregate_cache: dict[Path, tuple[int, int, str, dict[str, Any]]] = {}
        self._validated_aggregate_hashes: dict[tuple[Path, str], str] = {}
        self._known_asset_state: dict[Path, tuple[int, int, str]] = {}
        if not (self.root / WORKSPACE_MANIFEST).is_file():
            if not create_if_missing:
                raise WorkspaceFormatError("所选目录不是 RotoWeave 工作区。")
            create_workspace(self.root, workspace_name)
        self.validate(full_hash=False)
        self._recover_legacy_undo_residue()

    @property
    def workspace_id(self) -> str:
        return str(self._manifest()["workspaceId"])

    @property
    def workspace_name(self) -> str:
        return str(self._manifest()["name"])

    def ensure_workspace_directory(self, path: Path) -> Path:
        candidate = path.absolute()
        try:
            relative = candidate.relative_to(self.root)
        except ValueError as exc:
            raise WorkspaceFormatError("受管目录必须位于当前工作区。") from exc
        validate_logical_path(relative.as_posix())
        current = self.root
        for part in relative.parts:
            current = current / part
            if current.exists():
                if is_reparse_point(current) or not current.is_dir():
                    raise WorkspaceFormatError("工作区受管目录不能是链接、联接或普通文件。")
                continue
            current.mkdir()
        safe = resolve_workspace_path(self.root, relative.as_posix())
        if safe != candidate.resolve(strict=False):
            raise WorkspaceFormatError("工作区受管目录解析结果不一致。")
        return safe

    def has_active_work(self) -> bool:
        return any(
            item.get("status") in {"queued", "running"}
            for item in self.runtime.list_jobs(limit=100000)
        )

    def _http_aggregate(self, api_path: str) -> dict[str, Any] | None:
        segments = [segment for segment in api_path.split("/") if segment]
        if segments and segments[0] == "size-profiles":
            return self._profiles()
        if segments and segments[0] in {
            "domain",
            "material-sources",
            "material-variants",
            "exports",
        }:
            return self._domain()
        return None

    def http_revision_target(self, api_path: str) -> str | None:
        aggregate = self._http_aggregate(api_path)
        if aggregate is None:
            return None
        return (
            "global:size-profiles"
            if "profiles" in aggregate
            else "workspace:domain"
        )

    def current_target_revision(self, target: str | None) -> str | None:
        if target == "global:size-profiles":
            return str(self._profiles().get("revisionId") or "")
        if target == "workspace:domain":
            return str(self._domain().get("revisionId") or "")
        return None

    def current_http_revision(self, api_path: str) -> str | None:
        aggregate = self._http_aggregate(api_path)
        return str(aggregate.get("revisionId") or "") if aggregate else None

    def assert_http_revision(self, api_path: str, expected_revision_id: str) -> None:
        aggregate = self._http_aggregate(api_path)
        if aggregate is None:
            return
        if str(aggregate.get("revisionId") or "") != expected_revision_id:
            raise WorkspaceRevisionConflict(
                "当前内容已被另一个页面或操作更新，请重新加载后再修改。"
            )

    def prepare_for_close(self) -> dict[str, Any]:
        if self.has_active_work():
            raise WorkspaceChangedError("仍有排队或运行中的任务，暂时不能准备复制或切换工作区。")
        validation = self.validate(full_hash=True)
        targets = [
            self.root / WORKSPACE_MANIFEST,
            self._profiles_path(),
            self._domain_path(),
        ]
        targets.extend(
            resolve_workspace_path(self.root, str(asset["path"]))
            for asset in self._iter_domain_assets(self._domain())
        )
        removed_parts = 0
        seen: set[Path] = set()
        for target in targets:
            candidates = [target.with_name(target.name + ".part")]
            if target.parent.is_dir() and not (target.parent / ".svn").exists():
                candidates.extend(target.parent.glob(f".{target.name}.*.part"))
            for candidate in candidates:
                resolved = candidate.resolve(strict=False)
                if resolved in seen or not candidate.is_file() or is_reparse_point(candidate):
                    continue
                seen.add(resolved)
                candidate.unlink(missing_ok=True)
                removed_parts += 1
        return {**validation, "removedPartFiles": removed_parts}

    def _tracked_read(self, path: Path, *, for_write: bool = False) -> dict[str, Any]:
        try:
            logical = path.relative_to(self.root).as_posix()
        except ValueError as exc:
            raise WorkspaceFormatError("工作区聚合路径越过了工作区边界。") from exc
        safe_path = resolve_workspace_path(self.root, logical).resolve(strict=False)
        try:
            stat_result = safe_path.stat()
        except OSError as exc:
            raise WorkspaceFormatError(f"无法读取工作区配置：{safe_path.name}。") from exc
        if not for_write:
            with self._cache_lock:
                cached = self._aggregate_cache.get(safe_path)
            if cached is not None and cached[0] == stat_result.st_size and cached[1] == stat_result.st_mtime_ns:
                return deepcopy(cached[3])
        value, current_hash = read_json(safe_path)
        with self._cache_lock:
            known = self._known_file_hashes.get(safe_path)
            if known is None:
                self._known_file_hashes[safe_path] = current_hash
            elif known != current_hash:
                raise WorkspaceChangedError(
                    f"{safe_path.name} 已被版本控制工具或其他程序修改，请重新打开工作区。"
                )
            self._aggregate_cache[safe_path] = (
                stat_result.st_size,
                stat_result.st_mtime_ns,
                current_hash,
                deepcopy(value),
            )
        return value

    def _validate_aggregate_once(
        self,
        path: Path,
        validation_key: str,
        value: dict[str, Any],
        validator: Callable[[], None],
    ) -> None:
        safe_path = path.resolve(strict=False)
        with self._cache_lock:
            cached = self._aggregate_cache.get(safe_path)
            current_hash = cached[2] if cached is not None else ""
            if current_hash and self._validated_aggregate_hashes.get((safe_path, validation_key)) == current_hash:
                return
        validator()
        with self._cache_lock:
            current = self._aggregate_cache.get(safe_path)
            if current is not None and current[2] == current_hash:
                self._validated_aggregate_hashes[(safe_path, validation_key)] = current_hash

    def _remember_written_aggregate(
        self, path: Path, value: dict[str, Any], file_hash: str
    ) -> None:
        safe_path = path.resolve(strict=False)
        stat_result = safe_path.stat()
        with self._cache_lock:
            self._known_file_hashes[safe_path] = file_hash
            self._aggregate_cache[safe_path] = (
                stat_result.st_size,
                stat_result.st_mtime_ns,
                file_hash,
                deepcopy(value),
            )
            stale = [key for key in self._validated_aggregate_hashes if key[0] == safe_path]
            for key in stale:
                self._validated_aggregate_hashes.pop(key, None)

    def _assert_assets_unchanged(self) -> None:
        for path, known in list(self._known_asset_state.items()):
            known_size, known_mtime, known_hash = known
            try:
                stat_result = path.stat()
            except OSError as exc:
                raise WorkspaceChangedError(f"工作区资源已被外部删除：{path.name}") from exc
            if stat_result.st_size == known_size and stat_result.st_mtime_ns == known_mtime:
                continue
            if sha256_file(path) != known_hash:
                raise WorkspaceChangedError(f"工作区资源已被版本控制工具或其他程序修改：{path.name}")
            self._known_asset_state[path] = (
                stat_result.st_size,
                stat_result.st_mtime_ns,
                known_hash,
            )

    def _write_aggregate(
        self,
        path: Path,
        value: dict[str, Any],
        previous: dict[str, Any],
        *,
        validator: Callable[[dict[str, Any]], None] | None = None,
    ) -> tuple[dict[str, Any], bool]:
        if not self.writable:
            raise WorkspaceReadOnlyError("当前工作区以只读方式打开。")
        self._assert_assets_unchanged()
        safe_path = path.resolve(strict=False)
        try:
            current_hash = sha256_file(safe_path)
        except OSError as exc:
            raise WorkspaceChangedError(f"{path.name} 无法在保存前复核。") from exc
        with self._cache_lock:
            known_hash = self._known_file_hashes.get(safe_path)
            cached = self._aggregate_cache.get(safe_path)
        if known_hash is None or cached is None:
            current = self._tracked_read(path, for_write=True)
            current_revision = str(current.get("revisionId"))
        elif current_hash != known_hash:
            raise WorkspaceChangedError(
                f"{path.name} 已被版本控制工具或其他程序修改，请重新打开工作区。"
            )
        else:
            current_revision = str(cached[3].get("revisionId"))
        if current_revision != str(previous.get("revisionId")):
            raise WorkspaceChangedError(f"{path.name} 的修订已经变化，请重新加载后再保存。")
        candidate = value
        candidate["updated_at"] = utc_now()
        finalized, changed = finalize_aggregate(candidate, previous=previous)
        if not changed:
            return deepcopy(previous), False
        if validator is not None:
            validator(finalized)
        file_hash = atomic_write_json(path, finalized)
        self._remember_written_aggregate(path, finalized, file_hash)
        return finalized, True

    def _manifest(self, *, for_write: bool = False) -> dict[str, Any]:
        path = self.root / WORKSPACE_MANIFEST
        value = self._tracked_read(path, for_write=for_write)
        self._validate_aggregate_once(
            path,
            "workspace-manifest-v3",
            value,
            lambda: validate_workspace_manifest(value),
        )
        return value
    def _profiles_path(self) -> Path:
        return self.root / "global" / "size-profiles.json"

    def _domain_path(self) -> Path:
        return self.root / WORKSPACE_DOMAIN

    def _domain(self, *, for_write: bool = False) -> dict[str, Any]:
        path = self._domain_path()
        value = self._tracked_read(path, for_write=for_write)
        self._validate_aggregate_once(
            path,
            "workspace-domain-v3",
            value,
            lambda: validate_workspace_domain(value),
        )
        return value

    @staticmethod
    def _migrate_action_refs_to_latest_variants(
        value: dict[str, Any],
        *,
        source_id: str | None = None,
        latest_variant_id: str | None = None,
        require_complete: bool = True,
    ) -> tuple[int, list[str]]:
        """Move same-source action refs to the latest immutable variant atomically."""

        variants = {
            str(item.get("id") or ""): item
            for item in value.get("materialVariants") or []
            if isinstance(item, dict)
        }
        sources = [
            item
            for item in value.get("materialSources") or []
            if isinstance(item, dict)
            and (source_id is None or item.get("id") == source_id)
        ]
        migrated = 0
        affected_action_ids: list[str] = []
        for source in sources:
            variant_ids = [str(item) for item in source.get("variantIds") or []]
            if not variant_ids:
                continue
            resolved_latest_id = str(latest_variant_id or variant_ids[-1])
            if resolved_latest_id not in variant_ids:
                raise WorkspaceFormatError("最新处理版本不属于目标素材源。")
            latest = variants.get(resolved_latest_id)
            if latest is None or latest.get("sourceId") != source.get("id"):
                raise WorkspaceFormatError("最新处理版本不存在或素材身份不一致。")
            latest_by_source_frame: dict[str, str] = {}
            for frame in latest.get("frames") or []:
                source_frame_id = str(frame.get("sourceFrameId") or "")
                frame_id = str(frame.get("id") or "")
                if not source_frame_id or not frame_id or source_frame_id in latest_by_source_frame:
                    raise WorkspaceFormatError("最新处理版本的源帧映射无效。")
                latest_by_source_frame[source_frame_id] = frame_id
            expected_source_frames = {
                str(frame.get("id") or "") for frame in source.get("frames") or []
            }
            if require_complete and set(latest_by_source_frame) != expected_source_frames:
                raise WorkspaceFormatError("最新处理版本没有完整覆盖素材源帧。")
            if not set(latest_by_source_frame).issubset(expected_source_frames):
                raise WorkspaceFormatError("最新处理版本映射了不属于素材源的帧。")

            source_variant_ids = set(variant_ids)
            character_id = str(source.get("characterId") or "")
            for action in value.get("actions") or []:
                if action.get("characterId") != character_id:
                    continue
                action_changed = False
                for ref in action.get("frameRefs") or []:
                    current_variant_id = str(ref.get("variantId") or "")
                    if current_variant_id not in source_variant_ids:
                        continue
                    current_variant = variants.get(current_variant_id)
                    current_frame = next(
                        (
                            frame
                            for frame in (current_variant or {}).get("frames") or []
                            if frame.get("id") == ref.get("frameId")
                        ),
                        None,
                    )
                    source_frame_id = str((current_frame or {}).get("sourceFrameId") or "")
                    next_frame_id = latest_by_source_frame.get(source_frame_id)
                    if not next_frame_id:
                        continue
                    if current_variant_id == resolved_latest_id and ref.get("frameId") == next_frame_id:
                        continue
                    ref["variantId"] = resolved_latest_id
                    ref["frameId"] = next_frame_id
                    migrated += 1
                    action_changed = True
                if action_changed:
                    affected_action_ids.append(str(action.get("id") or ""))
        return migrated, affected_action_ids

    @staticmethod
    def _initialize_domain_character(character: dict[str, Any]) -> None:
        calibration = character.setdefault("calibration", {
            "sizeProfiles": [{"id": "default", "name": "默认", "presetId": None, "unitMode": "pixels", "width": 512, "height": 512}],
            "activeSizeProfileId": "default", "sizeGuideCenterX": 0.0,
            "sizeGuideBottomY": 0.0, "alignmentHorizonY": 0.0,
            "shadowStandardY": 0.0, "pixelsPerUnit": CANONICAL_PIXELS_PER_UNIT, "coreReference": None,
        })
        calibration.setdefault("pixelsPerUnit", CANONICAL_PIXELS_PER_UNIT)
        for profile in calibration.get("sizeProfiles") or []:
            if isinstance(profile, dict):
                profile.setdefault("presetId", None)
                profile.setdefault("unitMode", "pixels")
        character.setdefault("shadow", {"enabled": True, "color": "#000000", "baseOpacity": 0.35, "lightAngleDegrees": 135.0})
        delivery = character.setdefault("delivery", {
            "defaultActionId": None, "globalTextureScale": 1.0, "actionSettings": {},
            "atlas": {"maxSize": 4096, "padding": 2, "extrude": 1, "framePadding": 0},
        })
        for setting in (delivery.get("actionSettings") or {}).values():
            if isinstance(setting, dict):
                setting.setdefault("includeInExport", True)

    @staticmethod
    def _iter_domain_assets(value: Any):
        if isinstance(value, dict):
            if {"path", "sha256", "bytes"}.issubset(value):
                yield value
            for item in value.values():
                yield from WorkspaceRepository._iter_domain_assets(item)
        elif isinstance(value, list):
            for item in value:
                yield from WorkspaceRepository._iter_domain_assets(item)

    def _discard_unreferenced_asset_state(self, value: dict[str, Any]) -> None:
        referenced = {
            resolve_workspace_path(self.root, str(asset["path"]))
            for asset in self._iter_domain_assets(value)
        }
        with self._cache_lock:
            for path in list(self._known_asset_state):
                if path not in referenced:
                    self._known_asset_state.pop(path, None)

    @classmethod
    def _asset_map(cls, value: dict[str, Any]) -> dict[str, tuple[str, int]]:
        return {
            str(asset["path"]): (str(asset["sha256"]), int(asset["bytes"]))
            for asset in cls._iter_domain_assets(value)
        }

    @staticmethod
    def _is_managed_asset_path(logical: str) -> bool:
        parts = Path(logical).parts
        return bool(
            parts
            and (
                parts[0] == "materials"
                or (
                    len(parts) >= 5
                    and parts[0] == "characters"
                    and parts[2] == "core-reference"
                    and parts[-1] == "core.png"
                )
            )
        )

    def _recover_legacy_undo_residue(self) -> None:
        """Consume the retired undo ledger without trusting stale candidates."""

        ledger_path = self.runtime.path.parent / "workspace-undo-pending.json"
        if not ledger_path.is_file():
            return
        try:
            try:
                value, _ = read_json(ledger_path)
            except (OSError, WorkspaceFormatError):
                return
            if value.get("workspaceId") != self.workspace_id:
                return
            raw_paths = value.get("paths")
            if (
                value.get("schemaVersion") != 1
                or not isinstance(raw_paths, list)
                or not all(isinstance(item, str) for item in raw_paths)
            ):
                return
            try:
                paths = {validate_logical_path(item) for item in raw_paths}
            except WorkspaceFormatError:
                return
            referenced = set(self._asset_map(self._domain()))
            candidates = sorted(
                path
                for path in paths
                if path not in referenced and self._is_managed_asset_path(path)
            )
            self._remove_domain_asset_files_now(candidates)
        finally:
            try:
                ledger_path.unlink(missing_ok=True)
            except OSError:
                pass

    def _domain_asset_record(
        self,
        logical_path: str,
        *,
        prefix: str,
        index: int | None = None,
    ) -> dict[str, Any]:
        logical = validate_logical_path(logical_path)
        path = resolve_workspace_path(self.root, logical)
        if not path.is_file() or is_reparse_point(path):
            raise WorkspaceFormatError("领域资产必须是工作区内的普通文件。")
        result: dict[str, Any] = {
            "id": new_id(prefix),
            "path": logical,
            "sha256": sha256_file(path),
            "bytes": path.stat().st_size,
        }
        if index is not None:
            result["index"] = index
        return result

    def _mutate_domain(
        self,
        mutator: Callable[[dict[str, Any]], None],
        *,
        expected_revision_id: str | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            previous = self._domain(for_write=True)
            if (
                expected_revision_id is not None
                and str(previous.get("revisionId") or "") != expected_revision_id
            ):
                raise WorkspaceRevisionConflict(
                    "格式 3 领域状态已变化，请刷新后重试。"
                )
            candidate = deepcopy(previous)
            mutator(candidate)
            saved, _ = self._write_aggregate(
                self._domain_path(),
                candidate,
                previous,
                validator=lambda value: validate_workspace_domain(
                    value, content_hash_verified=True
                ),
            )
            self._discard_unreferenced_asset_state(saved)
            return saved

    def workspace_domain(self) -> dict[str, Any]:
        return deepcopy(self._domain())

    def create_domain_character(
        self,
        name: str,
        *,
        expected_revision_id: str | None = None,
    ) -> dict[str, Any]:
        character = {
            "id": new_id("dchar"),
            "name": str(name).strip(),
            "actionIds": [],
            "materialSourceIds": [],
            "calibration": {
                "sizeProfiles": [{"id": "default", "name": "默认", "presetId": None, "unitMode": "pixels", "width": 512, "height": 512}],
                "activeSizeProfileId": "default", "sizeGuideCenterX": 0.0,
                "sizeGuideBottomY": 0.0, "alignmentHorizonY": 0.0,
                "shadowStandardY": 0.0, "pixelsPerUnit": CANONICAL_PIXELS_PER_UNIT, "coreReference": None,
            },
            "shadow": {"enabled": True, "color": "#000000", "baseOpacity": 0.35, "lightAngleDegrees": 135.0},
            "delivery": {"defaultActionId": None, "globalTextureScale": 1.0, "actionSettings": {}, "atlas": {"maxSize": 4096, "padding": 2, "extrude": 1, "framePadding": 0}},
            "exportState": {
                "status": "not-exported",
                "currentAtlas": None,
            },
        }

        def mutate(value: dict[str, Any]) -> None:
            value["characters"].append(character)

        saved = self._mutate_domain(
            mutate,
            expected_revision_id=expected_revision_id,
        )
        return next(
            deepcopy(item)
            for item in saved["characters"]
            if item["id"] == character["id"]
        )

    def update_domain_character(
        self,
        character_id: str,
        *,
        name: str,
        expected_revision_id: str,
    ) -> dict[str, Any]:
        normalized_name = str(name).strip()
        if not normalized_name:
            raise WorkspaceFormatError("角色名称不能为空。")

        def mutate(value: dict[str, Any]) -> None:
            character = next(
                (item for item in value["characters"] if item["id"] == character_id),
                None,
            )
            if character is None:
                raise WorkspaceFormatError("角色不存在。")
            character["name"] = normalized_name

        saved = self._mutate_domain(
            mutate,
            expected_revision_id=expected_revision_id,
        )
        return deepcopy(
            next(item for item in saved["characters"] if item["id"] == character_id)
        )

    def delete_domain_character(
        self,
        character_id: str,
        *,
        expected_revision_id: str,
    ) -> dict[str, Any]:
        removed: dict[str, Any] = {}
        asset_paths: list[str] = []

        def collect_asset(asset: object) -> None:
            if isinstance(asset, dict):
                logical = str(asset.get("path") or "")
                if logical:
                    asset_paths.append(logical)

        def mutate(value: dict[str, Any]) -> None:
            character = next(
                (item for item in value["characters"] if item["id"] == character_id),
                None,
            )
            if character is None:
                raise WorkspaceFormatError("角色不存在。")
            removed.update(deepcopy(character))
            source_ids = {
                item["id"]
                for item in value["materialSources"]
                if item.get("characterId") == character_id
            }
            variant_ids = {
                item["id"]
                for item in value["materialVariants"]
                if item.get("sourceId") in source_ids
            }
            for source in value["materialSources"]:
                if source.get("id") not in source_ids:
                    continue
                collect_asset(source.get("video"))
                for frame in source.get("frames", []):
                    collect_asset(frame)
                    collect_asset(frame.get("linear"))
            for variant in value["materialVariants"]:
                if variant.get("id") not in variant_ids:
                    continue
                for frame in variant.get("frames", []):
                    collect_asset(frame)
                    collect_asset(frame.get("emission"))
            value["actions"] = [
                item for item in value["actions"] if item.get("characterId") != character_id
            ]
            value["materialVariants"] = [
                item for item in value["materialVariants"] if item.get("id") not in variant_ids
            ]
            value["materialSources"] = [
                item for item in value["materialSources"] if item.get("id") not in source_ids
            ]
            value["characters"] = [
                item for item in value["characters"] if item.get("id") != character_id
            ]

        self._mutate_domain(
            mutate,
            expected_revision_id=expected_revision_id,
        )
        return {
            "removed": removed,
            "assetPaths": asset_paths,
        }

    def create_material_source(
        self,
        character_id: str,
        display_name: str,
        video_path: str,
        frame_paths: list[str],
        *,
        metadata: dict[str, Any] | None = None,
        frame_metadata: list[dict[str, Any]] | None = None,
        source_id: str | None = None,
        expected_revision_id: str | None = None,
    ) -> dict[str, Any]:
        resolved_source_id = source_id or new_id("msrc")
        resolved_frame_metadata = frame_metadata or [{} for _ in frame_paths]
        if len(resolved_frame_metadata) != len(frame_paths):
            raise WorkspaceFormatError("源帧元数据数量与源帧文件不一致。")
        frames: list[dict[str, Any]] = []
        for index, (path, frame_info) in enumerate(
            zip(frame_paths, resolved_frame_metadata, strict=True)
        ):
            record = self._domain_asset_record(path, prefix="srcf", index=index)
            linear_path = str(frame_info.get("linearPath") or path)
            record.update(
                {
                    "ptsUs": int(frame_info.get("ptsUs", round(index / 24 * 1_000_000))),
                    "durationUs": int(frame_info.get("durationUs", round(1_000_000 / 24))),
                    "width": int(frame_info.get("width", 1)),
                    "height": int(frame_info.get("height", 1)),
                    "linear": self._domain_asset_record(linear_path, prefix="linf"),
                    "thumbnailRevision": str(
                        frame_info.get("thumbnailRevision") or record["sha256"]
                    ),
                }
            )
            frames.append(record)
        resolved_metadata = deepcopy(metadata or {})
        resolved_metadata.setdefault("fps", 24.0)
        resolved_metadata.setdefault("durationSeconds", len(frames) / 24.0)
        resolved_metadata.setdefault("frameCount", len(frames))
        resolved_metadata.setdefault("width", frames[0]["width"] if frames else 1)
        resolved_metadata.setdefault("height", frames[0]["height"] if frames else 1)
        resolved_metadata.setdefault(
            "color",
            {
                "transfer": "bt709",
                "primaries": "bt709",
                "matrix": "bt709",
                "range": "tv",
            },
        )
        resolved_metadata.setdefault("warnings", [])
        source = {
            "id": resolved_source_id,
            "characterId": character_id,
            "displayName": str(display_name).strip(),
            "video": self._domain_asset_record(video_path, prefix="video"),
            "metadata": resolved_metadata,
            "frames": frames,
            "variantIds": [],
            "createdAt": utc_now(),
        }

        def mutate(value: dict[str, Any]) -> None:
            character = next(
                (item for item in value["characters"] if item["id"] == character_id),
                None,
            )
            if character is None:
                raise WorkspaceFormatError("素材源所属角色不存在。")
            character["materialSourceIds"].append(source["id"])
            value["materialSources"].append(source)

        saved = self._mutate_domain(
            mutate,
            expected_revision_id=expected_revision_id,
        )
        return next(
            deepcopy(item)
            for item in saved["materialSources"]
            if item["id"] == source["id"]
        )

    def append_material_variant(
        self,
        source_id: str,
        kind: str,
        frame_paths: list[str],
        settings: dict[str, Any],
        *,
        emission_paths: list[str | None] | None = None,
        source_frame_ids: list[str] | None = None,
        variant_id: str | None = None,
        expected_revision_id: str | None = None,
    ) -> dict[str, Any]:
        domain = self._domain()
        source = next(
            (item for item in domain["materialSources"] if item["id"] == source_id),
            None,
        )
        if source is None:
            raise WorkspaceFormatError("素材版本所属素材源不存在。")
        source_frames = source.get("frames") or []
        selected_source_frame_ids = (
            [str(frame["id"]) for frame in source_frames]
            if source_frame_ids is None
            else [str(item) for item in source_frame_ids]
        )
        source_frames_by_id = {str(frame["id"]): frame for frame in source_frames}
        if not selected_source_frame_ids or len(frame_paths) != len(selected_source_frame_ids):
            raise WorkspaceFormatError("处理帧必须与本次选择的源帧一一对应。")
        if len(selected_source_frame_ids) != len(set(selected_source_frame_ids)):
            raise WorkspaceFormatError("处理版本的源帧映射不能重复。")
        if any(item not in source_frames_by_id for item in selected_source_frame_ids):
            raise WorkspaceFormatError("处理版本映射了不属于素材源的帧。")
        if emission_paths is not None and len(emission_paths) != len(frame_paths):
            raise WorkspaceFormatError("特效层必须与处理帧一一对应。")
        settings_snapshot = deepcopy(settings)
        settings_sha256 = hashlib.sha256(
            json.dumps(
                settings_snapshot,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        variant_frames: list[dict[str, Any]] = []
        for index, (path, source_frame_id) in enumerate(
            zip(frame_paths, selected_source_frame_ids, strict=True)
        ):
            record = self._domain_asset_record(path, prefix="varf", index=index)
            record["sourceFrameId"] = source_frame_id
            emission_path = emission_paths[index] if emission_paths is not None else None
            if emission_path:
                record["emission"] = self._domain_asset_record(
                    emission_path, prefix="emit", index=index
                )
            variant_frames.append(record)
        variant = {
            "id": variant_id or new_id("mvar"),
            "sourceId": source_id,
            "kind": kind,
            "settings": settings_snapshot,
            "settingsSha256": settings_sha256,
            "frames": variant_frames,
            "createdAt": utc_now(),
            "migration": {
                "actionFrameCount": 0,
                "actionIds": [],
            },
        }

        def mutate(value: dict[str, Any]) -> None:
            target_source = next(
                (item for item in value["materialSources"] if item["id"] == source_id),
                None,
            )
            if target_source is None:
                raise WorkspaceFormatError("素材版本所属素材源不存在。")
            target_source.setdefault("variantIds", []).append(variant["id"])
            value["materialVariants"].append(variant)
            migrated_count, action_ids = self._migrate_action_refs_to_latest_variants(
                value,
                source_id=source_id,
                latest_variant_id=variant["id"],
                require_complete=False,
            )
            variant["migration"]["actionFrameCount"] = migrated_count
            variant["migration"]["actionIds"] = action_ids

        saved = self._mutate_domain(
            mutate,
            expected_revision_id=expected_revision_id,
        )
        return next(
            deepcopy(item)
            for item in saved["materialVariants"]
            if item["id"] == variant["id"]
        )

    def get_material_source(self, source_id: str) -> dict[str, Any] | None:
        return next(
            (
                deepcopy(item)
                for item in self._domain()["materialSources"]
                if item["id"] == source_id
            ),
            None,
        )

    def get_material_variant(self, variant_id: str) -> dict[str, Any] | None:
        return next(
            (
                deepcopy(item)
                for item in self._domain()["materialVariants"]
                if item["id"] == variant_id
            ),
            None,
        )

    def find_material_source_by_content_hash(
        self, character_id: str, sha256: str
    ) -> dict[str, Any] | None:
        return next(
            (
                deepcopy(item)
                for item in self._domain()["materialSources"]
                if item.get("characterId") == character_id
                and (item.get("video") or {}).get("sha256") == sha256
            ),
            None,
        )

    def _publish_asset_directory(
        self,
        final_root: Path,
        files: dict[str, Path],
    ) -> dict[str, str]:
        if not files:
            raise WorkspaceFormatError("正式资产发布列表不能为空。")
        parent = self.ensure_workspace_directory(final_root.parent)
        if final_root.exists():
            raise WorkspaceFormatError("正式资产发布目标已存在。")
        part_root = parent / f".{final_root.name}.part-{uuid.uuid4().hex}"
        published: dict[str, str] = {}
        try:
            part_root.mkdir()
            for relative_name, source in files.items():
                logical_name = validate_logical_path(relative_name)
                resolved_source = source.resolve(strict=True)
                if not resolved_source.is_file() or is_reparse_point(resolved_source):
                    raise WorkspaceFormatError("待发布资产必须是普通文件。")
                target = part_root / Path(*logical_name.split("/"))
                target.parent.mkdir(parents=True, exist_ok=True)
                expected_size = resolved_source.stat().st_size
                expected_hash = sha256_file(resolved_source)
                shutil.copyfile(resolved_source, target)
                if target.stat().st_size != expected_size or sha256_file(target) != expected_hash:
                    raise WorkspaceFormatError("正式资产复制校验失败。")
                with target.open("rb+") as handle:
                    os.fsync(handle.fileno())
                published[relative_name] = logical_workspace_path(
                    self.root,
                    final_root / Path(*logical_name.split("/")),
                )
            part_root.replace(final_root)
            return published
        except Exception:
            shutil.rmtree(part_root, ignore_errors=True)
            raise

    def publish_material_source(
        self,
        character_id: str,
        display_name: str,
        video_path: str,
        frames: list[dict[str, Any]],
        metadata: dict[str, Any],
        *,
        source_id: str | None = None,
        expected_revision_id: str | None = None,
    ) -> dict[str, Any]:
        source_video = Path(video_path).resolve(strict=True)
        digest = sha256_file(source_video)
        duplicate = self.find_material_source_by_content_hash(character_id, digest)
        if duplicate is not None:
            return duplicate
        resolved_source_id = source_id or new_id("msrc")
        validate_stable_id(resolved_source_id, "素材源")
        final_root = self.root / "materials" / "sources" / resolved_source_id
        suffix = source_video.suffix.lower() or ".bin"
        files: dict[str, Path] = {f"source{suffix}": source_video}
        for index, frame in enumerate(frames):
            preview = Path(str(frame.get("path") or "")).resolve(strict=True)
            linear = Path(str(frame.get("linearPath") or "")).resolve(strict=True)
            files[f"frames/{index:06d}{preview.suffix.lower() or '.png'}"] = preview
            files[f"linear/{index:06d}{linear.suffix.lower() or '.exr'}"] = linear
        published = self._publish_asset_directory(final_root, files)
        frame_paths: list[str] = []
        frame_metadata: list[dict[str, Any]] = []
        for index, frame in enumerate(frames):
            preview = Path(str(frame.get("path") or ""))
            linear = Path(str(frame.get("linearPath") or ""))
            preview_key = f"frames/{index:06d}{preview.suffix.lower() or '.png'}"
            linear_key = f"linear/{index:06d}{linear.suffix.lower() or '.exr'}"
            frame_paths.append(published[preview_key])
            frame_metadata.append(
                {
                    "linearPath": published[linear_key],
                    "ptsUs": int(frame.get("ptsUs", 0)),
                    "durationUs": int(frame.get("durationUs", 0)),
                    "width": int(frame.get("width", 0)),
                    "height": int(frame.get("height", 0)),
                    "thumbnailRevision": str(frame.get("thumbnailRevision") or ""),
                }
            )
        try:
            return self.create_material_source(
                character_id,
                display_name,
                published[f"source{suffix}"],
                frame_paths,
                metadata=metadata,
                frame_metadata=frame_metadata,
                source_id=resolved_source_id,
                expected_revision_id=expected_revision_id,
            )
        except Exception:
            shutil.rmtree(final_root, ignore_errors=True)
            raise

    def publish_material_variant(
        self,
        source_id: str,
        kind: str,
        frame_paths: list[str],
        settings: dict[str, Any],
        *,
        emission_paths: list[str | None] | None = None,
        source_frame_ids: list[str] | None = None,
        variant_id: str | None = None,
        expected_revision_id: str | None = None,
    ) -> dict[str, Any]:
        source = self.get_material_source(source_id)
        if source is None:
            raise WorkspaceFormatError("素材版本所属素材源不存在。")
        selected_source_frame_ids = (
            [str(frame["id"]) for frame in source.get("frames") or []]
            if source_frame_ids is None
            else [str(item) for item in source_frame_ids]
        )
        if not selected_source_frame_ids or len(frame_paths) != len(selected_source_frame_ids):
            raise WorkspaceFormatError("处理帧必须与本次选择的源帧一一对应。")
        if emission_paths is not None and len(emission_paths) != len(frame_paths):
            raise WorkspaceFormatError("特效层必须与处理帧一一对应。")
        resolved_variant_id = variant_id or new_id("mvar")
        validate_stable_id(resolved_variant_id, "素材版本")
        final_root = self.root / "materials" / "variants" / resolved_variant_id
        files: dict[str, Path] = {}
        for index, raw_path in enumerate(frame_paths):
            path = Path(raw_path).resolve(strict=True)
            files[f"frames/{index:06d}{path.suffix.lower() or '.png'}"] = path
            emission_path = emission_paths[index] if emission_paths is not None else None
            if emission_path:
                layer = Path(emission_path).resolve(strict=True)
                files[f"emission/{index:06d}{layer.suffix.lower() or '.png'}"] = layer
        published = self._publish_asset_directory(final_root, files)
        published_paths = [
            published[f"frames/{index:06d}{Path(raw_path).suffix.lower() or '.png'}"]
            for index, raw_path in enumerate(frame_paths)
        ]
        published_emission_paths = (
            [
                (
                    published[
                        f"emission/{index:06d}{Path(raw_path).suffix.lower() or '.png'}"
                    ]
                    if raw_path
                    else None
                )
                for index, raw_path in enumerate(emission_paths)
            ]
            if emission_paths is not None
            else None
        )
        try:
            variant = self.append_material_variant(
                source_id,
                kind,
                published_paths,
                settings,
                emission_paths=published_emission_paths,
                source_frame_ids=selected_source_frame_ids,
                variant_id=resolved_variant_id,
                expected_revision_id=expected_revision_id,
            )
            return variant
        except Exception:
            shutil.rmtree(final_root, ignore_errors=True)
            raise

    def create_domain_action(
        self,
        character_id: str,
        name: str,
        *,
        expected_revision_id: str | None = None,
    ) -> dict[str, Any]:
        action = {
            "id": new_id("action"),
            "characterId": character_id,
            "name": str(name).strip(),
            "previewLoop": True,
            "loop": True,
            "frameRefs": [],
        }

        def mutate(value: dict[str, Any]) -> None:
            character = next(
                (item for item in value["characters"] if item["id"] == character_id),
                None,
            )
            if character is None:
                raise WorkspaceFormatError("动作所属角色不存在。")
            character["actionIds"].append(action["id"])
            character["delivery"]["actionSettings"][action["id"]] = {"textureScale": 1.0, "runtimeLoop": True, "includeInExport": True}
            if character["delivery"].get("defaultActionId") is None:
                character["delivery"]["defaultActionId"] = action["id"]
            value["actions"].append(action)

        saved = self._mutate_domain(
            mutate,
            expected_revision_id=expected_revision_id,
        )
        return next(
            deepcopy(item)
            for item in saved["actions"]
            if item["id"] == action["id"]
        )

    def get_domain_action(self, action_id: str) -> dict[str, Any] | None:
        return next(
            (
                deepcopy(item)
                for item in self._domain()["actions"]
                if item["id"] == action_id
            ),
            None,
        )

    def update_domain_action(
        self,
        action_id: str,
        *,
        name: str | None = None,
        loop: bool | None = None,
        preview_loop: bool | None = None,
        expected_revision_id: str,
    ) -> dict[str, Any]:
        normalized_name = str(name).strip() if name is not None else None
        if normalized_name is not None and not normalized_name:
            raise WorkspaceFormatError("动作名称不能为空。")

        def mutate(value: dict[str, Any]) -> None:
            action = next(
                (item for item in value["actions"] if item["id"] == action_id),
                None,
            )
            if action is None:
                raise WorkspaceFormatError("动作不存在。")
            if normalized_name is not None:
                action["name"] = normalized_name
            chosen_loop = preview_loop if preview_loop is not None else loop
            if chosen_loop is not None:
                action["previewLoop"] = bool(chosen_loop)
                action["loop"] = bool(chosen_loop)

        saved = self._mutate_domain(
            mutate,
            expected_revision_id=expected_revision_id,
        )
        return deepcopy(next(item for item in saved["actions"] if item["id"] == action_id))

    def delete_domain_action(
        self,
        action_id: str,
        *,
        expected_revision_id: str,
    ) -> dict[str, Any]:
        removed: dict[str, Any] = {}

        def mutate(value: dict[str, Any]) -> None:
            action = next(
                (item for item in value["actions"] if item["id"] == action_id),
                None,
            )
            if action is None:
                raise WorkspaceFormatError("动作不存在。")
            removed.update(deepcopy(action))
            value["actions"] = [
                item for item in value["actions"] if item["id"] != action_id
            ]
            character = next(
                item
                for item in value["characters"]
                if item["id"] == action["characterId"]
            )
            character["actionIds"] = [
                item for item in character["actionIds"] if item != action_id
            ]
            character["delivery"]["actionSettings"].pop(action_id, None)
            if character["delivery"].get("defaultActionId") == action_id:
                character["delivery"]["defaultActionId"] = character["actionIds"][0] if character["actionIds"] else None

        self._mutate_domain(
            mutate,
            expected_revision_id=expected_revision_id,
        )
        return removed

    @staticmethod
    def _normalized_action_transform(raw: dict[str, Any] | None) -> dict[str, Any]:
        value = deepcopy(raw or {})
        position = value.get("position") or {}
        scale = value.get("scale") or {}
        shadow = value.get("shadow") or {}
        shadow_offset = shadow.get("offset") or {}
        shadow_scale = shadow.get("scale") or {}
        return {
            "position": {
                "x": float(position.get("x", 0.0)),
                "y": float(position.get("y", 0.0)),
            },
            "scale": {
                "x": float(scale.get("x", 1.0)),
                "y": float(scale.get("y", 1.0)),
            },
            "rotationDegrees": float(value.get("rotationDegrees", 0.0)),
            "color": str(value.get("color") or "#ffffff").lower(),
            "opacity": float(value.get("opacity", 1.0)),
            "shadow": {
                "enabled": shadow.get("enabled") if shadow.get("enabled") is None else bool(shadow.get("enabled")),
                "color": str(shadow["color"]).lower() if shadow.get("color") is not None else None,
                "opacity": float(shadow["opacity"]) if shadow.get("opacity") is not None else None,
                "offset": {
                    "x": float(shadow_offset.get("x", 0.0)),
                    "y": float(shadow_offset.get("y", 0.0)),
                },
                "scale": {
                    "x": float(shadow_scale.get("x", 1.0)),
                    "y": float(shadow_scale.get("y", 1.0)),
                },
            },
        }

    def update_domain_character_settings(self, character_id: str, changes: dict[str, Any], *, expected_revision_id: str) -> dict[str, Any]:
        allowed = {"calibration", "shadow", "delivery"}
        if set(changes) - allowed:
            raise WorkspaceFormatError("角色设置包含不支持的字段。")
        def merge(target: dict[str, Any], patch: dict[str, Any]) -> None:
            for key, item in patch.items():
                if isinstance(item, dict) and isinstance(target.get(key), dict):
                    merge(target[key], item)
                else:
                    target[key] = deepcopy(item)
        def mutate(value: dict[str, Any]) -> None:
            character = next((item for item in value["characters"] if item["id"] == character_id), None)
            if character is None:
                raise WorkspaceFormatError("角色不存在。")
            self._initialize_domain_character(character)
            merge(character, changes)
        saved = self._mutate_domain(
            mutate,
            expected_revision_id=expected_revision_id,
        )
        return deepcopy(next(item for item in saved["characters"] if item["id"] == character_id))

    def set_domain_core_reference(self, character_id: str, source_path: Path, *, expected_revision_id: str) -> dict[str, Any]:
        source = Path(source_path).resolve(strict=True)
        source_hash = sha256_file(source)
        core_root = self.root / "characters" / character_id / "core-reference"
        final_root = core_root / source_hash
        final_file = final_root / "core.png"
        previous_character = next(
            (item for item in self._domain().get("characters", []) if item.get("id") == character_id),
            None,
        )
        if previous_character is None:
            raise WorkspaceFormatError("角色不存在。")
        previous_asset = deepcopy(((previous_character.get("calibration") or {}).get("coreReference")))
        created_generation = False
        if final_root.exists():
            if not final_file.is_file() or is_reparse_point(final_file) or sha256_file(final_file) != source_hash:
                raise WorkspaceFormatError("同内容核心图资产目录已存在但校验失败。")
            logical_core_path = logical_workspace_path(self.root, final_file)
        else:
            published = self._publish_asset_directory(final_root, {"core.png": source})
            logical_core_path = published["core.png"]
            created_generation = True
        resolved_core_path = resolve_workspace_path(self.root, logical_core_path)
        asset = self._domain_asset_record(logical_core_path, prefix="core")
        try:
            from PIL import Image
            with Image.open(resolved_core_path) as image:
                asset.update(width=int(image.width), height=int(image.height), scale=1.0, origin={"x": 0.0, "y": 0.0})
            character = self.update_domain_character_settings(
                character_id,
                {"calibration": {"coreReference": asset}},
                expected_revision_id=expected_revision_id,
            )
        except Exception:
            if created_generation:
                shutil.rmtree(final_root, ignore_errors=True)
            raise
        if isinstance(previous_asset, dict) and previous_asset.get("path") != logical_core_path:
            try:
                previous_file = resolve_workspace_path(self.root, previous_asset.get("path"))
                resolved_core_root = core_root.resolve()
                if previous_file.parent.parent == resolved_core_root and previous_file.parent != final_root.resolve():
                    self.remove_domain_asset_files([str(previous_asset.get("path") or "")])
            except (OSError, WorkspaceFormatError):
                pass
        return character

    def delete_domain_core_reference(self, character_id: str, *, expected_revision_id: str) -> dict[str, Any]:
        current = next(
            (item for item in self._domain().get("characters", []) if item.get("id") == character_id),
            None,
        )
        previous_asset = deepcopy((((current or {}).get("calibration") or {}).get("coreReference")))
        character = self.update_domain_character_settings(
            character_id,
            {"calibration": {"coreReference": None}},
            expected_revision_id=expected_revision_id,
        )
        if isinstance(previous_asset, dict) and previous_asset.get("path"):
            self.remove_domain_asset_files([str(previous_asset["path"])])
        return character

    def replace_action_frame_refs(
        self,
        action_id: str,
        frame_refs: list[dict[str, Any]],
        *,
        expected_revision_id: str,
    ) -> dict[str, Any]:
        normalized: list[dict[str, Any]] = []
        for item in frame_refs:
            transform = self._normalized_action_transform(item.get("transform"))
            normalized.append(
                {
                    "id": str(item.get("id") or new_id("afrm")),
                    "variantId": str(item.get("variantId") or ""),
                    "frameId": str(item.get("frameId") or ""),
                    "durationSeconds": float(item.get("durationSeconds", 1 / 24)),
                    "enabled": bool(item.get("enabled", True)),
                    "transform": transform,
                }
            )

        def mutate(value: dict[str, Any]) -> None:
            action = next(
                (item for item in value["actions"] if item["id"] == action_id),
                None,
            )
            if action is None:
                raise WorkspaceFormatError("动作不存在。")
            action["frameRefs"] = normalized

        saved = self._mutate_domain(
            mutate,
            expected_revision_id=expected_revision_id,
        )
        return next(
            deepcopy(item)
            for item in saved["actions"]
            if item["id"] == action_id
        )

    def append_action_frame_refs(
        self,
        action_id: str,
        frame_refs: list[dict[str, Any]],
        *,
        expected_revision_id: str,
    ) -> dict[str, Any]:
        current = self.get_domain_action(action_id)
        if current is None:
            raise WorkspaceFormatError("动作不存在。")
        return self.replace_action_frame_refs(
            action_id,
            [*(current.get("frameRefs") or []), *frame_refs],
            expected_revision_id=expected_revision_id,
        )

    def cleanup_material_variant(
        self,
        variant_id: str,
        *,
        explicit: bool,
        expected_revision_id: str,
    ) -> dict[str, Any]:
        if not explicit:
            raise WorkspaceFormatError("素材版本只能由用户显式清理。")
        removed: dict[str, Any] = {}

        def mutate(value: dict[str, Any]) -> None:
            if any(
                ref.get("variantId") == variant_id
                for action in value["actions"]
                for ref in action.get("frameRefs", [])
            ):
                raise WorkspaceFormatError("素材版本仍被动作引用，不能清理。")
            variant = next(
                (
                    item
                    for item in value["materialVariants"]
                    if item["id"] == variant_id
                ),
                None,
            )
            if variant is None:
                raise WorkspaceFormatError("素材版本不存在。")
            removed.update(deepcopy(variant))
            value["materialVariants"] = [
                item for item in value["materialVariants"] if item["id"] != variant_id
            ]
            for source in value["materialSources"]:
                source["variantIds"] = [
                    item for item in source.get("variantIds", []) if item != variant_id
                ]

        self._mutate_domain(
            mutate,
            expected_revision_id=expected_revision_id,
        )
        return {
            "removed": removed,
            "referenceCount": 0,
            "assetPaths": [item["path"] for item in removed.get("frames", [])],
        }

    def material_variant_reference_count(self, variant_id: str) -> int:
        return sum(
            1
            for action in self._domain()["actions"]
            for ref in action.get("frameRefs", [])
            if ref.get("variantId") == variant_id
        )

    def delete_material_source(
        self,
        source_id: str,
        *,
        explicit: bool,
        expected_revision_id: str,
    ) -> dict[str, Any]:
        if not explicit:
            raise WorkspaceFormatError("素材源只能由用户显式删除。")
        removed: dict[str, Any] = {}
        removed_variants: list[dict[str, Any]] = []
        asset_paths: list[str] = []

        def collect_asset(asset: object) -> None:
            if not isinstance(asset, dict):
                return
            logical = str(asset.get("path") or "")
            if not logical:
                return
            target = resolve_workspace_path(self.root, logical)
            material_root = (self.root / "materials").resolve(strict=False)
            if material_root not in target.parents:
                raise WorkspaceFormatError("素材资产路径不属于受管素材目录。")
            asset_paths.append(logical)

        def mutate(value: dict[str, Any]) -> None:
            source = next(
                (
                    item
                    for item in value["materialSources"]
                    if item["id"] == source_id
                ),
                None,
            )
            if source is None:
                raise WorkspaceFormatError("素材源不存在。")
            variants = [
                item for item in value["materialVariants"]
                if item.get("sourceId") == source_id
            ]
            variant_ids = {str(item.get("id") or "") for item in variants}
            reference_count = sum(
                1
                for action in value["actions"]
                for ref in action.get("frameRefs", [])
                if ref.get("variantId") in variant_ids
            )
            if reference_count:
                raise WorkspaceFormatError("素材的处理版本仍被动作引用，不能删除。")
            removed.update(deepcopy(source))
            removed_variants.extend(deepcopy(variants))
            collect_asset(source.get("video"))
            for frame in source.get("frames", []):
                collect_asset(frame)
                collect_asset(frame.get("linear"))
            for variant in variants:
                for frame in variant.get("frames", []):
                    collect_asset(frame)
                    collect_asset(frame.get("emission"))
            value["materialVariants"] = [
                item for item in value["materialVariants"]
                if item.get("id") not in variant_ids
            ]
            value["materialSources"] = [
                item for item in value["materialSources"] if item["id"] != source_id
            ]
            for character in value["characters"]:
                character["materialSourceIds"] = [
                    item
                    for item in character.get("materialSourceIds", [])
                    if item != source_id
                ]

        self._mutate_domain(
            mutate,
            expected_revision_id=expected_revision_id,
        )
        removed_variant_ids = [str(item["id"]) for item in removed_variants]
        return {
            "removed": removed,
            "removedVariantIds": removed_variant_ids,
            "removedVariantCount": len(removed_variant_ids),
            "referenceCount": 0,
            "assetPaths": asset_paths,
        }

    def remove_domain_asset_files(self, logical_paths: list[str]) -> int:
        return self._remove_domain_asset_files_now(logical_paths)

    def _remove_domain_asset_files_now(self, logical_paths: list[str]) -> int:
        reclaimed = 0
        material_root = (self.root / "materials").resolve(strict=False)
        touched: set[Path] = set()
        for logical in sorted(set(logical_paths)):
            target = resolve_workspace_path(self.root, logical)
            if (
                target.is_file()
                and not is_reparse_point(target)
                and self._is_managed_asset_path(logical)
            ):
                try:
                    size = target.stat().st_size
                    target.unlink(missing_ok=True)
                except OSError as exc:
                    LOGGER.warning(
                        "workspace asset cleanup left an orphan file: %s (%s)",
                        target,
                        exc,
                    )
                    continue
                reclaimed += size
                touched.add(target.parent)
        for directory in sorted(touched, key=lambda item: len(item.parts), reverse=True):
            current = directory
            stop_root = (
                material_root
                if material_root in current.parents
                else (self.root / "characters" / Path(current).parts[-3] / "core-reference").resolve(strict=False)
            )
            while stop_root in current.parents and current != stop_root:
                try:
                    current.rmdir()
                except OSError:
                    break
                current = current.parent
        return reclaimed

    def set_domain_export_state(
        self,
        character_id: str,
        status: str,
        *,
        current_atlas_path: str | None,
        expected_revision_id: str,
    ) -> dict[str, Any]:
        export_state = {
            "status": status,
            "currentAtlas": (
                self._domain_asset_record(
                    current_atlas_path,
                    prefix="atlas",
                )
                if current_atlas_path
                else None
            ),
        }

        def mutate(value: dict[str, Any]) -> None:
            character = next(
                (item for item in value["characters"] if item["id"] == character_id),
                None,
            )
            if character is None:
                raise WorkspaceFormatError("导出状态所属角色不存在。")
            character["exportState"] = export_state

        saved = self._mutate_domain(
            mutate,
            expected_revision_id=expected_revision_id,
        )
        return deepcopy(
            next(
                item
                for item in saved["characters"]
                if item["id"] == character_id
            )["exportState"]
        )

    def _profiles(self, *, for_write: bool = False) -> dict[str, Any]:
        path = self._profiles_path()
        value = self._tracked_read(path, for_write=for_write)

        def validate() -> None:
            validate_aggregate(value, "尺寸档位配置")
            if not isinstance(value.get("profiles"), list):
                raise WorkspaceFormatError("尺寸档位配置无效。")
            self._validate_record_ids(value["profiles"], "尺寸档位")

        self._validate_aggregate_once(path, "size-profiles", value, validate)
        return value

    @staticmethod
    def _validate_record_ids(records: Any, label: str) -> None:
        if not isinstance(records, list) or any(
            not isinstance(item, dict) for item in records
        ):
            raise WorkspaceFormatError(f"{label}列表无效。")
        ids = [validate_stable_id(item.get("id"), label) for item in records]
        if len(ids) != len(set(ids)):
            raise WorkspaceFormatError(f"{label}列表包含重复 ID。")
        assert_no_case_duplicates(ids, label)

    def list_size_profiles(self) -> list[dict[str, Any]]:
        aggregate = self._profiles()
        result = deepcopy(aggregate.get("profiles", []))
        for profile in result:
            profile.setdefault("unit_mode", "unity")
            profile["revisionId"] = str(aggregate.get("revisionId") or "")
        return sorted(
            result,
            key=lambda item: str(item.get("name") or "").casefold(),
        )

    def get_size_profile(self, profile_id: str) -> dict[str, Any] | None:
        return next(
            (item for item in self.list_size_profiles() if item.get("id") == profile_id),
            None,
        )

    def create_size_profile(
        self,
        name: str,
        width_world: float,
        height_world: float,
        unit_mode: str = "unity",
    ) -> dict[str, Any]:
        normalized = normalized_display_name(name)
        if not normalized:
            raise ValueError("尺寸档位名称不能为空。")
        if not 0 < float(width_world) <= 1000 or not 0 < float(height_world) <= 1000:
            raise ValueError("尺寸档位宽高必须大于 0 且不超过 1000 世界单位。")
        if unit_mode not in {"pixels", "unity"}:
            raise ValueError("尺寸档位单位必须是像素或 Unity 世界单位。")
        with self._lock:
            previous = self._profiles(for_write=True)
            if any(item.get("normalized_name") == normalized for item in previous["profiles"]):
                raise ValueError("尺寸档位名称不能重复。")
            now = utc_now()
            profile = {
                "id": new_id("siz"),
                "name": name.strip(),
                "normalized_name": normalized,
                "width_world": float(width_world),
                "height_world": float(height_world),
                "unit_mode": unit_mode,
                "created_at": now,
                "updated_at": now,
            }
            candidate = deepcopy(previous)
            candidate["profiles"].append(profile)
            saved, _ = self._write_aggregate(
                self._profiles_path(), candidate, previous
            )
            result = deepcopy(
                next(item for item in saved["profiles"] if item["id"] == profile["id"])
            )
            result["revisionId"] = str(saved.get("revisionId") or "")
            return result

    def update_size_profile(
        self, profile_id: str, changes: dict[str, Any]
    ) -> dict[str, Any] | None:
        with self._lock:
            previous = self._profiles(for_write=True)
            candidate = deepcopy(previous)
            current = next(
                (item for item in candidate["profiles"] if item.get("id") == profile_id),
                None,
            )
            if current is None:
                return None
            if changes.get("name") is not None:
                name = str(changes["name"]).strip()
                normalized = normalized_display_name(name)
                if not normalized:
                    raise ValueError("尺寸档位名称不能为空。")
                if any(
                    item.get("id") != profile_id
                    and item.get("normalized_name") == normalized
                    for item in candidate["profiles"]
                ):
                    raise ValueError("尺寸档位名称不能重复。")
                current["name"] = name
                current["normalized_name"] = normalized
            for key in ("width_world", "height_world"):
                if changes.get(key) is None:
                    continue
                value = float(changes[key])
                if not 0 < value <= 1000:
                    raise ValueError("尺寸档位宽高必须大于 0 且不超过 1000 世界单位。")
                current[key] = value
            if changes.get("unit_mode") is not None:
                unit_mode = str(changes["unit_mode"])
                if unit_mode not in {"pixels", "unity"}:
                    raise ValueError("尺寸档位单位必须是像素或 Unity 世界单位。")
                current["unit_mode"] = unit_mode
            else:
                current.setdefault("unit_mode", "unity")
            current["updated_at"] = utc_now()
            saved, _ = self._write_aggregate(
                self._profiles_path(), candidate, previous
            )
            result = deepcopy(
                next(item for item in saved["profiles"] if item["id"] == profile_id)
            )
            result["revisionId"] = str(saved.get("revisionId") or "")
            return result

    def delete_size_profile(self, profile_id: str) -> bool:
        with self._lock:
            previous = self._profiles(for_write=True)
            candidate = deepcopy(previous)
            next_profiles = [
                item for item in candidate["profiles"] if item.get("id") != profile_id
            ]
            if len(next_profiles) == len(candidate["profiles"]):
                return False
            candidate["profiles"] = next_profiles
            self._write_aggregate(self._profiles_path(), candidate, previous)
            return True

    def validate(self, *, full_hash: bool = False) -> dict[str, Any]:
        manifest = self._manifest()
        domain = self._domain()
        self._profiles()
        referenced: set[Path] = set()
        for asset in self._iter_domain_assets(domain):
            path = resolve_workspace_path(self.root, str(asset["path"]))
            if not path.is_file() or is_reparse_point(path):
                raise WorkspaceFormatError("格式 3 领域状态引用了缺失或不安全的资产。")
            stat_result = path.stat()
            if stat_result.st_size != int(asset["bytes"]):
                raise WorkspaceFormatError("格式 3 领域资产大小已变化。")
            digest = str(asset["sha256"])
            if full_hash and sha256_file(path) != digest:
                raise WorkspaceFormatError("格式 3 领域资产内容哈希已变化。")
            self._known_asset_state[path] = (
                stat_result.st_size,
                stat_result.st_mtime_ns,
                digest,
            )
            referenced.add(path)
        for path in list(self._known_asset_state):
            if path not in referenced:
                self._known_asset_state.pop(path, None)
        return {
            "workspaceId": manifest["workspaceId"],
            "name": manifest["name"],
            "characters": len(domain.get("characters") or []),
            "referencedFiles": len(referenced),
            "fullHash": full_hash,
        }

    def create_job(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self.runtime.create_job(*args, **kwargs)

    def _decorate_job(self, job: dict[str, Any]) -> dict[str, Any]:
        result = deepcopy(job)
        domain = self._domain()
        character_id = str(result.get("character_id") or "")
        source_id = str(result.get("source_id") or "")
        character = next(
            (item for item in domain.get("characters") or [] if item.get("id") == character_id),
            None,
        )
        source = next(
            (item for item in domain.get("materialSources") or [] if item.get("id") == source_id),
            None,
        )
        result["character_name"] = character.get("name") if character else None
        result["source_name"] = source.get("name") if source else None
        return result

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        job = self.runtime.get_job(job_id)
        return self._decorate_job(job) if job else None

    def list_jobs(
        self,
        limit: int = 100,
        character_id: str | None = None,
    ) -> list[dict[str, Any]]:
        jobs = self.runtime.list_jobs(limit=max(limit, 1000), character_id=character_id)
        return [self._decorate_job(item) for item in jobs[:limit]]

    def update_job(self, job_id: str, **changes: Any) -> dict[str, Any] | None:
        updated = self.runtime.update_job(job_id, **changes)
        return self._decorate_job(updated) if updated else None

    def append_job_log(self, job_id: str, level: str, message: str) -> None:
        self.runtime.append_job_log(job_id, level, message)

    def cancel_job(self, job_id: str) -> dict[str, Any] | None:
        updated = self.runtime.request_cancel(job_id)
        return self._decorate_job(updated) if updated else None

    def recover_jobs(self) -> list[str]:
        return self.runtime.recover_jobs()
