from __future__ import annotations

import gzip
import hashlib
import io
import math
import struct
import tarfile
import zlib
from pathlib import Path


WORKSPACE = Path(__file__).resolve().parent.parent
ASSETS_ROOT = WORKSPACE / "unity" / "RotoWeave-UnityImporter" / "Assets"
OUTPUT = WORKSPACE / "release" / "RotoWeave-UnityImporter.unitypackage"
SHADOW_ASSET_PATH = "Assets/RotoWeave/Runtime/ShadowGradient.png"


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    )


def shadow_gradient_png(size: int = 64) -> bytes:
    rows = bytearray()
    half = size / 2.0
    for y in range(size):
        rows.append(0)
        for x in range(size):
            normalized_x = (x + 0.5 - half) / half
            normalized_y = (y + 0.5 - half) / half
            radius = math.sqrt(normalized_x * normalized_x + normalized_y * normalized_y)
            t = max(0.0, min(1.0, (radius - 0.04) / 0.96))
            smooth = t * t * (3.0 - 2.0 * t)
            alpha = int(round(255.0 * (1.0 - smooth) ** 1.35))
            rows.extend((255, 255, 255, alpha))
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
        + _png_chunk(b"IDAT", zlib.compress(bytes(rows), 9))
        + _png_chunk(b"IEND", b"")
    )


VIRTUAL_ASSETS = {SHADOW_ASSET_PATH: shadow_gradient_png()}


def guid_for(path: str) -> str:
    return hashlib.md5(f"RotoWeave-UnityImporter:{path}".encode("utf-8")).hexdigest()


def meta_for(path: str, is_directory: bool) -> bytes:
    guid = guid_for(path)
    if is_directory:
        body = f"""fileFormatVersion: 2
guid: {guid}
folderAsset: yes
DefaultImporter:
  externalObjects: {{}}
  userData:
  assetBundleName:
  assetBundleVariant:
"""
    elif path.endswith(".cs"):
        body = f"""fileFormatVersion: 2
guid: {guid}
MonoImporter:
  externalObjects: {{}}
  serializedVersion: 2
  defaultReferences: []
  executionOrder: 0
  icon: {{instanceID: 0}}
  userData:
  assetBundleName:
  assetBundleVariant:
"""
    elif path.endswith(".png"):
        body = f"""fileFormatVersion: 2
guid: {guid}
TextureImporter:
  internalIDToNameTable: []
  externalObjects: {{}}
  serializedVersion: 12
  mipmaps:
    mipMapMode: 0
    enableMipMap: 0
    sRGBTexture: 1
    linearTexture: 0
    fadeOut: 0
    borderMipMap: 0
    mipMapsPreserveCoverage: 0
    alphaTestReferenceValue: 0.5
    mipMapFadeDistanceStart: 1
    mipMapFadeDistanceEnd: 3
  bumpmap:
    convertToNormalMap: 0
    externalNormalMap: 0
    heightScale: 0.25
    normalMapFilter: 0
    flipGreenChannel: 0
  isReadable: 0
  streamingMipmaps: 0
  streamingMipmapsPriority: 0
  vTOnly: 0
  ignoreMasterTextureLimit: 0
  grayScaleToAlpha: 0
  generateCubemap: 6
  cubemapConvolution: 0
  seamlessCubemap: 0
  textureFormat: 1
  maxTextureSize: 64
  textureSettings:
    serializedVersion: 2
    filterMode: 1
    aniso: 1
    mipBias: 0
    wrapU: 1
    wrapV: 1
    wrapW: 1
  nPOTScale: 0
  lightmap: 0
  textureCompression: 0
  compressionQuality: 50
  crunchedCompression: 0
  allowsAlphaSplitting: 0
  spriteMode: 1
  spriteExtrude: 1
  spriteMeshType: 1
  alignment: 0
  spritePivot: {{x: 0.5, y: 0.5}}
  spritePixelsToUnits: 64
  spriteBorder: {{x: 0, y: 0, z: 0, w: 0}}
  spriteGenerateFallbackPhysicsShape: 0
  alphaUsage: 1
  alphaIsTransparency: 1
  spriteTessellationDetail: -1
  textureType: 8
  textureShape: 1
  singleChannelComponent: 0
  flipbookRows: 1
  flipbookColumns: 1
  maxTextureSizeSet: 0
  compressionQualitySet: 0
  textureFormatSet: 0
  ignorePngGamma: 0
  applyGammaDecoding: 0
  cookieLightType: 0
  platformSettings: []
  spriteSheet:
    serializedVersion: 2
    sprites: []
    outline: []
    physicsShape: []
    bones: []
    spriteID: 5e97eb03825dee720800000000000000
    internalID: 0
    vertices: []
    indices:
    edges: []
    weights: []
    secondaryTextures: []
    nameFileIdTable: {{}}
  mipmapLimitGroupName:
  pSDRemoveMatte: 0
  userData:
  assetBundleName:
  assetBundleVariant:
"""
    elif path.endswith(".shader"):
        body = f"""fileFormatVersion: 2
guid: {guid}
ShaderImporter:
  externalObjects: {{}}
  defaultTextures: []
  nonModifiableTextures: []
  preprocessorOverride: 0
  userData:
  assetBundleName:
  assetBundleVariant:
"""
    else:
        body = f"""fileFormatVersion: 2
guid: {guid}
DefaultImporter:
  externalObjects: {{}}
  userData:
  assetBundleName:
  assetBundleVariant:
"""
    return body.encode("utf-8")


