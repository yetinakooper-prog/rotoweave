from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
IMPORTER = (
    ROOT
    / "unity"
    / "RotoWeave-UnityImporter"
    / "Assets"
    / "RotoWeave"
    / "Editor"
    / "RotoWeaveCharacterImporter.cs"
)
MANIFEST = IMPORTER.with_name("RotoWeaveCharacterManifest.cs")
CONTRACT = IMPORTER.with_name("RotoWeaveProductContract.cs")
RUNTIME_CHARACTER = (
    ROOT
    / "unity"
    / "RotoWeave-UnityImporter"
    / "Assets"
    / "RotoWeave"
    / "Runtime"
    / "RotoWeaveCharacter.cs"
)


def test_importer8_strictly_consumes_format3_deduplicated_sprites() -> None:
    importer = IMPORTER.read_text(encoding="utf-8")
    manifest = MANIFEST.read_text(encoding="utf-8")
    contract = CONTRACT.read_text(encoding="utf-8")

    assert 'ScriptedImporterVersion = 8' in contract
    assert 'CharacterPackageFormat = 3' in contract
    assert 'CharacterPackageShape = "deduplicated-atlas-v3"' in contract
    assert "public RotoWeaveSprite[] sprites;" in manifest
    assert "public RotoWeaveFrameTransform transform;" in manifest
    assert "manifest.formatVersion != RotoWeaveProductContract.CharacterPackageFormat" in importer
    assert "manifest.sprites == null || manifest.sprites.Length == 0" in importer
    assert "BuildSprites(" in importer
    assert "EnsureUnique(manifest.sprites.Select(item => item.id), \"Sprite ID\")" in importer
    assert "EnsureUnique(spriteIds" not in importer


def test_importer8_writes_rotoweave_and_only_imports_exact_predecessor_packages() -> None:
    importer = IMPORTER.read_text(encoding="utf-8")

    assert 'ScriptedImporter(RotoWeaveProductContract.ScriptedImporterVersion, "rotoweave", "aifcharacter")' in importer
    assert 'extension == ".rotoweave"' in importer
    assert 'extension == ".aifcharacter"' in importer
    assert '? "AIFrameTools"' in importer
    assert "旧 .aifcharacter 不是精确的 AIFrameTools 4.0.0 格式 3 契约" in importer


def test_importer8_validates_and_exposes_the_design_size_contract() -> None:
    importer = IMPORTER.read_text(encoding="utf-8")
    manifest = MANIFEST.read_text(encoding="utf-8")
    runtime = RUNTIME_CHARACTER.read_text(encoding="utf-8")

    assert "public RotoWeaveDesignSize designSize;" in manifest
    assert "public int widthPixels;" in manifest
    assert "public float widthWorld;" in manifest
    assert "manifest.character.designSize.pixelsPerUnit" in importer
    assert "角色尺寸框缺失或像素、Unity 世界单位与固定 PPU 不一致" in importer
    assert "public Vector2 DesignSizeWorld => designSizeWorld;" in runtime
    assert "public float PixelsPerUnit => pixelsPerUnit;" in runtime


def test_importer8_uses_accumulated_timing_and_complete_frame_curves() -> None:
    importer = IMPORTER.read_text(encoding="utf-8")

    assert "FrameStartTimes(animation)" in importer
    assert "elapsed += animation.frames[index].durationSeconds" in importer
    assert "index * cycleDuration / animation.frames.Length" not in importer
    for binding in (
        '"m_LocalPosition.x"',
        '"m_LocalPosition.y"',
        '"m_LocalScale.x"',
        '"m_LocalScale.y"',
        '"localEulerAnglesRaw.z"',
        '"m_Color.r"',
        '"m_Color.g"',
        '"m_Color.b"',
        '"m_Color.a"',
        "shadowAnimationPath",
    ):
        assert binding in importer
    assert "AnimationUtility.TangentMode.Constant" in importer
    assert "accumulatedDuration - duration" in importer
    assert "AddTerminalObjectKey(baseKeys, animation.loop, cycleDuration)" in importer
    assert "new Keyframe(cycleDuration, terminalValue)" in importer
    assert "frameRate = Mathf.Clamp(animation.frameRate, 1f, 60f)" in importer
    assert "animation.frames.Length / duration" in importer
    assert "Mathf.Abs(animation.frameRate - resolvedFrameRate) > 0.001f" in importer


def test_importer8_keeps_texture_resolution_and_visual_scale_independent() -> None:
    importer = IMPORTER.read_text(encoding="utf-8")

    assert "resolvedPixelsPerUnit * sprite.outputScale" in importer
    assert '"m_LocalScale.x"' in importer
    assert '"m_LocalScale.y"' in importer


def test_importer8_creates_shadow_renderer_for_global_or_per_frame_enablement() -> None:
    importer = IMPORTER.read_text(encoding="utf-8")

    assert "manifest.character.shadow.enabled ||" in importer
    assert "manifest.animations.Any(animation =>" in importer
    assert "frame.transform.shadow.enabled" in importer
    assert "geometry.alpha" in importer


def test_importer8_requires_complete_resolved_shadow_geometry() -> None:
    importer = IMPORTER.read_text(encoding="utf-8")
    manifest = MANIFEST.read_text(encoding="utf-8")

    assert "public RotoWeaveShadowFrame shadow;" in manifest
    assert "public RotoWeaveVector2 positionPx;" in manifest
    assert "public float widthPx;" in manifest
    assert "public float depthPx;" in manifest
    assert "public float alpha;" in manifest
    assert "frame.shadow == null" in importer
    assert "geometry.positionPx.x / resolvedPixelsPerUnit" in importer
    assert "geometry.widthPx / resolvedPixelsPerUnit / shadowSprite.bounds.size.x" in importer
    assert "geometry.alpha" in importer
    assert "usesResolvedShadowGeometry" not in importer
    assert "LegacyShadowAnimationPath" not in importer
