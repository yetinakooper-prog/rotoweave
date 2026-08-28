from __future__ import annotations

import json
import math
import os
import subprocess
import threading
from pathlib import Path
from typing import Any, Callable

import cv2
import numpy as np
from PIL import Image

from .config import Settings
from .repositories.common import new_id
from .images import read_image, write_image
from .linear_media import (
    LinearMediaError,
    UnsupportedTransferError,
    bgr_u8_to_linear_rgb,
    rgb_u16_to_linear_rgb,
    validate_rec709_metadata,
    write_linear_exr,
)


ProgressCallback = Callable[[str, float, str | None], None]
ControlCallback = Callable[[], None]


class MediaError(RuntimeError):
    pass


def _ratio(value: str | int | float | None, fallback: float = 0.0) -> float:
    if value is None:
        return fallback
    if isinstance(value, (int, float)):
        return float(value)
    if "/" in value:
        numerator, denominator = value.split("/", 1)
        try:
            denominator_value = float(denominator)
            return float(numerator) / denominator_value if denominator_value else fallback
        except ValueError:
            return fallback
    try:
        return float(value)
    except ValueError:
        return fallback


def probe_video(path: Path, settings: Settings) -> dict[str, Any]:
    ffprobe = settings.locate_executable("ffprobe")
    if ffprobe:
        command = [
            str(ffprobe),
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=codec_name,width,height,pix_fmt,r_frame_rate,avg_frame_rate,time_base,duration,nb_frames,color_space,color_transfer,color_primaries,color_range:format=duration,format_name,size",
            "-of",
            "json",
            str(path),
        ]
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if result.returncode == 0:
            payload = json.loads(result.stdout or "{}")
            stream = (payload.get("streams") or [{}])[0]
            format_info = payload.get("format") or {}
            duration = _ratio(stream.get("duration"), _ratio(format_info.get("duration"), 0.0))
            fps = _ratio(stream.get("avg_frame_rate"), _ratio(stream.get("r_frame_rate"), 30.0))
            frame_count = int(stream.get("nb_frames") or round(duration * fps))
            return {
                "probe": "ffprobe",
                "codec": stream.get("codec_name"),
                "width": int(stream.get("width") or 0),
                "height": int(stream.get("height") or 0),
                "pixel_format": stream.get("pix_fmt"),
                "fps": fps,
                "fps_rational": stream.get("avg_frame_rate") or stream.get("r_frame_rate"),
                "time_base": stream.get("time_base"),
                "duration": duration,
                "frame_count": frame_count,
                "format": format_info.get("format_name"),
                "color_space": stream.get("color_space"),
                "color_transfer": stream.get("color_transfer"),
                "color_primaries": stream.get("color_primaries"),
                "color_range": stream.get("color_range"),
            }

    capture = cv2.VideoCapture(str(path))
    if not capture.isOpened():
        raise MediaError("无法读取视频；请确认文件完整且格式受支持。")
    fps = capture.get(cv2.CAP_PROP_FPS) or 30.0
    frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
    height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    capture.release()
    return {
        "probe": "opencv-fallback",
        "codec": None,
        "width": width,
        "height": height,
        "pixel_format": None,
        "fps": fps,
        "fps_rational": f"{int(round(fps * 1000))}/1000",
        "time_base": None,
        "duration": frame_count / fps if fps else 0,
        "frame_count": frame_count,
        "format": path.suffix.lower().lstrip("."),
        "warnings": ["ffprobe_unavailable"],
    }


