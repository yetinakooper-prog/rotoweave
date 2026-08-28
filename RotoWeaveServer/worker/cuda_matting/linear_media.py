from __future__ import annotations

import os
from enum import IntFlag
from pathlib import Path
from typing import Literal

# OpenCV intentionally requires an explicit opt-in for its bundled EXR codec.
# Set it before importing cv2 so every application/worker process has the same
# deterministic behaviour.
os.environ.setdefault("OPENCV_IO_ENABLE_OPENEXR", "1")

import cv2
import numpy as np

from .images import read_image
from contracts.integrity import atomic_write_bytes


class LinearMediaError(RuntimeError):
    """Raised when an authority image cannot be decoded or encoded safely."""


class UnsupportedTransferError(LinearMediaError):
    """Raised for HDR/wide-gamut input that 3.0 deliberately does not map."""


class UncertaintyFlag(IntFlag):
    NONE = 0
    ALPHA = 1 << 0
    RGB = 1 << 1
    TEMPORAL = 1 << 3
    SOURCE_LIMIT = 1 << 4


_HDR_TRANSFERS = {
    "arib-std-b67",
    "hlg",
    "pq",
    "smpte2084",
    "smpte-st-2084",
}
_SRGB_TRANSFERS = {"iec61966-2-1", "srgb"}
_REC709_TRANSFERS = {
    "bt470bg",
    "bt601",
    "bt709",
    "smpte170m",
    "smpte240m",
}


def normalize_transfer(value: object) -> Literal["srgb", "bt709"]:
    transfer = str(value or "bt709").strip().lower()
    if transfer in _HDR_TRANSFERS:
        raise UnsupportedTransferError(
            "RotoWeave 3.0 only accepts SDR sRGB/Rec.709; PQ/HLG/HDR input is rejected."
        )
    if transfer in _SRGB_TRANSFERS:
        return "srgb"
    if transfer in _REC709_TRANSFERS or transfer in {"", "unknown", "unspecified"}:
        return "bt709"
    raise UnsupportedTransferError(
        f"RotoWeave 3.0 does not support transfer characteristic: {transfer}."
    )


def validate_rec709_metadata(metadata: dict[str, object]) -> dict[str, str]:
    transfer = normalize_transfer(metadata.get("color_transfer") or metadata.get("transfer"))
    primaries = str(
        metadata.get("color_primaries") or metadata.get("primaries") or "bt709"
    ).strip().lower()
    if primaries not in {"", "bt709", "unknown", "unspecified"}:
        raise UnsupportedTransferError(
            f"RotoWeave 3.0 only accepts Rec.709 primaries; found {primaries}."
        )
    color_range = str(
        metadata.get("color_range") or metadata.get("range") or "tv"
    ).strip().lower()
    if color_range not in {"", "jpeg", "mpeg", "pc", "tv", "unknown", "unspecified"}:
        raise UnsupportedTransferError(f"Unsupported video color range: {color_range}.")
    matrix = str(
        metadata.get("color_space") or metadata.get("matrix") or "bt709"
    ).strip().lower()
    # FFmpeg performs YUV -> RGB using the stream matrix before the raw RGB
    # reaches us.  We still reject genuinely wide-gamut/constant-luminance
    # matrices rather than silently guessing.
    if matrix not in {
        "",
        "bt470bg",
        "bt709",
        "fcc",
        "rgb",
        "smpte170m",
        "unknown",
        "unspecified",
    }:
        raise UnsupportedTransferError(f"Unsupported video matrix: {matrix}.")
    return {
        "transfer": transfer,
        "primaries": "bt709",
        "matrix": matrix or "bt709",
        "range": color_range or "tv",
    }


def encoded_to_linear(rgb: np.ndarray, transfer: str = "bt709") -> np.ndarray:
    value = np.clip(np.asarray(rgb, dtype=np.float32), 0.0, 1.0)
    normalized = normalize_transfer(transfer)
    if normalized == "srgb":
        return np.where(
            value <= 0.04045,
            value / 12.92,
            np.power((value + 0.055) / 1.055, 2.4),
        ).astype(np.float32)
    return np.where(
        value < 0.081,
        value / 4.5,
        np.power((value + 0.099) / 1.099, 1.0 / 0.45),
    ).astype(np.float32)


def linear_to_srgb(rgb: np.ndarray) -> np.ndarray:
    value = np.clip(np.asarray(rgb, dtype=np.float32), 0.0, 1.0)
    return np.where(
        value <= 0.0031308,
        value * 12.92,
        1.055 * np.power(value, 1.0 / 2.4) - 0.055,
    ).astype(np.float32)


