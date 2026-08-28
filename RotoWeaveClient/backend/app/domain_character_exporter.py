from __future__ import annotations

import errno
import hashlib
import json
import math
import os
import shutil
import uuid
import zipfile
from copy import deepcopy
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any

from PIL import Image

from . import __version__
from contracts.product import (
    CANONICAL_PIXELS_PER_UNIT,
    CHARACTER_PACKAGE_FORMAT,
    CHARACTER_PACKAGE_SHAPE,
    COORDINATE_CONTRACT,
)
from .storage import sha256_file
from .domain_shadows import resolve_domain_action_shadows
from .workspace_format import (
    WorkspaceFormatError,
    logical_workspace_path,
    resolve_workspace_path,
)
from .workspace_repository import validate_unity_delivery_archive


ATLAS_PADDING = 2
PACKAGE_NAME = "character.rotoweave"


def _stable_id(prefix: str, *parts: object) -> str:
    payload = ":".join(str(part) for part in parts).encode("utf-8")
    return f"{prefix}_{hashlib.sha256(payload).hexdigest()[:24]}"


def _json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _zip_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = (
        zipfile.ZIP_STORED if name.endswith(".png") else zipfile.ZIP_DEFLATED
    )
    info.external_attr = 0o644 << 16
    return info


@dataclass(slots=True)
class SpriteSource:
    id: str
    content_key: str
    base_path: Path
    base_sha256: str
    emission_path: Path | None
    emission_sha256: str | None
    output_scale: float
    width: int
    height: int


@dataclass(slots=True)
class Placement:
    page: int
    x: int
    y: int


@dataclass(slots=True)
class PreparedSprite:
    source: SpriteSource
    base: Image.Image
    emission: Image.Image | None
    width: int
    height: int
    pivot_x: float
    pivot_y: float

    @property
    def id(self) -> str:
        return self.source.id

    @property
    def content_key(self) -> str:
        return self.source.content_key


@dataclass(frozen=True, slots=True)
class Rect:
    x: int
    y: int
    width: int
    height: int


@dataclass(slots=True)
class PackingPlan:
    placements: dict[str, Placement]
    page_sizes: list[tuple[int, int]]


def _checked_asset(root: Path, asset: dict[str, Any], label: str) -> Path:
    path = resolve_workspace_path(root, str(asset.get("path") or ""))
    if not path.is_file():
        raise WorkspaceFormatError(f"{label}缺失。")
    if path.stat().st_size != int(asset.get("bytes") or -1):
        raise WorkspaceFormatError(f"{label}大小校验失败。")
    digest = sha256_file(path)
    if digest != str(asset.get("sha256") or ""):
        raise WorkspaceFormatError(f"{label}哈希校验失败。")
    return path


def _design_size(character: dict[str, Any]) -> dict[str, Any]:
    calibration = character.get("calibration") or {}
    profiles = calibration.get("sizeProfiles") or []
    active_id = str(calibration.get("activeSizeProfileId") or "")
    profile = next((item for item in profiles if str(item.get("id") or "") == active_id), None)
    if not isinstance(profile, dict):
        raise WorkspaceFormatError("角色活动尺寸档位不存在。")
    unit_mode = str(profile.get("unitMode") or "")
    if unit_mode not in {"pixels", "unity"}:
        raise WorkspaceFormatError("角色尺寸档位单位无效。")
    ppu = float(calibration.get("pixelsPerUnit", CANONICAL_PIXELS_PER_UNIT))
    if not math.isfinite(ppu) or abs(ppu - CANONICAL_PIXELS_PER_UNIT) > 0.0001:
        raise WorkspaceFormatError("格式 3 固定使用 100 px/Unity unit。")
    source_width = float(profile.get("width", 0))
    source_height = float(profile.get("height", 0))
    if not all(math.isfinite(value) and value > 0 for value in (source_width, source_height)):
        raise WorkspaceFormatError("角色尺寸档位宽高无效。")
    raw_width_pixels = source_width if unit_mode == "pixels" else source_width * ppu
    raw_height_pixels = source_height if unit_mode == "pixels" else source_height * ppu
    width_pixels = round(raw_width_pixels)
    height_pixels = round(raw_height_pixels)
    if abs(raw_width_pixels - width_pixels) > 0.0001 or abs(raw_height_pixels - height_pixels) > 0.0001:
        raise WorkspaceFormatError("Unity 世界单位尺寸必须能按 100 PPU 精确换算为整数像素。")
    return {
        "profileId": active_id,
        "displayName": str(profile.get("name") or active_id),
        "sourceUnit": unit_mode,
        "sourceWidth": source_width,
        "sourceHeight": source_height,
        "widthPixels": int(width_pixels),
        "heightPixels": int(height_pixels),
        "widthWorld": width_pixels / ppu,
        "heightWorld": height_pixels / ppu,
        "pixelsPerUnit": ppu,
    }


