from __future__ import annotations

from pathlib import Path
from typing import Sequence

import cv2
import numpy as np


def read_image(path: str | Path, flags: int = cv2.IMREAD_COLOR) -> np.ndarray | None:
    """Read an image without relying on OpenCV's Windows ANSI path handling."""

    try:
        encoded = np.fromfile(str(Path(path)), dtype=np.uint8)
        if encoded.size == 0:
            return None
        return cv2.imdecode(encoded, flags)
    except (OSError, ValueError, cv2.error):
        return None


def write_image(
    path: str | Path,
    image: np.ndarray,
    params: Sequence[int] | None = None,
) -> bool:
    """Write an image through imencode/tofile so Unicode paths work on Windows."""

    target = Path(path)
    suffix = target.suffix.lower()
    if not suffix:
        return False
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        ok, encoded = cv2.imencode(suffix, image, list(params or ()))
        if not ok:
            return False
        encoded.tofile(str(target))
        return True
    except (OSError, ValueError, cv2.error):
        return False