def _create_thumbnail(
    source: Path | np.ndarray,
    target: Path,
    max_size: tuple[int, int] = (320, 240),
) -> None:
    image = (
        read_image(source, cv2.IMREAD_COLOR)
        if isinstance(source, Path)
        else source
    )
    if image is None:
        return
    height, width = image.shape[:2]
    scale = min(max_size[0] / max(width, 1), max_size[1] / max(height, 1), 1.0)
    resized = cv2.resize(
        image,
        (max(1, int(round(width * scale))), max(1, int(round(height * scale)))),
        interpolation=cv2.INTER_AREA,
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    write_image(target, resized, [cv2.IMWRITE_JPEG_QUALITY, 86])


def _extract_with_ffmpeg(
    source: Path,
    output_dir: Path,
    fps: float,
    start_time: float,
    end_time: float | None,
    ffmpeg: Path,
    report: ProgressCallback,
    check_control: ControlCallback,
) -> None:
    duration = max(0.001, (end_time - start_time) if end_time else 0.0)
    command = [
        str(ffmpeg),
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
    ]
    if start_time > 0:
        command.extend(["-ss", f"{start_time:.6f}"])
    command.extend(["-i", str(source)])
    if end_time is not None:
        command.extend(["-t", f"{max(0.001, end_time - start_time):.6f}"])
    command.extend(
        [
            "-vf",
            f"fps={fps:.8f}",
            "-vsync",
            "0",
            "-start_number",
            "0",
            str(output_dir / "%06d.png"),
            "-progress",
            "pipe:1",
            "-nostats",
        ]
    )
    creation_flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=creation_flags,
    )
    try:
        assert process.stdout is not None
        for line in process.stdout:
            check_control()
            key, _, value = line.strip().partition("=")
            if key in {"out_time_us", "out_time_ms"} and duration > 0:
                try:
                    elapsed = float(value) / 1_000_000.0
                    report("extract", min(0.94, elapsed / duration * 0.94), None)
                except ValueError:
                    pass
        return_code = process.wait()
    except BaseException:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
        raise
    if return_code != 0:
        raise MediaError(f"FFmpeg 分帧失败，退出代码 {return_code}。")


def _extract_linear_with_ffmpeg(
    source: Path,
    preview_dir: Path,
    linear_dir: Path,
    metadata: dict[str, Any],
    fps: float,
    start_time: float,
    end_time: float | None,
    ffmpeg: Path,
    report: ProgressCallback,
    check_control: ControlCallback,
) -> int:
    """Decode once to RGB48, then preserve the linear authority as half EXR."""

    try:
        color = validate_rec709_metadata(metadata)
    except UnsupportedTransferError as exc:
        raise MediaError(str(exc)) from exc
    width = int(metadata.get("width") or 0)
    height = int(metadata.get("height") or 0)
    if width <= 0 or height <= 0:
        raise MediaError("Video dimensions are unavailable for linear extraction.")
    preview_dir.mkdir(parents=True, exist_ok=True)
    linear_dir.mkdir(parents=True, exist_ok=True)
    command = [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y"]
    if start_time > 0:
        command.extend(["-ss", f"{start_time:.6f}"])
    command.extend(["-i", str(source)])
    if end_time is not None:
        command.extend(["-t", f"{max(0.001, end_time - start_time):.6f}"])
    command.extend(
        [
            "-vf",
            f"fps={fps:.8f}",
            "-vsync",
            "0",
            "-pix_fmt",
            "rgb48le",
            "-f",
            "rawvideo",
            "pipe:1",
        ]
    )
    creation_flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=creation_flags,
    )
    stderr_lines: list[str] = []

    def drain_stderr() -> None:
        assert process.stderr is not None
        for raw in process.stderr:
            stderr_lines.append(raw.decode("utf-8", errors="replace").rstrip())

    stderr_thread = threading.Thread(
        target=drain_stderr,
        name="ffmpeg-linear-stderr",
        daemon=True,
    )
    stderr_thread.start()
    bytes_per_frame = width * height * 3 * 2
    expected_duration = max(0.0, (end_time or 0.0) - start_time)
    expected_frames = max(1, int(round(expected_duration * fps)))
    frame_index = 0
    try:
        assert process.stdout is not None
        while True:
            check_control()
            chunks: list[bytes] = []
            remaining = bytes_per_frame
            while remaining:
                chunk = process.stdout.read(remaining)
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            if remaining == bytes_per_frame:
                break
            if remaining:
                raise MediaError("FFmpeg returned a truncated RGB48 video frame.")
            encoded_rgb = np.frombuffer(b"".join(chunks), dtype="<u2").reshape(
                height, width, 3
            )
            try:
                linear_rgb = rgb_u16_to_linear_rgb(encoded_rgb, color["transfer"])
                write_linear_exr(
                    linear_dir / f"{frame_index:06d}.exr", linear_rgb
                )
            except LinearMediaError as exc:
                raise MediaError(str(exc)) from exc
            preview_rgb = ((encoded_rgb.astype(np.uint32) + 128) // 257).astype(
                np.uint8
            )
            if not write_image(
                preview_dir / f"{frame_index:06d}.png", preview_rgb[:, :, ::-1]
            ):
                raise MediaError(f"Unable to encode preview frame {frame_index}.")
            frame_index += 1
            report(
                "extract_linear",
                min(0.94, frame_index / expected_frames * 0.94),
                None,
            )
        return_code = process.wait()
        stderr_thread.join(timeout=2)
    except BaseException:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
        raise
    if return_code != 0:
        detail = " | ".join(line for line in stderr_lines[-6:] if line)
        raise MediaError(
            f"FFmpeg linear extraction failed with exit code {return_code}: {detail}"
        )
    if frame_index == 0:
        raise MediaError("FFmpeg produced no linear video frames.")
    return frame_index