def _collect_sources(
    root: Path,
    domain: dict[str, Any],
    character: dict[str, Any],
) -> tuple[list[SpriteSource], list[dict[str, Any]]]:
    variants = {str(item["id"]): item for item in domain["materialVariants"]}
    sources = {str(item["id"]): item for item in domain["materialSources"]}
    actions_by_id = {str(item["id"]): item for item in domain["actions"]}
    actions: list[dict[str, Any]] = []
    grouped: dict[str, dict[str, Any]] = {}
    action_settings = ((character.get("delivery") or {}).get("actionSettings") or {})

    for action_id in character.get("actionIds") or []:
        action = actions_by_id.get(str(action_id))
        if action is None:
            raise WorkspaceFormatError("角色引用了不存在的动作。")
        if not bool((action_settings.get(str(action_id)) or {}).get("includeInExport", True)):
            continue
        enabled_frame_refs = [
            frame_ref
            for frame_ref in action.get("frameRefs") or []
            if frame_ref.get("enabled", True) is not False
        ]
        if not enabled_frame_refs:
            continue
        export_action = deepcopy(action)
        export_action["frameRefs"] = enabled_frame_refs
        actions.append(export_action)
        for frame_ref in enabled_frame_refs:
            variant = variants.get(str(frame_ref.get("variantId") or ""))
            if variant is None:
                raise WorkspaceFormatError("动作引用了不存在的素材版本。")
            source = sources.get(str(variant.get("sourceId") or ""))
            if source is None or source.get("characterId") != character["id"]:
                raise WorkspaceFormatError("动作素材版本不属于当前角色。")
            frame = next(
                (
                    item
                    for item in variant.get("frames") or []
                    if item.get("id") == frame_ref.get("frameId")
                ),
                None,
            )
            if frame is None:
                raise WorkspaceFormatError("动作引用了不存在的处理帧。")
            base_path = _checked_asset(root, frame, "处理帧")
            emission = frame.get("emission")
            emission_path = (
                _checked_asset(root, emission, "特效层")
                if isinstance(emission, dict)
                else None
            )
            content_key = str(frame["sha256"])
            emission_sha = str(emission["sha256"]) if isinstance(emission, dict) else None
            if emission_sha:
                content_key += f":{emission_sha}"
            texture_scale = float((action_settings.get(str(action_id)) or {}).get("textureScale", 1.0))
            if not math.isfinite(texture_scale) or texture_scale <= 0 or texture_scale > 8:
                raise WorkspaceFormatError("动作纹理比例必须在 0 到 8 倍之间。")
            desired_scale = texture_scale
            record = grouped.get(content_key)
            if record is None:
                with Image.open(base_path) as opened:
                    rgba = opened.convert("RGBA")
                    width, height = rgba.size
                    rgba.close()
                if emission_path is not None:
                    with Image.open(emission_path) as opened:
                        if opened.size != (width, height):
                            raise WorkspaceFormatError("特效层尺寸与处理帧不一致。")
                grouped[content_key] = {
                    "base_path": base_path,
                    "base_sha256": str(frame["sha256"]),
                    "emission_path": emission_path,
                    "emission_sha256": emission_sha,
                    "scale": desired_scale,
                    "source_width": width,
                    "source_height": height,
                }
            else:
                record["scale"] = max(float(record["scale"]), desired_scale)

    if not actions:
        raise WorkspaceFormatError("角色至少需要勾选一个含帧动作参与导出。")

    sprites = [
        SpriteSource(
            id=_stable_id("spr", content_key),
            content_key=content_key,
            base_path=record["base_path"],
            base_sha256=record["base_sha256"],
            emission_path=record["emission_path"],
            emission_sha256=record["emission_sha256"],
            output_scale=float(record["scale"]),
            width=max(1, int(round(int(record["source_width"]) * float(record["scale"])))),
            height=max(1, int(round(int(record["source_height"]) * float(record["scale"])))),
        )
        for content_key, record in sorted(grouped.items())
    ]
    return sprites, actions


def _pack_shelves(
    sprites: list[SpriteSource], max_size: int, padding: int = ATLAS_PADDING
) -> tuple[dict[str, Placement], list[tuple[int, int]]]:
    placements: dict[str, Placement] = {}
    page_sizes: list[tuple[int, int]] = []
    page = 0
    x = y = padding
    row_height = 0
    used_width = used_height = 1
    for sprite in sprites:
        if sprite.width + padding * 2 > max_size or sprite.height + padding * 2 > max_size:
            raise WorkspaceFormatError(
                f"Sprite {sprite.id} 在最大输出缩放后超过图集尺寸。"
            )
        if x + sprite.width + padding > max_size:
            x = padding
            y += row_height + padding
            row_height = 0
        if y + sprite.height + padding > max_size:
            page_sizes.append((used_width, used_height))
            page += 1
            x = y = padding
            row_height = 0
            used_width = used_height = 1
        placements[sprite.id] = Placement(page=page, x=x, y=y)
        x += sprite.width + padding
        row_height = max(row_height, sprite.height)
        used_width = max(used_width, x)
        used_height = max(used_height, y + row_height + padding)
    page_sizes.append((used_width, used_height))
    return placements, page_sizes


def _scaled_rgba(path: Path, size: tuple[int, int]) -> Image.Image:
    with Image.open(path) as opened:
        image = opened.convert("RGBA")
    if image.size == size:
        return image
    resized = image.resize(size, Image.Resampling.LANCZOS)
    image.close()
    return resized


