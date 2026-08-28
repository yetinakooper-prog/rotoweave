from __future__ import annotations

import hashlib
import io
import json
import zipfile
from pathlib import Path

from PIL import Image, ImageEnhance


WORKSPACE = Path(__file__).resolve().parent.parent
SOURCE_ICON = WORKSPACE / "public" / "og-rotoweave.png"
PRODUCT = json.loads((WORKSPACE.parent / "RotoWeaveContracts" / "product.json").read_text(encoding="utf-8"))
PRODUCT_VERSION = str(PRODUCT["version"])
CONTRACTS = PRODUCT["contracts"]
SAMPLE_PRODUCT_LINE = ".".join(PRODUCT_VERSION.split(".")[:2])
OUTPUT = WORKSPACE / "release" / f"SampleHero-{SAMPLE_PRODUCT_LINE}.rotoweave"


def stable_id(prefix: str, value: str) -> str:
    return f"{prefix}_{hashlib.sha256(value.encode('utf-8')).hexdigest()[:24]}"


def json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def png_bytes(image: Image.Image) -> bytes:
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=False)
    return output.getvalue()


def make_frame(icon: Image.Image, scale: float, angle: float, brightness: float) -> Image.Image:
    size = max(24, round(48 * scale))
    frame = icon.resize((size, size), Image.Resampling.LANCZOS)
    frame = ImageEnhance.Brightness(frame).enhance(brightness)
    if angle:
        frame = frame.rotate(angle, Image.Resampling.BICUBIC, expand=True)
    canvas = Image.new("RGBA", (48, 48), (0, 0, 0, 0))
    frame.thumbnail((48, 48), Image.Resampling.LANCZOS)
    canvas.alpha_composite(frame, ((48 - frame.width) // 2, 48 - frame.height))
    return canvas


def build() -> Path:
    if not SOURCE_ICON.is_file():
        raise SystemExit(f"Missing source icon: {SOURCE_ICON}")
    icon = Image.open(SOURCE_ICON).convert("RGBA")
    character_id = stable_id("chr", "RotoWeave Sample Hero v1")
    definitions = [
        ("呼吸 · 循环", True, [(0.90, 0, 0.82), (0.96, 0, 0.94), (1.0, 0, 1.08), (0.96, 0, 0.94)]),
        ("斩击 / 重击", False, [(0.90, -8, 0.90), (0.96, -18, 1.0), (1.0, 20, 1.18), (0.92, 8, 0.88)]),
        ("落幕（单次）", False, [(1.0, 0, 1.0), (0.88, 12, 0.78), (0.72, 24, 0.55), (0.56, 36, 0.35)]),
    ]
    entries: dict[str, bytes] = {}
    atlases: list[dict[str, object]] = []
    sprites: list[dict[str, object]] = []
    animations: list[dict[str, object]] = []
    atlas_id = stable_id("atl", f"{character_id}:shared:0")
    atlas_width, atlas_height = 256, 192
    atlas = Image.new("RGBA", (atlas_width, atlas_height), (0, 0, 0, 0))

    for animation_index, (display_name, loop, transforms) in enumerate(definitions):
        animation_id = stable_id("ani", f"{character_id}:{display_name}")
        frames: list[dict[str, object]] = []
        for frame_index, (scale, angle, brightness) in enumerate(transforms):
            left = 8 + frame_index * 64
            top = 8 + animation_index * 64
            atlas.alpha_composite(make_frame(icon, scale, angle, brightness), (left, top))
            bottom = atlas_height - (top + 48)
            sprite_id = stable_id("spr", f"{animation_id}:{frame_index}")
            sprites.append(
                {
                    "id": sprite_id,
                    "atlasId": atlas_id,
                    "rect": {"x": left, "y": bottom, "width": 48, "height": 48},
                    "pivot": {"x": 0.5, "y": 0.08},
                    "outputScale": 1.0,
                    "sourceSha256": hashlib.sha256(
                        f"{animation_id}:{frame_index}".encode("utf-8")
                    ).hexdigest(),
                }
            )
            frames.append(
                {
                    "id": stable_id("frm", f"{animation_id}:{frame_index}"),
                    "spriteId": sprite_id,
                    "index": frame_index,
                    "durationSeconds": 0.1,
                    "transform": {
                        "position": {
                            "x": round((frame_index - 1.5) * 0.4, 3),
                            "y": 0.0,
                        },
                        "scale": {"x": 1.0, "y": 1.0},
                        "rotationDegrees": 0.0,
                        "color": "#ffffff",
                        "opacity": 1.0,
                        "shadow": {
                            "enabled": True,
                            "color": "#000000",
                            "opacity": round(0.32 * max(0.55, brightness), 3),
                            "offset": {
                                "x": round(-2.0 + (frame_index - 1.5) * 0.20, 3),
                                "y": -1.5,
                            },
                            "scale": {
                                "x": round(0.94 + scale * 0.06, 3),
                                "y": round(1.04 - scale * 0.04, 3),
                            },
                        },
                    },
                }
            )
        animations.append(
            {
                "id": animation_id,
                "displayName": display_name,
                "unityScale": 1.0,
                "outputScale": 1.0,
                "pixelsPerUnit": float(CONTRACTS["canonicalPixelsPerUnit"]),
                "loop": loop,
                "frameRate": 10.0,
                "durationSeconds": len(frames) / 10.0,
                "frames": frames,
                "quality": {},
            }
        )

    atlas_path = "atlases/base/00.png"
    atlas_payload = png_bytes(atlas)
    entries[atlas_path] = atlas_payload
    atlases.append(
        {
            "id": atlas_id,
            "file": atlas_path,
            "width": atlas_width,
            "height": atlas_height,
            "sha256": hashlib.sha256(atlas_payload).hexdigest(),
        }
    )
    manifest = {
        "formatVersion": int(CONTRACTS["characterPackageFormat"]),
        "packageShape": str(CONTRACTS["characterPackageShape"]),
        "coordinateContract": str(CONTRACTS["coordinateContract"]),
        "generator": {"name": "RotoWeave", "version": PRODUCT_VERSION},
        "character": {
            "id": character_id,
            "name": "RotoWeave 示例角色",
            "revision": 1,
            "sourceRevision": 1,
            "defaultAnimationId": animations[0]["id"],
            "pixelsPerUnit": float(CONTRACTS["canonicalPixelsPerUnit"]),
            "canonicalPixelsPerUnit": float(CONTRACTS["canonicalPixelsPerUnit"]),
            "basePixelsPerUnit": float(CONTRACTS["canonicalPixelsPerUnit"]),
            "outputScale": 1.0,
            "shadow": {
                "enabled": True,
                "color": {"r": 0.0, "g": 0.0, "b": 0.0},
                "baseOpacity": 0.35,
                "lightAngleDegrees": 135.0,
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
        "atlases": {"base": atlases},
        "sprites": sprites,
        "animations": animations,
        "qualityWarnings": [],
        "warningsAcknowledged": True,
    }
    entries["manifest.json"] = json_bytes(manifest)
    checksums = [
        {"path": path, "sha256": hashlib.sha256(payload).hexdigest()}
        for path, payload in sorted(entries.items())
    ]
    entries["checksums.json"] = json_bytes(
        {"algorithm": "SHA-256", "files": checksums}
    )

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(OUTPUT, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path, payload in sorted(entries.items()):
            info = zipfile.ZipInfo(path, (1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, payload)
    digest = hashlib.sha256(OUTPUT.read_bytes()).hexdigest()
    OUTPUT.with_suffix(OUTPUT.suffix + ".sha256").write_text(
        f"{digest}  {OUTPUT.name}\n", encoding="ascii"
    )
    return OUTPUT


if __name__ == "__main__":
    result = build()
    print(f"Sample character: {result} ({result.stat().st_size} bytes)")