def _extract_with_opencv(
    source: Path,
    output_dir: Path,
    target_fps: float,
    start_time: float,
    end_time: float | None,
    report: ProgressCallback,
    check_control: ControlCallback,
) -> None:
    capture = cv2.VideoCapture(str(source))
    if not capture.isOpened():
        raise MediaError("OpenCV 无法打开输入视频。")
    source_fps = capture.get(cv2.CAP_PROP_FPS) or target_fps
    frame_total = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    source_duration = frame_total / source_fps if source_fps else 0
    effective_end = min(end_time or source_duration, source_duration) if source_duration else end_time
    capture.set(cv2.CAP_PROP_POS_MSEC, start_time * 1000)
    next_sample_time = start_time
    output_index = 0
    while True:
        check_control()
        ok, frame = capture.read()
        if not ok:
            break
        current_time = capture.get(cv2.CAP_PROP_POS_MSEC) / 1000.0
        if effective_end is not None and current_time > effective_end + 1e-6:
            break
        if current_time + 1e-6 < next_sample_time:
            continue
        if not write_image(output_dir / f"{output_index:06d}.png", frame):
            raise MediaError(f"frame_{output_index} 写入失败。")
        output_index += 1
        next_sample_time = start_time + output_index / target_fps
        if effective_end and effective_end > start_time:
            report("extract", min(0.94, (current_time - start_time) / (effective_end - start_time) * 0.94), None)
    capture.release()