def _prepare_sprites(
    sprites: list[SpriteSource], frame_padding: int
) -> list[PreparedSprite]:
    prepared: list[PreparedSprite] = []
    try:
        for sprite in sprites:
            size = (sprite.width, sprite.height)
            base = _scaled_rgba(sprite.base_path, size)
            emission: Image.Image | None = None
            try:
                emission = (
                    _scaled_rgba(sprite.emission_path, size)
                    if sprite.emission_path is not None
                    else None
                )
                base_alpha = base.getchannel("A")
                emission_alpha = emission.getchannel("A") if emission else None
                try:
                    base_box = base_alpha.getbbox()
                    emission_box = emission_alpha.getbbox() if emission_alpha else None
                finally:
                    base_alpha.close()
                    if emission_alpha:
                        emission_alpha.close()
                boxes = [box for box in (base_box, emission_box) if box is not None]
                anchor_x = sprite.width / 2.0
                anchor_y = float(sprite.height)
                if boxes:
                    left = min(box[0] for box in boxes)
                    top = min(box[1] for box in boxes)
                    right = max(box[2] for box in boxes)
                    bottom = max(box[3] for box in boxes)
                else:
                    left = min(sprite.width - 1, max(0, int(math.floor(anchor_x))))
                    top = max(0, sprite.height - 1)
                    right = left + 1
                    bottom = top + 1
                left = max(0, min(left, int(math.floor(anchor_x))) - frame_padding)
                top = max(0, min(top, int(math.floor(anchor_y))) - frame_padding)
                right = min(
                    sprite.width,
                    max(right, int(math.ceil(anchor_x))) + frame_padding,
                )
                bottom = min(
                    sprite.height,
                    max(bottom, int(math.ceil(anchor_y))) + frame_padding,
                )
                if right <= left or bottom <= top:
                    raise WorkspaceFormatError("Sprite 紧裁范围无效。")
                cropped_base = base.crop((left, top, right, bottom))
                cropped_emission = (
                    emission.crop((left, top, right, bottom)) if emission else None
                )
            finally:
                base.close()
                if emission:
                    emission.close()
            width = right - left
            height = bottom - top
            prepared.append(
                PreparedSprite(
                    source=sprite,
                    base=cropped_base,
                    emission=cropped_emission,
                    width=width,
                    height=height,
                    pivot_x=(anchor_x - left) / width,
                    pivot_y=(bottom - anchor_y) / height,
                )
            )
    except Exception:
        _close_prepared(prepared)
        raise
    return prepared


def _close_prepared(sprites: list[PreparedSprite]) -> None:
    for sprite in sprites:
        sprite.base.close()
        if sprite.emission is not None:
            sprite.emission.close()


class _MaxRectsPage:
    def __init__(self, width: int, height: int) -> None:
        self.width = width
        self.height = height
        self.free = [Rect(0, 0, width, height)]
        self.used: list[Rect] = []

    def find(self, width: int, height: int) -> tuple[tuple[int, int, int, int], Rect] | None:
        matches: list[tuple[tuple[int, int, int, int], Rect]] = []
        for free in self.free:
            if width <= free.width and height <= free.height:
                leftover_x = free.width - width
                leftover_y = free.height - height
                rect = Rect(free.x, free.y, width, height)
                matches.append(
                    ((min(leftover_x, leftover_y), max(leftover_x, leftover_y), free.y, free.x), rect)
                )
        return min(matches, key=lambda item: item[0]) if matches else None

    def place(self, placed: Rect) -> None:
        next_free: list[Rect] = []
        for free in self.free:
            if not _rects_intersect(free, placed):
                next_free.append(free)
                continue
            if placed.x > free.x:
                next_free.append(Rect(free.x, free.y, placed.x - free.x, free.height))
            if placed.x + placed.width < free.x + free.width:
                next_free.append(
                    Rect(
                        placed.x + placed.width,
                        free.y,
                        free.x + free.width - placed.x - placed.width,
                        free.height,
                    )
                )
            if placed.y > free.y:
                next_free.append(Rect(free.x, free.y, free.width, placed.y - free.y))
            if placed.y + placed.height < free.y + free.height:
                next_free.append(
                    Rect(
                        free.x,
                        placed.y + placed.height,
                        free.width,
                        free.y + free.height - placed.y - placed.height,
                    )
                )
        self.free = _prune_free_rects(next_free)
        self.used.append(placed)


def _rects_intersect(a: Rect, b: Rect) -> bool:
    return not (
        b.x >= a.x + a.width
        or b.x + b.width <= a.x
        or b.y >= a.y + a.height
        or b.y + b.height <= a.y
    )


def _contains(outer: Rect, inner: Rect) -> bool:
    return (
        inner.x >= outer.x
        and inner.y >= outer.y
        and inner.x + inner.width <= outer.x + outer.width
        and inner.y + inner.height <= outer.y + outer.height
    )


def _prune_free_rects(rects: list[Rect]) -> list[Rect]:
    valid = [rect for rect in rects if rect.width > 0 and rect.height > 0]
    return [
        rect
        for index, rect in enumerate(valid)
        if not any(
            index != other_index and _contains(other, rect)
            for other_index, other in enumerate(valid)
        )
    ]


def _candidate_bounds(
    sprites: list[PreparedSprite], max_size: int, padding: int, extrude: int
) -> list[tuple[int, int]]:
    slot_widths = [sprite.width + extrude * 2 + padding for sprite in sprites]
    slot_heights = [sprite.height + extrude * 2 + padding for sprite in sprites]
    largest_width = max(width - padding for width in slot_widths)
    largest_height = max(height - padding for height in slot_heights)
    total_area = sum(width * height for width, height in zip(slot_widths, slot_heights))
    square = min(max_size, max(1, int(math.ceil(math.sqrt(total_area)))))
    values = {
        largest_width,
        largest_height,
        square,
        max_size,
        *(
            value
            for value in (64, 128, 256, 512, 1024, 2048, 4096, 8192)
            if value <= max_size
        ),
    }
    widths = sorted(value for value in values if largest_width <= value <= max_size)
    heights = sorted(value for value in values if largest_height <= value <= max_size)
    actual_bounds = {(width, height) for width in widths for height in heights}
    return [
        (width + padding, height + padding)
        for width, height in sorted(
            actual_bounds,
            key=lambda size: (size[0] * size[1], abs(size[0] - size[1]), size[0], size[1]),
        )
    ]