def add_bytes(archive: tarfile.TarFile, name: str, payload: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(payload)
    info.mtime = 0
    info.mode = 0o644
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    archive.addfile(info, io.BytesIO(payload))


def build() -> Path:
    if not ASSETS_ROOT.is_dir():
        raise SystemExit(f"Unity source assets are missing: {ASSETS_ROOT}")
    records: list[tuple[str, Path | None, bool]] = []
    for directory in sorted(
        path
        for path in ASSETS_ROOT.rglob("*")
        if path.is_dir() and "Tests" not in path.relative_to(ASSETS_ROOT).parts
    ):
        pathname = directory.relative_to(ASSETS_ROOT.parent).as_posix()
        records.append((pathname, None, True))
    for source in sorted(
        path
        for path in ASSETS_ROOT.rglob("*")
        if path.is_file()
        and not path.name.endswith(".meta")
        and "Tests" not in path.relative_to(ASSETS_ROOT).parts
    ):
        pathname = source.relative_to(ASSETS_ROOT.parent).as_posix()
        records.append((pathname, source, False))
    physical_paths = {pathname for pathname, _, _ in records}
    for pathname in sorted(VIRTUAL_ASSETS):
        if pathname not in physical_paths:
            records.append((pathname, None, False))

    tar_buffer = io.BytesIO()
    with tarfile.open(fileobj=tar_buffer, mode="w", format=tarfile.GNU_FORMAT) as archive:
        for pathname, source, is_directory in sorted(records):
            guid = guid_for(pathname)
            if not is_directory:
                payload = source.read_bytes() if source is not None else VIRTUAL_ASSETS[pathname]
                add_bytes(archive, f"{guid}/asset", payload)
            add_bytes(archive, f"{guid}/asset.meta", meta_for(pathname, is_directory))
            add_bytes(archive, f"{guid}/pathname", pathname.encode("utf-8"))

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT.open("wb") as output_file:
        with gzip.GzipFile(filename="", mode="wb", fileobj=output_file, compresslevel=9, mtime=0) as compressed:
            compressed.write(tar_buffer.getvalue())
    return OUTPUT


if __name__ == "__main__":
    package = build()
    print(f"Unity package: {package} ({package.stat().st_size} bytes)")
