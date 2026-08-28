from __future__ import annotations

import os
import tempfile
from pathlib import Path

# OpenCV disables its bundled OpenEXR codec unless this flag exists before the
# first cv2 import.  The signed adapter is deliberately self-contained and
# does not import image writers from the main FastAPI process.
os.environ.setdefault("OPENCV_IO_ENABLE_OPENEXR", "1")

import cv2
import numpy as np


class AdapterMediaError(RuntimeError):
    """A signed adapter authority image could not be read or written safely."""


def _atomic_encode(path: Path, extension: str, image: np.ndarray, params: list[int]) -> None:
    ok, encoded = cv2.imencode(extension, np.ascontiguousarray(image), params)
    if not ok:
        raise AdapterMediaError(f"OpenCV failed to encode {path.name}.")
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(encoded.tobytes())
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def read_linear_exr(path: Path) -> tuple[np.ndarray, np.ndarray | None]:
    image = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
    if image is None or image.dtype not in {np.dtype(np.float16), np.dtype(np.float32)}:
        raise AdapterMediaError(f"Unable to read linear EXR: {path.name}.")
    value = image.astype(np.float32)
    if value.ndim != 3 or value.shape[2] not in {3, 4}:
        raise AdapterMediaError(f"Unexpected linear EXR channel layout: {path.name}.")
    # Channel reversal creates a negative stride view.  Torch rejects such
    # arrays, so the adapter authority is materialized as contiguous RGB.
    rgb = np.ascontiguousarray(value[:, :, :3][:, :, ::-1])
    alpha = value[:, :, 3] if value.shape[2] == 4 else None
    if not np.isfinite(rgb).all() or (alpha is not None and not np.isfinite(alpha).all()):
        raise AdapterMediaError(f"Linear EXR contains NaN or infinity: {path.name}.")
    return rgb, alpha


def linear_to_srgb(value: np.ndarray) -> np.ndarray:
    linear = np.clip(np.asarray(value, dtype=np.float32), 0.0, 1.0)
    return np.where(
        linear <= 0.0031308,
        linear * 12.92,
        1.055 * np.power(linear, 1.0 / 2.4) - 0.055,
    ).astype(np.float32)


def linear_rgb_to_srgb_u8(value: np.ndarray) -> np.ndarray:
    return np.rint(linear_to_srgb(value) * 255.0).astype(np.uint8)


def write_linear_exr(path: Path, rgb: np.ndarray, alpha: np.ndarray | None = None) -> None:
    linear_rgb = np.asarray(rgb, dtype=np.float32)
    if (
        linear_rgb.ndim != 3
        or linear_rgb.shape[2] != 3
        or not np.isfinite(linear_rgb).all()
    ):
        raise AdapterMediaError("Linear EXR RGB must be finite HxWx3.")
    encoded = linear_rgb[:, :, ::-1]
    if alpha is not None:
        matte = np.asarray(alpha, dtype=np.float32)
        if matte.shape != linear_rgb.shape[:2] or not np.isfinite(matte).all():
            raise AdapterMediaError("Linear EXR Alpha does not match RGB.")
        encoded = np.dstack((encoded, np.clip(matte, 0.0, 1.0)))
    params = [cv2.IMWRITE_EXR_TYPE, cv2.IMWRITE_EXR_TYPE_HALF]
    if hasattr(cv2, "IMWRITE_EXR_COMPRESSION") and hasattr(
        cv2, "IMWRITE_EXR_COMPRESSION_ZIP"
    ):
        params.extend([cv2.IMWRITE_EXR_COMPRESSION, cv2.IMWRITE_EXR_COMPRESSION_ZIP])
    _atomic_encode(path, ".exr", encoded, params)