def _pack_maxrects_in_bounds(
    sprites: list[PreparedSprite],
    width: int,
    height: int,
    padding: int,
    extrude: int,
) -> PackingPlan:
    ordered = sorted(
        sprites,
        key=lambda sprite: (
            -(sprite.width + extrude * 2 + padding) * (sprite.height + extrude * 2 + padding),
            -max(sprite.width, sprite.height),
            -sprite.height,
            -sprite.width,
            sprite.id,
        ),
    )
    pages: list[_MaxRectsPage] = []
    placements: dict[str, Placement] = {}
    for sprite in ordered:
        slot_width = sprite.width + extrude * 2 + padding
        slot_height = sprite.height + extrude * 2 + padding
        matches: list[tuple[tuple[int, int, int, int, int], int, Rect]] = []
        for page_index, page in enumerate(pages):
            found = page.find(slot_width, slot_height)
            if found is not None:
                score, rect = found
                matches.append(((*score, page_index), page_index, rect))
        if matches:
            _, page_index, slot = min(matches, key=lambda item: item[0])
        else:
            if slot_width > width or slot_height > height:
                raise WorkspaceFormatError(
                    f"Sprite {sprite.id} 在最大输出缩放后超过图集尺寸。"
                )
            page_index = len(pages)
            pages.append(_MaxRectsPage(width, height))
            found = pages[page_index].find(slot_width, slot_height)
            assert found is not None
            _, slot = found
        pages[page_index].place(slot)
        placements[sprite.id] = Placement(
            page=page_index,
            x=slot.x + extrude,
            y=slot.y + extrude,
        )
    page_sizes = [
        (
            max(1, max(rect.x + rect.width for rect in page.used) - padding),
            max(1, max(rect.y + rect.height for rect in page.used) - padding),
        )
        for page in pages
    ]
    return PackingPlan(placements=placements, page_sizes=page_sizes)


def _packing_signature(plan: PackingPlan, sprites: list[PreparedSprite]) -> tuple[tuple[int, int, int], ...]:
    return tuple(
        (plan.placements[sprite.id].page, plan.placements[sprite.id].y, plan.placements[sprite.id].x)
        for sprite in sorted(sprites, key=lambda item: item.id)
    )


def _pack_compact(
    sources: list[SpriteSource],
    sprites: list[PreparedSprite],
    max_size: int,
    padding: int,
    extrude: int,
) -> PackingPlan:
    for sprite in sprites:
        if (
            sprite.width + extrude * 2 > max_size
            or sprite.height + extrude * 2 > max_size
        ):
            raise WorkspaceFormatError(
                f"Sprite {sprite.id} 在最大输出缩放后超过图集尺寸。"
            )
    stable_sources = sorted(sources, key=lambda sprite: (sprite.content_key, sprite.id))
    _, baseline_pages = _pack_shelves(stable_sources, max_size, padding)
    page_cap = len(baseline_pages)
    area_cap = sum(width * height for width, height in baseline_pages)
    candidates: list[PackingPlan] = []
    for width, height in _candidate_bounds(sprites, max_size, padding, extrude):
        plan = _pack_maxrects_in_bounds(
            sprites, width, height, padding, extrude
        )
        if (
            len(plan.page_sizes) <= page_cap
            and sum(page_width * page_height for page_width, page_height in plan.page_sizes)
            <= area_cap
        ):
            candidates.append(plan)
    if not candidates:
        raise WorkspaceFormatError("图集边距生效后无法在不增加页数或页面面积的前提下完成排版。")

    def score(plan: PackingPlan) -> tuple[Any, ...]:
        area = sum(width * height for width, height in plan.page_sizes)
        worst_aspect = max(
            max(width / height, height / width) for width, height in plan.page_sizes
        )
        largest_dimension = max(max(size) for size in plan.page_sizes)
        return (
            area,
            len(plan.page_sizes),
            worst_aspect,
            largest_dimension,
            tuple(plan.page_sizes),
            _packing_signature(plan, sprites),
        )

    return min(candidates, key=score)


def _paste_extruded(
    atlas: Image.Image, sprite: Image.Image, x: int, y: int, extrude: int
) -> None:
    atlas.paste(sprite, (x, y))
    if extrude <= 0:
        return
    width, height = sprite.size
    left = sprite.crop((0, 0, 1, height)).resize((extrude, height))
    right = sprite.crop((width - 1, 0, width, height)).resize((extrude, height))
    top = sprite.crop((0, 0, width, 1)).resize((width, extrude))
    bottom = sprite.crop((0, height - 1, width, height)).resize((width, extrude))
    corners = [
        (sprite.crop((0, 0, 1, 1)), (x - extrude, y - extrude)),
        (sprite.crop((width - 1, 0, width, 1)), (x + width, y - extrude)),
        (sprite.crop((0, height - 1, 1, height)), (x - extrude, y + height)),
        (sprite.crop((width - 1, height - 1, width, height)), (x + width, y + height)),
    ]
    try:
        atlas.paste(left, (x - extrude, y))
        atlas.paste(right, (x + width, y))
        atlas.paste(top, (x, y - extrude))
        atlas.paste(bottom, (x, y + height))
        for corner, position in corners:
            expanded = corner.resize((extrude, extrude))
            try:
                atlas.paste(expanded, position)
            finally:
                expanded.close()
    finally:
        left.close()
        right.close()
        top.close()
        bottom.close()
        for corner, _ in corners:
            corner.close()


