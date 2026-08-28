from __future__ import annotations

import ctypes
import hashlib
import json
import math
import os
import re
import stat
import unicodedata
import uuid
from copy import deepcopy
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from contracts.product import WORKSPACE_FORMAT_VERSION


WORKSPACE_KIND = "rotoweave-workspace"
WORKSPACE_DOMAIN_KIND = "rotoweave-workspace-domain"
AGGREGATE_SCHEMA_VERSION = 1
WORKSPACE_MANIFEST = "rotoweave.json"
LEGACY_WORKSPACE_MANIFEST = "aiframe.json"
LEGACY_WORKSPACE_KIND = "aiframe-workspace"
LEGACY_WORKSPACE_DOMAIN_KIND = "aiframe-workspace-domain"
WORKSPACE_DOMAIN = "domain/workspace-state.json"
DOMAIN_SCHEMA_REVISION = 7

_SAFE_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{2,127}$")
_WINDOWS_RESERVED = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
}
_VOL_NAME_NTFS = "NTFS"
_INVALID_WINDOWS_PATH_CHARS = set('<>:"|?*')


class WorkspaceError(RuntimeError):
    """Base error for a user-owned folder workspace."""


class WorkspaceFormatError(WorkspaceError):
    """The selected folder is not a valid current workspace."""


class WorkspaceChangedError(WorkspaceError):
    """A tracked aggregate changed outside the active session."""


class WorkspaceRevisionConflict(WorkspaceError):
    """A browser attempted to mutate an aggregate from an older revision."""


class WorkspaceReadOnlyError(WorkspaceError):
    """A mutation was attempted against a read-only workspace."""


def safe_workspace_name_segment(
    value: object, *, fallback: str = "未命名角色", max_length: int = 80
) -> str:
    """Return a readable Windows path segment without embedding identity.

    Stable IDs remain authoritative.  This segment is only the human-readable
    prefix used by newly-created character directories.
    """

    normalized = unicodedata.normalize("NFKC", str(value or "")).strip()
    cleaned = "".join(
        "_"
        if character in _INVALID_WINDOWS_PATH_CHARS
        or character in {"/", "\\"}
        or ord(character) < 32
        else character
        for character in normalized
    ).rstrip(" .")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    cleaned = cleaned[:max_length].rstrip(" .") or fallback
    if cleaned.split(".", 1)[0].casefold() in _WINDOWS_RESERVED:
        cleaned = f"_{cleaned}"
    return cleaned


def safe_workspace_filename(
    value: object, *, fallback_stem: str = "素材", fallback_suffix: str = ""
) -> str:
    """Keep an imported basename readable while making it workspace-safe."""

    basename = str(value or "").replace("\\", "/").rsplit("/", 1)[-1]
    parsed = Path(basename)
    suffix = parsed.suffix.lower() or fallback_suffix.lower()
    if suffix and (not suffix.startswith(".") or len(suffix) > 16):
        suffix = fallback_suffix.lower()
    stem = parsed.stem if parsed.suffix else basename
    safe_stem = safe_workspace_name_segment(
        stem, fallback=fallback_stem, max_length=max(1, 160 - len(suffix))
    )
    return f"{safe_stem}{suffix}"


def canonical_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def semantic_payload(value: dict[str, Any]) -> dict[str, Any]:
    volatile = {"contentHash", "revisionId", "updatedAt", "updated_at"}

    def clean(item: Any) -> Any:
        if isinstance(item, dict):
            return {
                key: clean(child)
                for key, child in item.items()
                if key not in volatile
            }
        if isinstance(item, list):
            return [clean(child) for child in item]
        return deepcopy(item)

    return clean(value)


def semantic_hash(value: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json_bytes(semantic_payload(value))).hexdigest()


