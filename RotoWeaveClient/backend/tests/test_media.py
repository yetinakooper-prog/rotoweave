from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

from backend.app.media import _ratio, extract_frames


class OpenCvOnlySettings:
    @staticmethod
    def locate_executable(_: str) -> None:
        return None


def test_ratio_parses_ffprobe_rationals() -> None:
    assert abs(_ratio("30000/1001") - 29.97002997) < 1e-6
    assert _ratio("0/0", 24.0) == 24.0


def test_opencv_cfr_resampling_records_timeline(tmp_path: Path) -> None:
    video = tmp_path / "source.avi"
    writer = cv2.VideoWriter(str(video), cv2.VideoWriter_fourcc(*"MJPG"), 10.0, (80, 48))
    assert writer.isOpened()
    for index in range(10):
        image = np.full((48, 80, 3), (0, 220, 0), dtype=np.uint8)
        cv2.circle(image, (10 + index * 5, 24), 7, (20, 40, 220), -1)
        writer.write(image)
    writer.release()

    frames, result = extract_frames(
        video,
        tmp_path / "frames",
        tmp_path / "thumbs",
        {"fps": 10.0, "duration": 1.0},
        5.0,
        0.0,
        1.0,
        OpenCvOnlySettings(),  # type: ignore[arg-type]
        lambda *_: None,
        lambda: None,
    )

    assert result["extractor"] == "opencv-fallback"
    assert 4 <= len(frames) <= 6
    assert all(frame["duration_us"] == 200_000 for frame in frames)
    assert [frame["frame_index"] for frame in frames] == list(range(len(frames)))
    assert all(Path(frame["source_path"]).is_file() for frame in frames)


def test_selected_candidate_frames_are_continuously_renumbered_at_target_fps(
    tmp_path: Path,
) -> None:
    video = tmp_path / "selected.avi"
    writer = cv2.VideoWriter(str(video), cv2.VideoWriter_fourcc(*"MJPG"), 10.0, (80, 48))
    assert writer.isOpened()
    for index in range(10):
        image = np.full((48, 80, 3), (index * 15, 180, 20), dtype=np.uint8)
        writer.write(image)
    writer.release()
    selected = [
        {"ordinal": 1, "source_pts_us": 200_000, "duration_us": 200_000},
        {"ordinal": 3, "source_pts_us": 600_000, "duration_us": 200_000},
    ]

    frames, result = extract_frames(
        video,
        tmp_path / "selected-frames",
        tmp_path / "selected-thumbs",
        {"fps": 10.0, "duration": 1.0},
        5.0,
        0.2,
        0.8,
        OpenCvOnlySettings(),  # type: ignore[arg-type]
        lambda *_: None,
        lambda: None,
        selected,
    )

    assert result["selected_ordinals"] == [1, 3]
    assert result["duration"] == 0.4
    assert [frame["frame_index"] for frame in frames] == [0, 1]
    assert [frame["time_us"] for frame in frames] == [0, 200_000]
    assert [frame["duration_us"] for frame in frames] == [200_000, 200_000]
    assert [frame["source_timeline_ordinal"] for frame in frames] == [1, 3]
    assert [frame["pts_us"] for frame in frames] == [200_000, 600_000]