def _png_bytes(image: Image.Image) -> bytes:
    output = BytesIO()
    image.save(output, format="PNG", compress_level=9, optimize=True)
    return output.getvalue()


def _baseline_png_byte_count(
    sprites: list[SpriteSource], max_size: int, padding: int
) -> int:
    sprites = sorted(sprites, key=lambda sprite: (sprite.content_key, sprite.id))
    placements, page_sizes = _pack_shelves(sprites, max_size, padding)
    has_emission = any(sprite.emission_path is not None for sprite in sprites)
    total = 0
    for page_index, size in enumerate(page_sizes):
        base = Image.new("RGBA", size, (0, 0, 0, 0))
        emission = Image.new("RGB", size, (0, 0, 0)) if has_emission else None
        try:
            for sprite in sprites:
                placement = placements[sprite.id]
                if placement.page != page_index:
                    continue
                rendered = _scaled_rgba(sprite.base_path, (sprite.width, sprite.height))
                try:
                    base.alpha_composite(rendered, (placement.x, placement.y))
                finally:
                    rendered.close()
                if emission is not None and sprite.emission_path is not None:
                    rendered_emission = _scaled_rgba(
                        sprite.emission_path, (sprite.width, sprite.height)
                    )
                    rgb = rendered_emission.convert("RGB")
                    try:
                        emission.paste(rgb, (placement.x, placement.y))
                    finally:
                        rgb.close()
                        rendered_emission.close()
            output = BytesIO()
            base.save(output, format="PNG", compress_level=6, optimize=False)
            total += len(output.getvalue())
            if emission is not None:
                output = BytesIO()
                emission.save(output, format="PNG", compress_level=6, optimize=False)
                total += len(output.getvalue())
        finally:
            base.close()
            if emission is not None:
                emission.close()
    return total


def _encode_pages(
    sprites: list[PreparedSprite],
    placements: dict[str, Placement],
    page_sizes: list[tuple[int, int]],
    extrude: int,
) -> tuple[list[bytes], list[bytes]]:
    has_emission = any(sprite.emission is not None for sprite in sprites)
    base_pages: list[bytes] = []
    emission_pages: list[bytes] = []
    for page_index, size in enumerate(page_sizes):
        base = Image.new("RGBA", size, (0, 0, 0, 0))
        emission = Image.new("RGB", size, (0, 0, 0)) if has_emission else None
        try:
            for sprite in sprites:
                placement = placements[sprite.id]
                if placement.page != page_index:
                    continue
                _paste_extruded(base, sprite.base, placement.x, placement.y, extrude)
                if emission is not None and sprite.emission is not None:
                    rgb = sprite.emission.convert("RGB")
                    try:
                        _paste_extruded(emission, rgb, placement.x, placement.y, extrude)
                    finally:
                        rgb.close()
            base_pages.append(_png_bytes(base))
            if emission is not None:
                emission_pages.append(_png_bytes(emission))
        finally:
            base.close()
            if emission is not None:
                emission.close()
    return base_pages, emission_pages