def extract_frames(
    source: Path,
    output_dir: Path,
    thumb_dir: Path,
    metadata: dict[str, Any],
    target_fps: float | None,
    start_time: float,
    end_time: float | None,
    settings: Settings,
    report: ProgressCallback,
    check_control: ControlCallback,
    selected_timeline: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    output_dir.mkdir(parents=True, exist_ok=True)
    thumb_dir.mkdir(parents=True, exist_ok=True)
    fps = max(1.0, min(60.0, float(target_fps or metadata.get("fps") or 30.0)))
    source_duration = float(metadata.get("duration") or 0.0)
    effective_end = end_time
    if source_duration:
        effective_end = min(effective_end or source_duration, source_duration)
    if effective_end is not None and effective_end <= start_time:
        raise MediaError("结束时间必须晚于开始时间。")

    linear_dir = output_dir.parent / "source_linear"
    ffmpeg = settings.locate_executable("ffmpeg")
    if ffmpeg:
        _extract_linear_with_ffmpeg(
            source,
            output_dir,
            linear_dir,
            metadata,
            fps,
            start_time,
            effective_end,
            ffmpeg,
            report,
            check_control,
        )
        extractor = "ffmpeg-rgb48-linear-exr"
        source_authority = "linear-rec709-half-exr"
    else:
        _extract_with_opencv(
            source, output_dir, fps, start_time, effective_end, report, check_control
        )
        extractor = "opencv-fallback"
        source_authority = "derived-from-8bit-fallback"
        linear_dir.mkdir(parents=True, exist_ok=True)
        for preview in sorted(output_dir.glob("*.png")):
            image = read_image(preview, cv2.IMREAD_COLOR)
            if image is None:
                raise MediaError(f"Unable to decode fallback frame {preview.name}.")
            try:
                write_linear_exr(
                    linear_dir / f"{preview.stem}.exr",
                    bgr_u8_to_linear_rgb(image, "srgb"),
                )
            except LinearMediaError as exc:
                raise MediaError(str(exc)) from exc

    paths = sorted(output_dir.glob("*.png"))
    if not paths:
        raise MediaError("视频没有产生可用帧。")
    duration_us = int(round(1_000_000 / fps))
    selected = sorted(selected_timeline or [], key=lambda item: int(item["ordinal"]))
    if selected:
        base_ordinal = int(selected[0]["ordinal"])
        selected_paths: list[tuple[Path, dict[str, Any] | None]] = []
        for item in selected:
            candidate_index = int(item["ordinal"]) - base_ordinal
            if candidate_index < 0 or candidate_index >= len(paths):
                raise MediaError(
                    f"候选帧 #{int(item['ordinal']) + 1} 未能从原视频精确提取，请重新生成代理时间线。"
                )
            selected_paths.append((paths[candidate_index], item))
        selected_path_set = {path for path, _ in selected_paths}
        for unused_path in paths:
            if unused_path not in selected_path_set:
                unused_path.unlink(missing_ok=True)
                (linear_dir / f"{unused_path.stem}.exr").unlink(missing_ok=True)
    else:
        selected_paths = [(path, None) for path in paths]

    frames: list[dict[str, Any]] = []
    for index, (frame_path, timeline_frame) in enumerate(selected_paths):
        check_control()
        image = read_image(frame_path, cv2.IMREAD_UNCHANGED)
        if image is None:
            continue
        height, width = image.shape[:2]
        linear_path = linear_dir / f"{frame_path.stem}.exr"
        if not linear_path.is_file():
            raise MediaError(
                f"Linear EXR authority is missing for frame {frame_path.stem}."
            )
        thumb_path = thumb_dir / f"{index:06d}.jpg"
        _create_thumbnail(image, thumb_path)
        source_pts_us = (
            int(timeline_frame["source_pts_us"])
            if timeline_frame is not None
            else int(round((start_time + index / fps) * 1_000_000))
        )
        frames.append(
            {
                "id": new_id("frm"),
                "frame_index": index,
                "pts_us": source_pts_us,
                "time_us": int(round(index / fps * 1_000_000)),
                "duration_us": duration_us,
                "width": width,
                "height": height,
                "source_path": str(frame_path),
                "linear_source_path": str(linear_path),
                "source_authority": source_authority,
                "source_color": {
                    **validate_rec709_metadata(metadata),
                    "workingSpace": "linear-rec709",
                },
                "thumb_path": str(thumb_path),
                "source_timeline_ordinal": (
                    int(timeline_frame["ordinal"]) if timeline_frame is not None else None
                ),
            }
        )
        if index % 12 == 0 or index == len(selected_paths) - 1:
            report("thumbnails", 0.94 + 0.06 * (index + 1) / len(selected_paths), None)

    result = {
        "extractor": extractor,
        "source_authority": source_authority,
        "fps": fps,
        "frame_count": len(frames),
        "candidate_frame_count": len(paths),
        "selected_ordinals": [int(item["ordinal"]) for item in selected],
        "start_time": start_time,
        "end_time": effective_end,
        "duration": len(frames) / fps,
    }
    return frames, result