def finalize_aggregate(
    value: dict[str, Any],
    *,
    previous: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], bool]:
    candidate = deepcopy(value)
    candidate.setdefault("schemaVersion", AGGREGATE_SCHEMA_VERSION)
    next_hash = semantic_hash(candidate)
    if previous is not None and str(previous.get("contentHash") or "") == next_hash:
        return deepcopy(previous), False
    candidate["revisionId"] = f"rev_{uuid.uuid4().hex}"
    candidate["contentHash"] = next_hash
    return candidate, True


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def _json_object_without_duplicate_keys(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise WorkspaceFormatError(f"工作区配置包含重复 JSON 字段：{key}。")
        value[key] = item
    return value


def read_json(path: Path) -> tuple[dict[str, Any], str]:
    try:
        payload = path.read_bytes()
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_json_object_without_duplicate_keys,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkspaceFormatError(f"无法读取工作区配置：{path.name}。") from exc
    if not isinstance(value, dict):
        raise WorkspaceFormatError(f"工作区配置必须是 JSON 对象：{path.name}。")
    if canonical_json_bytes(value) != payload:
        raise WorkspaceFormatError(
            f"工作区配置不是规范 UTF-8/LF/稳定排序格式：{path.name}。"
        )
    return value, sha256_bytes(payload)


def atomic_write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.part")
    try:
        with temporary.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_write_json(path: Path, value: dict[str, Any]) -> str:
    payload = canonical_json_bytes(value)
    atomic_write_bytes(path, payload)
    return sha256_bytes(payload)


def validate_stable_id(value: object, label: str) -> str:
    text = str(value or "")
    if not _SAFE_ID.fullmatch(text):
        raise WorkspaceFormatError(f"{label}包含无效稳定 ID。")
    return text


def validate_logical_path(value: object) -> str:
    raw = str(value or "")
    if not raw or "\\" in raw:
        raise WorkspaceFormatError("工作区资源路径必须使用相对 POSIX 路径。")
    logical = PurePosixPath(raw)
    if logical.is_absolute() or any(part in {"", ".", ".."} for part in logical.parts):
        raise WorkspaceFormatError("工作区资源路径不能是绝对路径或包含上级目录。")
    for part in logical.parts:
        if any(character in _INVALID_WINDOWS_PATH_CHARS or ord(character) < 32 for character in part):
            raise WorkspaceFormatError("工作区资源路径包含 Windows 不允许的字符。")
        if part.endswith((" ", ".")):
            raise WorkspaceFormatError("工作区资源路径不能以空格或点结尾。")
        stem = part.split(".", 1)[0].casefold()
        if stem in _WINDOWS_RESERVED:
            raise WorkspaceFormatError("工作区资源路径包含 Windows 保留名称。")
    return logical.as_posix()


def validate_aggregate(
    value: dict[str, Any],
    label: str,
    *,
    content_hash_verified: bool = False,
) -> None:
    if value.get("schemaVersion") != AGGREGATE_SCHEMA_VERSION:
        raise WorkspaceFormatError(f"{label}使用了不支持的聚合版本。")
    revision_id = str(value.get("revisionId") or "")
    content_hash = str(value.get("contentHash") or "")
    if not re.fullmatch(r"rev_[0-9a-f]{32}", revision_id):
        raise WorkspaceFormatError(f"{label}缺少有效 revisionId。")
    if not re.fullmatch(r"[0-9a-f]{64}", content_hash):
        raise WorkspaceFormatError(f"{label}缺少有效 contentHash。")
    if not content_hash_verified and semantic_hash(value) != content_hash:
        raise WorkspaceFormatError(f"{label}内容摘要不一致。")


def resolve_workspace_path(root: Path, logical_path: object) -> Path:
    logical = validate_logical_path(logical_path)
    root_resolved = root.resolve()
    candidate = root_resolved.joinpath(*PurePosixPath(logical).parts)
    resolved = candidate.resolve(strict=False)
    if resolved == root_resolved or root_resolved not in resolved.parents:
        raise WorkspaceFormatError("工作区资源路径越过了工作区边界。")
    current = root_resolved
    for part in PurePosixPath(logical).parts:
        current /= part
        if current.exists() and is_reparse_point(current):
            raise WorkspaceFormatError("工作区受管路径不能包含链接或目录联接。")
    return resolved


def logical_workspace_path(root: Path, path: Path) -> str:
    root_resolved = root.resolve()
    resolved = path.resolve(strict=False)
    if root_resolved not in resolved.parents:
        raise WorkspaceFormatError("正式资源必须位于当前工作区。")
    logical = validate_logical_path(resolved.relative_to(root_resolved).as_posix())
    if resolve_workspace_path(root_resolved, logical) != resolved:
        raise WorkspaceFormatError("工作区受管路径不能经过链接或目录联接。")
    return logical


def is_reparse_point(path: Path) -> bool:
    try:
        attributes = path.lstat().st_file_attributes
    except (AttributeError, OSError):
        return path.is_symlink()
    return bool(attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def assert_no_case_duplicates(values: Iterable[str], label: str) -> None:
    seen: dict[str, str] = {}
    for value in values:
        folded = value.casefold()
        if folded in seen and seen[folded] != value:
            raise WorkspaceFormatError(f"{label}包含仅大小写不同的重复项。")
        seen[folded] = value


def workspace_volume_filesystem(path: Path) -> str | None:
    if os.name != "nt":
        return None
    root = Path(path.anchor or path.resolve().anchor)
    if not root.anchor or str(path).startswith("\\\\"):
        return None
    volume_name = ctypes.create_unicode_buffer(261)
    filesystem_name = ctypes.create_unicode_buffer(261)
    serial = ctypes.c_ulong()
    max_component = ctypes.c_ulong()
    flags = ctypes.c_ulong()
    ok = ctypes.windll.kernel32.GetVolumeInformationW(
        ctypes.c_wchar_p(str(root)),
        volume_name,
        len(volume_name),
        ctypes.byref(serial),
        ctypes.byref(max_component),
        ctypes.byref(flags),
        filesystem_name,
        len(filesystem_name),
    )
    return filesystem_name.value if ok else None


def workspace_writable_reason(root: Path) -> str | None:
    if str(root).startswith("\\\\"):
        return "首版只允许本机 NTFS 工作区，网络共享只能在外部复制后打开。"
    for candidate in (root, *root.parents):
        if candidate.exists() and is_reparse_point(candidate):
            return "工作区路径不能经过符号链接或目录联接。"
    filesystem = workspace_volume_filesystem(root)
    if os.name == "nt" and filesystem != _VOL_NAME_NTFS:
        return f"首版只允许本机 NTFS 工作区，当前文件系统为 {filesystem or '未知'}。"
    return None


def verify_workspace_atomic_replace(root: Path) -> None:
    """Prove that the selected NTFS folder supports same-directory replace."""

    token = uuid.uuid4().hex
    source = root / f".rotoweave-write-probe-{token}.part"
    target = root / f".rotoweave-write-probe-{token}.replace.part"
    try:
        with source.open("xb") as handle:
            handle.write(b"RotoWeave workspace probe\n")
            handle.flush()
            os.fsync(handle.fileno())
        source.replace(target)
        if target.read_bytes() != b"RotoWeave workspace probe\n":
            raise OSError("replace readback mismatch")
    except OSError as exc:
        raise WorkspaceReadOnlyError(
            "所选工作区不支持可靠的同目录原子替换。"
        ) from exc
    finally:
        for candidate in (source, target):
            try:
                candidate.unlink(missing_ok=True)
            except OSError:
                pass


def new_workspace_manifest(name: str) -> dict[str, Any]:
    value = {
        "kind": WORKSPACE_KIND,
        "workspaceFormatVersion": WORKSPACE_FORMAT_VERSION,
        "workspaceId": f"wrk_{uuid.uuid4().hex}",
        "name": name.strip(),
        "characters": [],
        "domainState": WORKSPACE_DOMAIN,
    }
    finalized, _ = finalize_aggregate(value)
    return finalized


def new_profiles_manifest() -> dict[str, Any]:
    finalized, _ = finalize_aggregate({"profiles": []})
    return finalized


def new_workspace_domain() -> dict[str, Any]:
    finalized, _ = finalize_aggregate(
        {
            "kind": WORKSPACE_DOMAIN_KIND,
            "workspaceFormatVersion": WORKSPACE_FORMAT_VERSION,
            "domainSchemaRevision": DOMAIN_SCHEMA_REVISION,
            "characters": [],
            "actions": [],
            "materialSources": [],
            "materialVariants": [],
        }
    )
    return finalized


def _domain_records(value: dict[str, Any], key: str, label: str) -> list[dict[str, Any]]:
    records = value.get(key)
    if not isinstance(records, list) or any(not isinstance(item, dict) for item in records):
        raise WorkspaceFormatError(f"{label}列表无效。")
    ids = [validate_stable_id(item.get("id"), label) for item in records]
    if len(ids) != len(set(ids)):
        raise WorkspaceFormatError(f"{label}列表包含重复 ID。")
    assert_no_case_duplicates(ids, label)
    return records


def _validate_domain_asset(value: Any, label: str) -> None:
    if not isinstance(value, dict):
        raise WorkspaceFormatError(f"{label}资产记录无效。")
    validate_stable_id(value.get("id"), label)
    validate_logical_path(value.get("path"))
    if not re.fullmatch(r"[0-9a-f]{64}", str(value.get("sha256") or "")):
        raise WorkspaceFormatError(f"{label}缺少有效 SHA-256。")
    size = value.get("bytes")
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        raise WorkspaceFormatError(f"{label}文件大小无效。")


def _domain_number(
    value: Any,
    label: str,
    *,
    minimum: float = 0.0,
    strictly_positive: bool = False,
) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise WorkspaceFormatError(f"{label}无效。")
    normalized = float(value)
    if not math.isfinite(normalized) or (
        normalized <= minimum if strictly_positive else normalized < minimum
    ):
        raise WorkspaceFormatError(f"{label}无效。")
    return normalized


def validate_workspace_domain(
    value: dict[str, Any], *, content_hash_verified: bool = False
) -> None:
    """Validate the complete format-3 business graph without implicit migration."""

    validate_aggregate(
        value,
        "格式 3 领域状态",
        content_hash_verified=content_hash_verified,
    )
    if value.get("kind") != WORKSPACE_DOMAIN_KIND:
        raise WorkspaceFormatError("格式 3 领域状态类型无效。")
    if value.get("workspaceFormatVersion") != WORKSPACE_FORMAT_VERSION:
        raise WorkspaceFormatError(
            f"领域状态仅支持工作区格式 {WORKSPACE_FORMAT_VERSION}。"
        )
    if value.get("domainSchemaRevision") != DOMAIN_SCHEMA_REVISION:
        raise WorkspaceFormatError(
            f"领域状态仅支持内部版本 {DOMAIN_SCHEMA_REVISION}。"
        )
    characters = _domain_records(value, "characters", "领域角色")
    actions = _domain_records(value, "actions", "动作")
    sources = _domain_records(value, "materialSources", "素材源")
    variants = _domain_records(value, "materialVariants", "素材版本")
    character_by_id = {str(item["id"]): item for item in characters}
    action_by_id = {str(item["id"]): item for item in actions}
    source_by_id = {str(item["id"]): item for item in sources}
    variant_by_id = {str(item["id"]): item for item in variants}

    for character in characters:
        if not str(character.get("name") or "").strip():
            raise WorkspaceFormatError("领域角色名称不能为空。")
        action_ids = character.get("actionIds")
        source_ids = character.get("materialSourceIds")
        if not isinstance(action_ids, list) or not isinstance(source_ids, list):
            raise WorkspaceFormatError("领域角色引用索引无效。")
        if len(action_ids) != len(set(action_ids)) or len(source_ids) != len(set(source_ids)):
            raise WorkspaceFormatError("领域角色引用索引包含重复项。")
        if any(action_id not in action_by_id for action_id in action_ids):
            raise WorkspaceFormatError("领域角色引用了不存在的动作。")
        if any(source_id not in source_by_id for source_id in source_ids):
            raise WorkspaceFormatError("领域角色引用了不存在的素材源。")
        export_state = character.get("exportState")
        if not isinstance(export_state, dict) or export_state.get("status") not in {
            "not-exported",
            "stale",
            "current",
            "failed",
        }:
            raise WorkspaceFormatError("角色导出状态无效。")
        current_atlas = export_state.get("currentAtlas")
        if current_atlas is not None:
            _validate_domain_asset(current_atlas, "当前图集")
        if value.get("domainSchemaRevision") == DOMAIN_SCHEMA_REVISION:
            calibration = character.get("calibration")
            shadow = character.get("shadow")
            delivery = character.get("delivery")
            if not isinstance(calibration, dict) or not isinstance(shadow, dict) or not isinstance(delivery, dict):
                raise WorkspaceFormatError("角色缺少校准、阴影或导出设置。")
            profiles = calibration.get("sizeProfiles")
            if not isinstance(profiles, list) or not profiles:
                raise WorkspaceFormatError("角色至少需要一个尺寸档位。")
            if calibration.get("activeSizeProfileId") not in {item.get("id") for item in profiles if isinstance(item, dict)}:
                raise WorkspaceFormatError("角色当前尺寸档位无效。")
            for profile in profiles:
                if not isinstance(profile, dict) or not str(profile.get("id") or ""):
                    raise WorkspaceFormatError("角色尺寸档位无效。")
                if profile.get("presetId") is not None:
                    validate_stable_id(profile.get("presetId"), "角色尺寸预设")
                if profile.get("unitMode") not in {"pixels", "unity"}:
                    raise WorkspaceFormatError("尺寸档位单位必须是像素或 Unity 世界单位。")
                width = _domain_number(profile.get("width"), "尺寸档位宽度", strictly_positive=True)
                height = _domain_number(profile.get("height"), "尺寸档位高度", strictly_positive=True)
                maximum = 16384 if profile.get("unitMode") == "pixels" else 163.84
                if width > maximum or height > maximum:
                    raise WorkspaceFormatError("尺寸档位超过当前单位的合法范围。")
            ppu = _domain_number(calibration.get("pixelsPerUnit"), "角色校准 PPU", strictly_positive=True)
            if abs(ppu - 100.0) > 0.0001:
                raise WorkspaceFormatError("格式 3 校准固定使用 100 px/Unity unit。")
            for key in ("sizeGuideCenterX", "sizeGuideBottomY", "alignmentHorizonY", "shadowStandardY"):
                _domain_number(calibration.get(key), f"角色校准 {key}", minimum=-65536)
            if not isinstance(shadow.get("enabled"), bool) or not re.fullmatch(r"#[0-9a-fA-F]{6}", str(shadow.get("color") or "")):
                raise WorkspaceFormatError("角色全局阴影设置无效。")
            _domain_number(shadow.get("baseOpacity"), "角色阴影透明度")
            _domain_number(shadow.get("lightAngleDegrees"), "角色阴影光源角度", minimum=-36000)
            atlas = delivery.get("atlas")
            if not isinstance(atlas, dict) or atlas.get("maxSize") not in {2048, 4096, 8192}:
                raise WorkspaceFormatError("角色图集设置无效。")
            atlas_limits = {
                "padding": (0, 128),
                "extrude": (0, 32),
                "framePadding": (0, 256),
            }
            for key, (minimum, maximum) in atlas_limits.items():
                setting = atlas.get(key)
                if (
                    isinstance(setting, bool)
                    or not isinstance(setting, int)
                    or setting < minimum
                    or setting > maximum
                ):
                    raise WorkspaceFormatError("角色图集间距、扩边或紧裁留白无效。")
            action_settings = delivery.get("actionSettings")
            if not isinstance(action_settings, dict):
                raise WorkspaceFormatError("角色逐动作导出设置无效。")
            for action_id, setting in action_settings.items():
                if action_id not in action_ids or not isinstance(setting, dict):
                    raise WorkspaceFormatError("角色逐动作导出设置引用无效。")
                if not isinstance(setting.get("runtimeLoop"), bool) or not isinstance(setting.get("includeInExport"), bool):
                    raise WorkspaceFormatError("角色逐动作循环或参与导出设置无效。")
                _domain_number(setting.get("textureScale"), "动作纹理比例", strictly_positive=True)

    source_frame_ids: set[str] = set()
    for source in sources:
        character_id = str(source.get("characterId") or "")
        if character_id not in character_by_id:
            raise WorkspaceFormatError("素材源引用了不存在的角色。")
        if source["id"] not in character_by_id[character_id]["materialSourceIds"]:
            raise WorkspaceFormatError("素材源未被所属角色索引。")
        if not str(source.get("displayName") or "").strip():
            raise WorkspaceFormatError("素材源名称不能为空。")
        _validate_domain_asset(source.get("video"), "源视频")
        metadata = source.get("metadata")
        if not isinstance(metadata, dict):
            raise WorkspaceFormatError("素材源缺少视频元数据。")
        _domain_number(metadata.get("fps"), "素材帧率", strictly_positive=True)
        _domain_number(metadata.get("durationSeconds"), "素材时长")
        width = _domain_number(metadata.get("width"), "素材宽度", strictly_positive=True)
        height = _domain_number(metadata.get("height"), "素材高度", strictly_positive=True)
        if not float(width).is_integer() or not float(height).is_integer():
            raise WorkspaceFormatError("素材尺寸必须是整数。")
        warnings = metadata.get("warnings", [])
        if not isinstance(warnings, list) or any(not isinstance(item, str) for item in warnings):
            raise WorkspaceFormatError("素材警告列表无效。")
        color = metadata.get("color")
        if not isinstance(color, dict):
            raise WorkspaceFormatError("素材颜色信息无效。")
        variant_ids = source.get("variantIds")
        if (
            not isinstance(variant_ids, list)
            or len(variant_ids) != len(set(variant_ids))
            or any(
                variant_id not in variant_by_id
                or variant_by_id[variant_id].get("sourceId") != source["id"]
                for variant_id in variant_ids
            )
        ):
            raise WorkspaceFormatError("素材源版本索引无效。")
        frames = source.get("frames")
        if not isinstance(frames, list) or not frames:
            raise WorkspaceFormatError("素材源帧列表无效。")
        if metadata.get("frameCount") != len(frames):
            raise WorkspaceFormatError("素材源帧数与元数据不一致。")
        for index, frame in enumerate(frames):
            _validate_domain_asset(frame, "源帧")
            if frame["id"] in source_frame_ids or frame.get("index") != index:
                raise WorkspaceFormatError("源帧 ID 或顺序无效。")
            _domain_number(frame.get("ptsUs"), "源帧时间戳")
            _domain_number(frame.get("durationUs"), "源帧时长", strictly_positive=True)
            _domain_number(frame.get("width"), "源帧宽度", strictly_positive=True)
            _domain_number(frame.get("height"), "源帧高度", strictly_positive=True)
            _validate_domain_asset(frame.get("linear"), "线性源帧")
            source_frame_ids.add(str(frame["id"]))

    variant_frame_ids: dict[str, set[str]] = {}
    for variant in variants:
        source_id = str(variant.get("sourceId") or "")
        if source_id not in source_by_id:
            raise WorkspaceFormatError("素材版本引用了不存在的素材源。")
        if variant.get("kind") not in {"basic", "high", "ultra", "photoshop"}:
            raise WorkspaceFormatError("素材版本处理类型无效。")
        if not isinstance(variant.get("settings"), dict):
            raise WorkspaceFormatError("素材版本缺少设置快照。")
        if not re.fullmatch(r"[0-9a-f]{64}", str(variant.get("settingsSha256") or "")):
            raise WorkspaceFormatError("素材版本缺少有效的设置摘要。")
        frames = variant.get("frames")
        source_frames = source_by_id[source_id].get("frames") or []
        source_frame_ids_for_variant = {str(item["id"]) for item in source_frames}
        if not isinstance(frames, list) or not frames:
            raise WorkspaceFormatError("素材版本帧列表无效。")
        ids: set[str] = set()
        mapped_source_ids: set[str] = set()
        for index, frame in enumerate(frames):
            _validate_domain_asset(frame, "处理帧")
            if frame.get("emission") is not None:
                _validate_domain_asset(frame.get("emission"), "处理帧特效层")
            if frame["id"] in ids or frame.get("index") != index:
                raise WorkspaceFormatError("处理帧 ID 或顺序无效。")
            source_frame_id = str(frame.get("sourceFrameId") or "")
            if (
                source_frame_id not in source_frame_ids_for_variant
                or source_frame_id in mapped_source_ids
            ):
                raise WorkspaceFormatError("处理帧的源帧映射无效。")
            ids.add(str(frame["id"]))
            mapped_source_ids.add(source_frame_id)
        variant_frame_ids[str(variant["id"])] = ids

    names_by_character: dict[str, set[str]] = {}
    frame_ref_ids: set[str] = set()
    for action in actions:
        character_id = str(action.get("characterId") or "")
        if character_id not in character_by_id:
            raise WorkspaceFormatError("动作引用了不存在的角色。")
        if action["id"] not in character_by_id[character_id]["actionIds"]:
            raise WorkspaceFormatError("动作未被所属角色索引。")
        name = str(action.get("name") or "").strip()
        normalized = unicodedata.normalize("NFKC", name).casefold()
        if not normalized or normalized in names_by_character.setdefault(character_id, set()):
            raise WorkspaceFormatError("同一角色的动作名称不能为空或重复。")
        names_by_character[character_id].add(normalized)
        if not isinstance(action.get("previewLoop", action.get("loop", True)), bool):
            raise WorkspaceFormatError("动作循环设置无效。")
        refs = action.get("frameRefs")
        if not isinstance(refs, list):
            raise WorkspaceFormatError("动作帧引用列表无效。")
        for ref in refs:
            if not isinstance(ref, dict):
                raise WorkspaceFormatError("动作帧引用无效。")
            ref_id = validate_stable_id(ref.get("id"), "动作帧引用")
            if ref_id in frame_ref_ids:
                raise WorkspaceFormatError("动作帧引用 ID 重复。")
            frame_ref_ids.add(ref_id)
            if value.get("domainSchemaRevision") == DOMAIN_SCHEMA_REVISION and not isinstance(ref.get("enabled"), bool):
                raise WorkspaceFormatError("动作帧启用状态无效。")
            variant_id = str(ref.get("variantId") or "")
            frame_id = str(ref.get("frameId") or "")
            if variant_id not in variant_by_id or frame_id not in variant_frame_ids[variant_id]:
                raise WorkspaceFormatError("动作帧引用了不存在的不可变版本帧。")
            duration = ref.get("durationSeconds")
            if not isinstance(duration, (int, float)) or isinstance(duration, bool) or duration <= 0:
                raise WorkspaceFormatError("动作帧时长必须大于零。")
            transform = ref.get("transform")
            if not isinstance(transform, dict):
                raise WorkspaceFormatError("动作帧缺少变换参数。")
            if not isinstance(transform.get("position"), dict) or not isinstance(transform.get("scale"), dict):
                raise WorkspaceFormatError("动作帧位置或缩放参数无效。")
            position = transform["position"]
            scale = transform["scale"]
            numeric_values = [
                position.get("x"),
                position.get("y"),
                scale.get("x"),
                scale.get("y"),
                transform.get("rotationDegrees"),
            ]
            if any(
                not isinstance(item, (int, float))
                or isinstance(item, bool)
                or not math.isfinite(float(item))
                for item in numeric_values
            ) or float(scale["x"]) <= 0 or float(scale["y"]) <= 0:
                raise WorkspaceFormatError("动作帧变换数值无效。")
            if not re.fullmatch(r"#[0-9a-fA-F]{6}", str(transform.get("color") or "")):
                raise WorkspaceFormatError("动作帧覆盖颜色无效。")
            opacity = transform.get("opacity")
            if not isinstance(opacity, (int, float)) or isinstance(opacity, bool) or not 0 <= opacity <= 1:
                raise WorkspaceFormatError("动作帧透明度无效。")
            shadow = transform.get("shadow")
            if not isinstance(shadow, dict) or shadow.get("enabled") is not None and not isinstance(shadow.get("enabled"), bool):
                raise WorkspaceFormatError("动作帧阴影参数无效。")
            if shadow.get("color") is not None and not re.fullmatch(r"#[0-9a-fA-F]{6}", str(shadow.get("color") or "")):
                raise WorkspaceFormatError("动作帧阴影颜色无效。")
            shadow_opacity = shadow.get("opacity")
            if (
                shadow_opacity is not None and (not isinstance(shadow_opacity, (int, float))
                or isinstance(shadow_opacity, bool)
                or not 0 <= float(shadow_opacity) <= 1)
                or not isinstance(shadow.get("offset"), dict)
                or not isinstance(shadow.get("scale"), dict)
            ):
                raise WorkspaceFormatError("动作帧阴影参数无效。")
            shadow_values = [
                shadow["offset"].get("x"),
                shadow["offset"].get("y"),
                shadow["scale"].get("x"),
                shadow["scale"].get("y"),
            ]
            if any(
                not isinstance(item, (int, float))
                or isinstance(item, bool)
                or not math.isfinite(float(item))
                for item in shadow_values
            ) or float(shadow["scale"]["x"]) <= 0 or float(shadow["scale"]["y"]) <= 0:
                raise WorkspaceFormatError("动作帧阴影变换数值无效。")


def validate_workspace_manifest(value: dict[str, Any]) -> None:
    validate_aggregate(value, "工作区清单")
    if value.get("kind") != WORKSPACE_KIND:
        raise WorkspaceFormatError("所选目录不是 RotoWeave 工作区。")
    if value.get("workspaceFormatVersion") != WORKSPACE_FORMAT_VERSION:
        if value.get("workspaceFormatVersion") in {1, 2}:
            raise WorkspaceFormatError(
                "这是旧版工作区格式；RotoWeave 4.0 不迁移或读取格式 2。"
            )
        raise WorkspaceFormatError(
            f"仅支持工作区格式 {WORKSPACE_FORMAT_VERSION}。"
        )
    validate_stable_id(value.get("workspaceId"), "工作区")
    if not str(value.get("name") or "").strip():
        raise WorkspaceFormatError("工作区名称不能为空。")
    if value.get("characters") != []:
        raise WorkspaceFormatError("格式 3 清单不得包含已退役的 classic 角色索引。")
    if value.get("domainState") != WORKSPACE_DOMAIN:
        raise WorkspaceFormatError("格式 3 工作区缺少领域状态入口。")


def inspect_legacy_workspace(root: Path) -> dict[str, Any]:
    target = root.expanduser().absolute().resolve(strict=False)
    canonical_path = target / WORKSPACE_MANIFEST
    legacy_path = target / LEGACY_WORKSPACE_MANIFEST
    if canonical_path.is_file() and legacy_path.is_file():
        raise WorkspaceFormatError(
            "rotoweave.json 与 aiframe.json 同时存在，禁止猜测或自动合并。"
        )
    if canonical_path.is_file():
        manifest, _ = read_json(canonical_path)
        validate_workspace_manifest(manifest)
        return {"state": "current", "migratable": False, "name": manifest["name"]}
    if not legacy_path.is_file():
        return {"state": "not-workspace", "migratable": False, "name": None}
    legacy_manifest, _ = read_json(legacy_path)
    validate_aggregate(legacy_manifest, "AIFrameTools 4.0 工作区清单")
    if (
        legacy_manifest.get("kind") != LEGACY_WORKSPACE_KIND
        or legacy_manifest.get("workspaceFormatVersion") != WORKSPACE_FORMAT_VERSION
        or legacy_manifest.get("domainState") != WORKSPACE_DOMAIN
    ):
        raise WorkspaceFormatError(
            "旧工作区不是精确的 AIFrameTools 4.0 / Workspace Format 3 契约。"
        )
    validate_stable_id(legacy_manifest.get("workspaceId"), "旧工作区")
    if not str(legacy_manifest.get("name") or "").strip():
        raise WorkspaceFormatError("旧工作区名称不能为空。")
    legacy_domain, _ = read_json(target / WORKSPACE_DOMAIN)
    validate_aggregate(legacy_domain, "AIFrameTools 4.0 领域状态")
    if (
        legacy_domain.get("kind") != LEGACY_WORKSPACE_DOMAIN_KIND
        or legacy_domain.get("workspaceFormatVersion") != WORKSPACE_FORMAT_VERSION
        or legacy_domain.get("domainSchemaRevision") != DOMAIN_SCHEMA_REVISION
    ):
        raise WorkspaceFormatError(
            "旧工作区领域状态不是精确的 AIFrameTools 4.0 / Domain 7 契约。"
        )
    return {
        "state": "legacy-migratable",
        "migratable": True,
        "name": legacy_manifest["name"],
    }


def migrate_legacy_workspace(root: Path) -> dict[str, Any]:
    target = root.expanduser().absolute().resolve(strict=False)
    inspection = inspect_legacy_workspace(target)
    if inspection["state"] != "legacy-migratable":
        raise WorkspaceFormatError("所选目录没有可迁移的 AIFrameTools 4.0 工作区。")
    verify_workspace_atomic_replace(target)
    legacy_path = target / LEGACY_WORKSPACE_MANIFEST
    canonical_path = target / WORKSPACE_MANIFEST
    domain_path = target / WORKSPACE_DOMAIN
    legacy_backup = target / "aiframe.json.aiframetools-4.0.bak"
    domain_backup = target / "domain" / "workspace-state.aiframetools-4.0.bak.json"
    if canonical_path.exists() or legacy_backup.exists() or domain_backup.exists():
        raise WorkspaceFormatError("迁移目标或回滚备份已存在，禁止覆盖或合并。")
    legacy_bytes = legacy_path.read_bytes()
    domain_bytes = domain_path.read_bytes()
    legacy_manifest, _ = read_json(legacy_path)
    legacy_domain, _ = read_json(domain_path)
    migrated_manifest, _ = finalize_aggregate(
        {**legacy_manifest, "kind": WORKSPACE_KIND}, previous=legacy_manifest
    )
    migrated_domain, _ = finalize_aggregate(
        {**legacy_domain, "kind": WORKSPACE_DOMAIN_KIND}, previous=legacy_domain
    )
    validate_workspace_manifest(migrated_manifest)
    validate_workspace_domain(migrated_domain)
    atomic_write_bytes(legacy_backup, legacy_bytes)
    atomic_write_bytes(domain_backup, domain_bytes)
    domain_changed = False
    try:
        atomic_write_json(domain_path, migrated_domain)
        domain_changed = True
        atomic_write_json(canonical_path, migrated_manifest)
        legacy_path.unlink()
    except Exception:
        canonical_path.unlink(missing_ok=True)
        if domain_changed:
            atomic_write_bytes(domain_path, domain_bytes)
        raise
    return {
        "state": "migrated",
        "migratable": False,
        "name": migrated_manifest["name"],
        "manifest": WORKSPACE_MANIFEST,
        "legacyBackup": legacy_backup.name,
        "domainBackup": domain_backup.relative_to(target).as_posix(),
    }


def create_workspace(root: Path, name: str) -> dict[str, Any]:
    target = root.resolve(strict=False)
    if target.exists() and any(target.iterdir()):
        raise WorkspaceFormatError("新工作区目录必须不存在或为空。")
    target.mkdir(parents=True, exist_ok=True)
    if is_reparse_point(target):
        raise WorkspaceFormatError("新工作区目录不能是链接或目录联接。")
    manifest = new_workspace_manifest(name)
    atomic_write_json(target / WORKSPACE_MANIFEST, manifest)
    atomic_write_json(target / "global" / "size-profiles.json", new_profiles_manifest())
    atomic_write_json(target / WORKSPACE_DOMAIN, new_workspace_domain())
    (target / "characters").mkdir(parents=True, exist_ok=True)
    return manifest