def bgr_u8_to_linear_rgb(image: np.ndarray, transfer: str = "srgb") -> np.ndarray:
    if image.ndim != 3 or image.shape[2] < 3 or image.dtype != np.uint8:
        raise LinearMediaError("Expected an 8-bit BGR source image.")
    rgb = image[:, :, :3][:, :, ::-1].astype(np.float32) / 255.0
    return encoded_to_linear(rgb, transfer)


def rgb_u16_to_linear_rgb(image: np.ndarray, transfer: str = "bt709") -> np.ndarray:
    if image.ndim != 3 or image.shape[2] != 3 or image.dtype != np.uint16:
        raise LinearMediaError("Expected a 16-bit RGB source image.")
    return encoded_to_linear(image.astype(np.float32) / 65535.0, transfer)


def _encode_image(extension: str, image: np.ndarray, params: list[int]) -> bytes:
    ok, encoded = cv2.imencode(extension, np.ascontiguousarray(image), params)
    if not ok:
        raise LinearMediaError(f"OpenCV failed to encode {extension} authority image.")
    return encoded.tobytes()


def write_linear_exr(
    path: Path,
    rgb: np.ndarray,
    alpha: np.ndarray | None = None,
) -> None:
    linear_rgb = np.asarray(rgb, dtype=np.float32)
    if linear_rgb.ndim != 3 or linear_rgb.shape[2] != 3:
        raise LinearMediaError("Linear EXR RGB must have shape HxWx3.")
    if not np.isfinite(linear_rgb).all():
        raise LinearMediaError("Linear EXR RGB contains NaN or infinity.")
    bgr = linear_rgb[:, :, ::-1]
    if alpha is not None:
        linear_alpha = np.asarray(alpha, dtype=np.float32)
        if linear_alpha.shape != linear_rgb.shape[:2]:
            raise LinearMediaError("Linear EXR alpha does not match RGB dimensions.")
        if not np.isfinite(linear_alpha).all():
            raise LinearMediaError("Linear EXR alpha contains NaN or infinity.")
        encoded_image = np.dstack((bgr, np.clip(linear_alpha, 0.0, 1.0)))
    else:
        encoded_image = bgr
    params = [cv2.IMWRITE_EXR_TYPE, cv2.IMWRITE_EXR_TYPE_HALF]
    if hasattr(cv2, "IMWRITE_EXR_COMPRESSION") and hasattr(
        cv2, "IMWRITE_EXR_COMPRESSION_ZIP"
    ):
        params.extend(
            [cv2.IMWRITE_EXR_COMPRESSION, cv2.IMWRITE_EXR_COMPRESSION_ZIP]
        )
    atomic_write_bytes(path, _encode_image(".exr", encoded_image, params))


def write_confidence_exr(path: Path, confidence: np.ndarray) -> None:
    value = np.asarray(confidence, dtype=np.float32)
    if value.ndim != 2 or not np.isfinite(value).all():
        raise LinearMediaError("Confidence EXR must be a finite HxW image.")
    params = [cv2.IMWRITE_EXR_TYPE, cv2.IMWRITE_EXR_TYPE_HALF]
    atomic_write_bytes(path, _encode_image(".exr", np.clip(value, 0.0, 1.0), params))


def read_linear_exr(path: Path) -> tuple[np.ndarray, np.ndarray | None]:
    image = read_image(path, cv2.IMREAD_UNCHANGED)
    if image is None or image.dtype not in {np.dtype(np.float16), np.dtype(np.float32)}:
        raise LinearMediaError(f"Unable to read linear EXR: {path.name}.")
    value = image.astype(np.float32)
    if value.ndim == 2:
        return np.repeat(value[:, :, None], 3, axis=2), None
    if value.ndim != 3 or value.shape[2] not in {3, 4}:
        raise LinearMediaError(f"Unexpected EXR channel layout: {path.name}.")
    rgb = value[:, :, :3][:, :, ::-1]
    alpha = value[:, :, 3] if value.shape[2] == 4 else None
    if not np.isfinite(rgb).all() or (alpha is not None and not np.isfinite(alpha).all()):
        raise LinearMediaError(f"EXR contains NaN or infinity: {path.name}.")
    return rgb, alpha


def read_confidence_exr(path: Path) -> np.ndarray:
    image = read_image(path, cv2.IMREAD_UNCHANGED)
    if image is None:
        raise LinearMediaError(f"Unable to read confidence EXR: {path.name}.")
    value = np.asarray(image, dtype=np.float32)
    if value.ndim == 3:
        if value.shape[2] not in {1, 3, 4}:
            raise LinearMediaError(
                f"Unexpected confidence EXR channel layout: {path.name}."
            )
        value = value[:, :, 0]
    if value.ndim != 2 or not np.isfinite(value).all():
        raise LinearMediaError(f"Confidence EXR is not a finite HxW image: {path.name}.")
    if float(value.min()) < -1e-4 or float(value.max()) > 1.0001:
        raise LinearMediaError(f"Confidence EXR is outside 0..1: {path.name}.")
    return np.clip(value, 0.0, 1.0)


