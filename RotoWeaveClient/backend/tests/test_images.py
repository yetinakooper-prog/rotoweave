from pathlib import Path

import cv2
import numpy as np

from backend.app.images import read_image, write_image


def test_unicode_image_path_round_trip(tmp_path: Path) -> None:
    target = tmp_path / "中文路径" / "绿幕帧.png"
    expected = np.zeros((7, 9, 4), dtype=np.uint8)
    expected[:, :, 1] = 255
    expected[:, :, 3] = 173

    assert write_image(target, expected) is True
    actual = read_image(target, cv2.IMREAD_UNCHANGED)

    assert actual is not None
    np.testing.assert_array_equal(actual, expected)