def write_confidence_exr(path: Path, confidence: np.ndarray) -> None:
    value = np.asarray(confidence, dtype=np.float32)
    if value.ndim != 2 or not np.isfinite(value).all():
        raise AdapterMediaError("Confidence must be a finite HxW image.")
    _atomic_encode(
        path,
        ".exr",
        np.clip(value, 0.0, 1.0),
        [cv2.IMWRITE_EXR_TYPE, cv2.IMWRITE_EXR_TYPE_HALF],
    )


def read_confidence_exr(path: Path) -> np.ndarray:
    image = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
    if image is None:
        raise AdapterMediaError(f"Unable to read confidence EXR: {path.name}.")
    value = np.asarray(image, dtype=np.float32)
    if value.ndim == 3:
        value = value[:, :, 0]
    if value.ndim != 2 or not np.isfinite(value).all():
        raise AdapterMediaError(f"Confidence EXR is invalid: {path.name}.")
    return np.clip(value, 0.0, 1.0)


def write_uncertainty_png(path: Path, flags: np.ndarray) -> None:
    value = np.asarray(flags)
    if value.ndim != 2 or np.any(value < 0) or np.any(value > 65535):
        raise AdapterMediaError("Uncertainty must fit a uint16 HxW image.")
    _atomic_encode(path, ".png", value.astype(np.uint16), [])


def write_delivery_base_png(path: Path, premultiplied: np.ndarray, alpha: np.ndarray) -> None:
    rgb = np.asarray(premultiplied, dtype=np.float32)
    matte = np.clip(np.asarray(alpha, dtype=np.float32), 0.0, 1.0)
    if rgb.shape != (*matte.shape, 3):
        raise AdapterMediaError("Delivery Base RGB and Alpha dimensions do not match.")
    bgra = np.dstack(
        (
            np.rint(linear_to_srgb(rgb) * 255.0).astype(np.uint8)[:, :, ::-1],
            np.rint(matte * 255.0).astype(np.uint8),
        )
    )
    _atomic_encode(path, ".png", bgra, [cv2.IMWRITE_PNG_COMPRESSION, 6])


def write_delivery_emission_png(path: Path, emission: np.ndarray) -> None:
    rgb = np.asarray(emission, dtype=np.float32)
    if rgb.ndim != 3 or rgb.shape[2] != 3 or not np.isfinite(rgb).all():
        raise AdapterMediaError("Delivery Emission must be a finite HxWx3 image.")
    # EXR remains the linear authority.  Desktop WebGL delivery is RGB24 SDR,
    # so values above display white are clipped only in this derived PNG.
    bgr = np.rint(linear_to_srgb(np.clip(rgb, 0.0, 1.0)) * 255.0).astype(
        np.uint8
    )[:, :, ::-1]
    _atomic_encode(path, ".png", bgr, [cv2.IMWRITE_PNG_COMPRESSION, 6])


def write_compatibility_rgba_png(
    path: Path, premultiplied: np.ndarray, alpha: np.ndarray
) -> None:
    rgb = np.asarray(premultiplied, dtype=np.float32)
    matte = np.clip(np.asarray(alpha, dtype=np.float32), 0.0, 1.0)
    if rgb.shape != (*matte.shape, 3):
        raise AdapterMediaError("Compatibility RGB and Alpha dimensions do not match.")
    straight = np.zeros_like(rgb)
    supported = matte > (1.0 / 65535.0)
    straight[supported] = rgb[supported] / matte[supported, None]
    bgra = np.dstack(
        (
            np.rint(linear_to_srgb(straight) * 255.0).astype(np.uint8)[:, :, ::-1],
            np.rint(matte * 255.0).astype(np.uint8),
        )
    )
    _atomic_encode(path, ".png", bgra, [cv2.IMWRITE_PNG_COMPRESSION, 6])


__all__ = [
    "AdapterMediaError",
    "linear_rgb_to_srgb_u8",
    "read_confidence_exr",
    "read_linear_exr",
    "write_compatibility_rgba_png",
    "write_confidence_exr",
    "write_delivery_base_png",
    "write_delivery_emission_png",
    "write_linear_exr",
    "write_uncertainty_png",
]