def write_uncertainty_png(path: Path, flags: np.ndarray) -> None:
    value = np.asarray(flags)
    if value.ndim != 2:
        raise LinearMediaError("Uncertainty flags must have shape HxW.")
    if np.any(value < 0) or np.any(value > np.iinfo(np.uint16).max):
        raise LinearMediaError("Uncertainty flags exceed uint16.")
    atomic_write_bytes(path, _encode_image(".png", value.astype(np.uint16), []))


def write_alpha_png(path: Path, alpha: np.ndarray) -> None:
    value = np.asarray(alpha, dtype=np.float32)
    if value.ndim != 2 or not np.isfinite(value).all():
        raise LinearMediaError("Alpha PNG must be a finite HxW image.")
    encoded = np.rint(np.clip(value, 0.0, 1.0) * 255.0).astype(np.uint8)
    atomic_write_bytes(
        path,
        _encode_image(".png", encoded, [cv2.IMWRITE_PNG_COMPRESSION, 6]),
    )


def write_delivery_base_png(
    path: Path,
    premultiplied_rgb: np.ndarray,
    alpha: np.ndarray,
) -> None:
    premultiplied = np.asarray(premultiplied_rgb, dtype=np.float32)
    matte = np.asarray(alpha, dtype=np.float32)
    if premultiplied.shape[:2] != matte.shape or premultiplied.shape[2:] != (3,):
        raise LinearMediaError("Delivery base RGB/alpha dimensions do not match.")
    encoded_rgb = np.rint(linear_to_srgb(premultiplied) * 255.0).astype(np.uint8)
    encoded_alpha = np.rint(np.clip(matte, 0.0, 1.0) * 255.0).astype(np.uint8)
    bgra = np.dstack((encoded_rgb[:, :, ::-1], encoded_alpha))
    atomic_write_bytes(path, _encode_image(".png", bgra, [cv2.IMWRITE_PNG_COMPRESSION, 6]))


def write_compatibility_rgba_png(
    path: Path,
    premultiplied_rgb: np.ndarray,
    alpha: np.ndarray,
) -> None:
    matte = np.clip(np.asarray(alpha, dtype=np.float32), 0.0, 1.0)
    premultiplied = np.asarray(premultiplied_rgb, dtype=np.float32)
    straight = np.zeros_like(premultiplied)
    supported = matte > (1.0 / 65535.0)
    straight[supported] = premultiplied[supported] / matte[supported, None]
    encoded_rgb = np.rint(linear_to_srgb(straight) * 255.0).astype(np.uint8)
    encoded_alpha = np.rint(matte * 255.0).astype(np.uint8)
    bgra = np.dstack((encoded_rgb[:, :, ::-1], encoded_alpha))
    atomic_write_bytes(path, _encode_image(".png", bgra, [cv2.IMWRITE_PNG_COMPRESSION, 6]))


def write_emission_png(path: Path, emission_rgb: np.ndarray) -> None:
    emission = np.asarray(emission_rgb, dtype=np.float32)
    if emission.ndim != 3 or emission.shape[2] != 3 or not np.isfinite(emission).all():
        raise LinearMediaError("Emission must be a finite HxWx3 image.")
    # WebGL delivery is RGB24 SDR.  EXR remains authoritative when energy is
    # above 1.0 and the QC result records clipping.
    encoded_rgb = np.rint(linear_to_srgb(np.clip(emission, 0.0, 1.0)) * 255.0).astype(
        np.uint8
    )
    atomic_write_bytes(
        path,
        _encode_image(
            ".png",
            encoded_rgb[:, :, ::-1],
            [cv2.IMWRITE_PNG_COMPRESSION, 6],
        ),
    )


def composite_premultiplied(
    premultiplied_rgb: np.ndarray,
    alpha: np.ndarray,
    background_rgb: tuple[float, float, float] | np.ndarray,
    emission_rgb: np.ndarray | None = None,
) -> np.ndarray:
    premultiplied = np.asarray(premultiplied_rgb, dtype=np.float32)
    matte = np.asarray(alpha, dtype=np.float32)
    background = np.asarray(background_rgb, dtype=np.float32)
    if background.ndim == 1:
        background = np.broadcast_to(background, premultiplied.shape)
    result = premultiplied + (1.0 - matte[:, :, None]) * background
    if emission_rgb is not None:
        result = result + np.asarray(emission_rgb, dtype=np.float32)
    return result