def _render_pages(
    stage: Path,
    sprites: list[PreparedSprite],
    placements: dict[str, Placement],
    page_sizes: list[tuple[int, int]],
    base_page_bytes: list[bytes],
    emission_page_bytes: list[bytes],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    base_records: list[dict[str, Any]] = []
    emission_records: list[dict[str, Any]] = []
    for page_index, size in enumerate(page_sizes):
        atlas_id = _stable_id("atl", page_index, *[s.id for s in sprites if placements[s.id].page == page_index])
        base_name = f"atlases/base/{page_index:02d}.png"
        base_path = stage / base_name
        base_path.parent.mkdir(parents=True, exist_ok=True)
        base_path.write_bytes(base_page_bytes[page_index])
        base_records.append(
            {
                "id": atlas_id,
                "file": base_name,
                "width": size[0],
                "height": size[1],
                "sha256": sha256_file(base_path),
            }
        )
        if emission_page_bytes:
            emission_name = f"atlases/emission/{page_index:02d}.png"
            emission_path = stage / emission_name
            emission_path.parent.mkdir(parents=True, exist_ok=True)
            emission_path.write_bytes(emission_page_bytes[page_index])
            emission_records.append(
                {
                    "id": atlas_id,
                    "file": emission_name,
                    "width": size[0],
                    "height": size[1],
                    "sha256": sha256_file(emission_path),
                }
            )
    return base_records, emission_records


def _write_package(stage: Path, manifest: dict[str, Any]) -> Path:
    manifest_path = stage / "manifest.json"
    manifest_path.write_bytes(_json_bytes(manifest))
    files = sorted(
        path
        for path in stage.rglob("*")
        if path.is_file() and path.name not in {PACKAGE_NAME, "checksums.json"}
    )
    checksums = {
        "algorithm": "SHA-256",
        "files": [
            {
                "path": path.relative_to(stage).as_posix(),
                "sha256": sha256_file(path),
            }
            for path in files
        ],
    }
    checksums_path = stage / "checksums.json"
    checksums_path.write_bytes(_json_bytes(checksums))
    archive_path = stage / PACKAGE_NAME
    with zipfile.ZipFile(archive_path, "w", allowZip64=True) as archive:
        for path in [*files, checksums_path]:
            name = path.relative_to(stage).as_posix()
            archive.writestr(_zip_info(name), path.read_bytes())
    validate_unity_delivery_archive(archive_path)
    return archive_path


def _remove_tree(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)


def export_domain_character(
    repository: Any,
    character_id: str,
    *,
    expected_revision_id: str,
    atlas_max_size: int = 4096,
) -> dict[str, Any]:
    if atlas_max_size < 64 or atlas_max_size > 8192:
        raise WorkspaceFormatError("图集最大尺寸必须在 64 到 8192 之间。")
    root = Path(repository.root).resolve()
    domain = repository.workspace_domain()
    if str(domain.get("revisionId") or "") != expected_revision_id:
        raise WorkspaceFormatError("格式 3 领域状态已变化，请刷新后重试。")
    character = next(
        (item for item in domain["characters"] if item["id"] == character_id),
        None,
    )
    if character is None:
        raise WorkspaceFormatError("角色不存在。")
    design_size = _design_size(character)
    sprites, actions = _collect_sources(root, domain, character)
    delivery = character.get("delivery") or {}
    atlas_settings = delivery.get("atlas") or {}
    padding = int(atlas_settings.get("padding", ATLAS_PADDING))
    extrude = int(atlas_settings.get("extrude", 0))
    frame_padding = int(atlas_settings.get("framePadding", 0))
    prepared = _prepare_sprites(sprites, frame_padding)
    try:
        plan = _pack_compact(sprites, prepared, atlas_max_size, padding, extrude)
        base_page_bytes, emission_page_bytes = _encode_pages(
            prepared,
            plan.placements,
            plan.page_sizes,
            extrude,
        )
        if sum(map(len, base_page_bytes)) + sum(map(len, emission_page_bytes)) > _baseline_png_byte_count(
            sprites, atlas_max_size, padding
        ):
            raise WorkspaceFormatError("紧凑排版的 PNG 总字节超过当前逐行排版，已停止导出。")
    except Exception:
        _close_prepared(prepared)
        raise
    placements = plan.placements
    page_sizes = plan.page_sizes
    export_root = root / "exports" / "domain" / character_id
    export_root.mkdir(parents=True, exist_ok=True)
    stage = export_root / f".stage-{uuid.uuid4().hex}"
    stage.mkdir()
    old_asset = (character.get("exportState") or {}).get("currentAtlas")
    final_root: Path | None = None
    created_final = False
    try:
        estimated = (
            sum(width * height * 8 for width, height in page_sizes)
            + sum(map(len, base_page_bytes))
            + sum(map(len, emission_page_bytes))
            + 1_048_576
        )
        if shutil.disk_usage(export_root).free < estimated:
            raise OSError(errno.ENOSPC, "insufficient workspace space")
        base_atlases, emission_atlases = _render_pages(
            stage,
            prepared,
            placements,
            page_sizes,
            base_page_bytes,
            emission_page_bytes,
        )
        atlas_ids = {index: record["id"] for index, record in enumerate(base_atlases)}
        sprite_by_key = {sprite.content_key: sprite for sprite in prepared}
        variants = {str(item["id"]): item for item in domain["materialVariants"]}
        animation_records: list[dict[str, Any]] = []
        global_shadow = character.get("shadow") or {"enabled": False, "color": "#000000", "baseOpacity": 0.0, "lightAngleDegrees": 90.0}
        action_settings = delivery.get("actionSettings") or {}
        included_action_ids = {str(action["id"]) for action in actions}
        default_action_id = str(delivery.get("defaultActionId") or "")
        if default_action_id not in included_action_ids:
            raise WorkspaceFormatError("默认动作必须属于参与导出的动作。")
        for action in actions:
            frame_records: list[dict[str, Any]] = []
            resolved_shadows = resolve_domain_action_shadows(
                root,
                domain,
                character,
                action["frameRefs"],
                loop=bool((action_settings.get(str(action["id"])) or {}).get("runtimeLoop", action.get("previewLoop", action.get("loop", True)))),
            )
            for index, frame_ref in enumerate(action["frameRefs"]):
                variant = variants[str(frame_ref["variantId"])]
                frame = next(
                    item
                    for item in variant["frames"]
                    if item["id"] == frame_ref["frameId"]
                )
                emission = frame.get("emission")
                content_key = str(frame["sha256"])
                if isinstance(emission, dict):
                    content_key += f":{emission['sha256']}"
                sprite = sprite_by_key[content_key]
                resolved_shadow = resolved_shadows[index]
                frame_records.append(
                    {
                        "id": str(frame_ref["id"]),
                        "index": index,
                        "spriteId": sprite.id,
                        "durationSeconds": float(frame_ref["durationSeconds"]),
                        "shadow": {
                            **resolved_shadow,
                            "positionPx": {
                                "x": float(resolved_shadow["positionPx"][0]),
                                "y": float(resolved_shadow["positionPx"][1]),
                            },
                        },
                        "transform": {
                            **frame_ref["transform"],
                            "shadow": {
                                **frame_ref["transform"]["shadow"],
                                "enabled": frame_ref["transform"]["shadow"].get("enabled") if frame_ref["transform"]["shadow"].get("enabled") is not None else bool(global_shadow.get("enabled", False)),
                                "color": frame_ref["transform"]["shadow"].get("color") or str(global_shadow.get("color") or "#000000"),
                                "opacity": frame_ref["transform"]["shadow"].get("opacity") if frame_ref["transform"]["shadow"].get("opacity") is not None else float(global_shadow.get("baseOpacity", 0.0)),
                            },
                        },
                    }
                )
            animation_records.append(
                {
                    "id": action["id"],
                    "displayName": action["name"],
                    "unityScale": 1.0,
                    "outputScale": float((action_settings.get(str(action["id"])) or {}).get("textureScale", 1.0)),
                    "pixelsPerUnit": design_size["pixelsPerUnit"],
                    "loop": bool((action_settings.get(str(action["id"])) or {}).get("runtimeLoop", action.get("previewLoop", action.get("loop", True)))),
                    "durationSeconds": sum(item["durationSeconds"] for item in frame_records),
                    "frameRate": len(frame_records) / sum(
                        item["durationSeconds"] for item in frame_records
                    ),
                    "frames": frame_records,
                }
            )
        sprite_records = []
        for sprite in prepared:
            placement = placements[sprite.id]
            page_height = page_sizes[placement.page][1]
            sprite_records.append(
                {
                    "id": sprite.id,
                    "atlasId": atlas_ids[placement.page],
                    "rect": {
                        "x": placement.x,
                        "y": page_height - placement.y - sprite.height,
                        "width": sprite.width,
                        "height": sprite.height,
                    },
                    "pivot": {"x": sprite.pivot_x, "y": sprite.pivot_y},
                    "outputScale": sprite.source.output_scale,
                    "sourceSha256": sprite.source.base_sha256,
                    **(
                        {"emissionSha256": sprite.source.emission_sha256}
                        if sprite.source.emission_sha256
                        else {}
                    ),
                }
            )
        manifest = {
            "formatVersion": CHARACTER_PACKAGE_FORMAT,
            "packageShape": CHARACTER_PACKAGE_SHAPE,
            "coordinateContract": COORDINATE_CONTRACT,
            "generator": {"name": "RotoWeave", "version": __version__},
            "character": {
                "id": character["id"],
                "name": character["name"],
                "revision": 1,
                "sourceRevision": 1,
                "defaultAnimationId": default_action_id,
                "pixelsPerUnit": design_size["pixelsPerUnit"],
                "canonicalPixelsPerUnit": CANONICAL_PIXELS_PER_UNIT,
                "basePixelsPerUnit": CANONICAL_PIXELS_PER_UNIT,
                "outputScale": 1.0,
                "designSize": design_size,
                "shadow": {
                    "enabled": bool(global_shadow.get("enabled", False)),
                    "color": {"r": int(str(global_shadow.get("color", "#000000"))[1:3], 16) / 255, "g": int(str(global_shadow.get("color", "#000000"))[3:5], 16) / 255, "b": int(str(global_shadow.get("color", "#000000"))[5:7], 16) / 255},
                    "baseOpacity": float(global_shadow.get("baseOpacity", 0.0)),
                    "lightAngleDegrees": float(global_shadow.get("lightAngleDegrees", 90.0)),
                    "rotationDegrees": 0.0,
                },
            },
            "renderContract": {
                "pipeline": "Built-in",
                "target": "WebGL2",
                "colorSpace": "Linear",
                "base": {
                    "alphaMode": "straight",
                    "blend": {"source": "SrcAlpha", "destination": "OneMinusSrcAlpha"},
                },
                "emission": {
                    "colorSpace": "Linear",
                    "blend": {"source": "One", "destination": "One"},
                },
            },
            "textureDefaults": {
                "base": {
                    "format": "RGBA32",
                    "sRGB": True,
                    "wrapMode": "Clamp",
                    "filterMode": "Bilinear",
                    "mipmaps": False,
                    "compression": "None",
                },
                "emission": {
                    "format": "RGB24",
                    "sRGB": False,
                    "wrapMode": "Clamp",
                    "filterMode": "Bilinear",
                    "mipmaps": False,
                    "compression": "None",
                },
            },
            "atlases": {
                "base": base_atlases,
                **({"emission": emission_atlases} if emission_atlases else {}),
            },
            "sprites": sprite_records,
            "animations": animation_records,
            "deduplication": {
                "identity": "base-and-emission-sha256",
                "referencedFrames": sum(len(item["frames"]) for item in animation_records),
                "uniqueSprites": len(sprite_records),
                "resolutionPolicy": "maximum-texture-scale",
            },
        }
        archive_path = _write_package(stage, manifest)
        package_hash = sha256_file(archive_path)
        final_root = export_root / package_hash
        if final_root.exists():
            existing = final_root / PACKAGE_NAME
            if not existing.is_file() or sha256_file(existing) != package_hash:
                raise WorkspaceFormatError("当前图集 generation 内容冲突。")
            _remove_tree(stage)
        else:
            for child in list(stage.iterdir()):
                if child.name != PACKAGE_NAME:
                    _remove_tree(child) if child.is_dir() else child.unlink()
            stage.replace(final_root)
            created_final = True
        final_archive = final_root / PACKAGE_NAME
        logical_archive = logical_workspace_path(root, final_archive)
        export_state = repository.set_domain_export_state(
            character_id,
            "current",
            current_atlas_path=logical_archive,
            expected_revision_id=expected_revision_id,
        )
        if isinstance(old_asset, dict):
            old_path = resolve_workspace_path(root, str(old_asset.get("path") or ""))
            old_generation = old_path.parent
            if old_generation != final_root and export_root in old_generation.parents:
                _remove_tree(old_generation)
        return {
            "characterId": character_id,
            "archivePath": logical_archive,
            "sha256": package_hash,
            "bytes": final_archive.stat().st_size,
            "manifest": manifest,
            "exportState": export_state,
        }
    except OSError as exc:
        _remove_tree(stage)
        if created_final and final_root is not None:
            _remove_tree(final_root)
        if exc.errno == errno.ENOSPC:
            raise WorkspaceFormatError("工作区空间不足，旧图集保持不变。") from exc
        raise
    except Exception:
        _remove_tree(stage)
        if created_final and final_root is not None:
            _remove_tree(final_root)
        raise
    finally:
        _close_prepared(prepared)


def estimate_domain_character(repository: Any, character_id: str, *, expected_revision_id: str, atlas_max_size: int | None = None) -> dict[str, Any]:
    domain = repository.workspace_domain()
    if str(domain.get("revisionId")) != expected_revision_id:
        raise WorkspaceFormatError("格式 3 领域状态已变化，请刷新后重试。")
    character = next((item for item in domain["characters"] if item["id"] == character_id), None)
    if character is None:
        raise WorkspaceFormatError("角色不存在。")
    atlas = ((character.get("delivery") or {}).get("atlas") or {})
    max_size = int(atlas_max_size or atlas.get("maxSize", 4096)); padding = int(atlas.get("padding", ATLAS_PADDING))
    extrude = int(atlas.get("extrude", 0)); frame_padding = int(atlas.get("framePadding", 0))
    sprites, actions = _collect_sources(Path(repository.root).resolve(), domain, character)
    prepared = _prepare_sprites(sprites, frame_padding)
    try:
        plan = _pack_compact(sprites, prepared, max_size, padding, extrude)
        base_page_bytes, emission_page_bytes = _encode_pages(
            prepared, plan.placements, plan.page_sizes, extrude
        )
        actual_png_bytes = sum(map(len, base_page_bytes)) + sum(map(len, emission_page_bytes))
        if actual_png_bytes > _baseline_png_byte_count(sprites, max_size, padding):
            raise WorkspaceFormatError("紧凑排版的 PNG 总字节超过当前逐行排版，已停止预估。")
        pages = plan.page_sizes
        rgba_bytes = sum(width * height * 4 for width, height in pages)
        used = sum(sprite.width * sprite.height for sprite in prepared)
        area = max(1, sum(width * height for width, height in pages))
        return {"referencedFrames": sum(len(action["frameRefs"]) for action in actions), "uniqueSprites": len(sprites), "maximumOutput": {"width": max(sprite.width for sprite in sprites), "height": max(sprite.height for sprite in sprites)}, "pageCount": len(pages), "pages": [{"index": index, "width": value[0], "height": value[1]} for index, value in enumerate(pages)], "rgbaBytes": rgba_bytes, "estimatedPngBytes": actual_png_bytes, "packingRatio": used / area}
    finally:
        _close_prepared(prepared)


def repair_domain_atlas_page(repository: Any, character_id: str, page_index: int, replacement_path: Path, *, expected_revision_id: str) -> dict[str, Any]:
    root = Path(repository.root).resolve(); domain = repository.workspace_domain()
    if str(domain.get("revisionId")) != expected_revision_id:
        raise WorkspaceFormatError("格式 3 领域状态已变化，请刷新后重试。")
    character = next((item for item in domain["characters"] if item["id"] == character_id), None)
    asset = ((character or {}).get("exportState") or {}).get("currentAtlas")
    if not isinstance(asset, dict): raise WorkspaceFormatError("角色尚未导出。")
    current_archive = _checked_asset(root, asset, "当前角色包")
    atlas_name = f"atlases/base/{page_index:02d}.png"; replacement = Path(replacement_path).resolve(strict=True)
    with zipfile.ZipFile(current_archive, "r") as source:
        try: original = source.read(atlas_name)
        except KeyError as exc: raise WorkspaceFormatError("图集页面不存在。") from exc
        entries = {name: source.read(name) for name in source.namelist() if name != "checksums.json"}
    with Image.open(replacement) as proposed, Image.open(__import__("io").BytesIO(original)) as existing:
        if proposed.format != "PNG" or "A" not in proposed.mode: raise WorkspaceFormatError("修复图必须是带透明通道的 PNG。")
        if proposed.size != existing.size: raise WorkspaceFormatError("修复图尺寸必须与原图集页面完全一致。")
    entries[atlas_name] = replacement.read_bytes()
    checksums = {"algorithm": "SHA-256", "files": [{"path": name, "sha256": hashlib.sha256(data).hexdigest()} for name, data in sorted(entries.items())]}
    export_root = root / "exports" / "domain" / character_id; stage = export_root / f".repair-{uuid.uuid4().hex}"; stage.mkdir(parents=True)
    staged_archive = stage / PACKAGE_NAME
    try:
        with zipfile.ZipFile(staged_archive, "w", allowZip64=True) as output:
            for name, data in sorted(entries.items()): output.writestr(_zip_info(name), data)
            output.writestr(_zip_info("checksums.json"), _json_bytes(checksums))
        validate_unity_delivery_archive(staged_archive); digest = sha256_file(staged_archive); final_root = export_root / digest
        if final_root.exists(): _remove_tree(stage)
        else: stage.replace(final_root)
        final_archive = final_root / PACKAGE_NAME
        state = repository.set_domain_export_state(character_id, "current", current_atlas_path=logical_workspace_path(root, final_archive), expected_revision_id=expected_revision_id)
        old_generation = current_archive.parent
        if old_generation != final_root and export_root in old_generation.parents: _remove_tree(old_generation)
        return {"characterId": character_id, "pageIndex": page_index, "sha256": digest, "bytes": final_archive.stat().st_size, "exportState": state}
    except Exception:
        _remove_tree(stage); raise
