from __future__ import annotations

import gc
import hashlib
import json
import multiprocessing
import os
import queue
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Iterator

os.environ.setdefault("OPENCV_IO_ENABLE_OPENEXR", "1")

import cv2
import numpy as np

try:
    from .adapter_media import (
        linear_rgb_to_srgb_u8,
        read_confidence_exr,
        read_linear_exr,
        write_compatibility_rgba_png,
        write_confidence_exr,
        write_delivery_base_png,
        write_delivery_emission_png,
        write_linear_exr,
        write_uncertainty_png,
    )
    from .model_runtime import (
        FrozenModelLayout,
        infer_corridorkey,
        infer_sam3_alpha,
        infer_sam2matting_alpha,
        infer_vitmatte_alpha,
        load_corridorkey,
        load_sam3,
        load_sam2matting_bplus,
        load_vitmatte_base,
    )
except ImportError:
    from adapter_media import (  # type: ignore[no-redef]
        linear_rgb_to_srgb_u8,
        read_confidence_exr,
        read_linear_exr,
        write_compatibility_rgba_png,
        write_confidence_exr,
        write_delivery_base_png,
        write_delivery_emission_png,
        write_linear_exr,
        write_uncertainty_png,
    )
    from model_runtime import (  # type: ignore[no-redef]
        FrozenModelLayout,
        infer_corridorkey,
        infer_sam3_alpha,
        infer_sam2matting_alpha,
        infer_vitmatte_alpha,
        load_corridorkey,
        load_sam3,
        load_sam2matting_bplus,
        load_vitmatte_base,
    )


ALPHA_UNCERTAINTY = np.uint16(1 << 0)
RGB_UNCERTAINTY = np.uint16(1 << 1)
TEMPORAL_UNCERTAINTY = np.uint16(1 << 3)

GHOST_COMPONENT_MIN_IMAGE_RATIO = 0.005
GHOST_COMPONENT_MAX_ALPHA_P95 = 0.35
GHOST_COMPONENT_MAX_STRAIGHT_RGB_P90 = 0.42


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Unable to read signed-adapter input: {path.name}.") from exc
    if not isinstance(value, dict):
        raise RuntimeError("Signed-adapter input manifest must be an object.")
    return value


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_path(root: Path, value: object, *, exists: bool) -> Path:
    path = Path(str(value or "")).resolve(strict=False)
    try:
        path.relative_to(root.resolve())
    except ValueError as exc:
        raise RuntimeError(f"Adapter path escapes its generation: {path.name}.") from exc
    if path.is_symlink() or (exists and not path.is_file()):
        raise RuntimeError(f"Adapter authority path is unavailable: {path.name}.")
    return path


def _unload_cuda(*objects: object) -> None:
    # Models are intentionally phase-resident: SAM produces Alpha evidence for
    # the sequence, then it is unloaded before CorridorKey or ViTMatte starts.
    del objects
    gc.collect()
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.synchronize()
            torch.cuda.empty_cache()
            torch.cuda.ipc_collect()
    except (ImportError, RuntimeError):
        pass


def _phase_memory_receipt() -> dict[str, float]:
    allocated = reserved = 0.0
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.synchronize()
            allocated = float(torch.cuda.max_memory_allocated() / 2**20)
            reserved = float(torch.cuda.max_memory_reserved() / 2**20)
    except (ImportError, RuntimeError):
        pass
    working_set = 0.0
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("rest", ctypes.c_size_t * 7),
            ]

        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        if ctypes.windll.psapi.GetProcessMemoryInfo(
            ctypes.windll.kernel32.GetCurrentProcess(),
            ctypes.byref(counters),
            counters.cb,
        ):
            working_set = float(counters.PeakWorkingSetSize / 2**20)
    else:
        try:
            import resource

            working_set = float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss) / 1024.0
        except (ImportError, OSError):
            pass
    return {
        "peakAllocatedMiB": allocated,
        "peakReservedMiB": reserved,
        "peakWorkingSetMiB": working_set,
    }


def _isolated_sam_phase(
    output: Any,
    profile: str,
    generation_root: str,
    frames: list[dict[str, Any]],
    evidence: list[dict[str, Any]],
    intermediate: str,
) -> None:
    sys = __import__("sys")
    sys.stdout = sys.stderr
    try:
        layout = FrozenModelLayout.from_environment()
        predictor = load_sam3(layout) if profile == "ultra" else load_sam2matting_bplus(layout)
        try:
            for ordinal, (record, screen_record) in enumerate(zip(frames, evidence)):
                _, rgb = _shape_checked_source(Path(generation_root), record)
                hint = _alpha_hint(rgb, screen_record["screen"])
                alpha = (
                    infer_sam3_alpha(predictor, linear_rgb_to_srgb_u8(rgb), hint)
                    if profile == "ultra"
                    else infer_sam2matting_alpha(predictor, linear_rgb_to_srgb_u8(rgb), hint)
                )
                np.save(Path(intermediate) / f"{ordinal:06d}.npy", alpha.astype(np.float16))
        finally:
            memory = _phase_memory_receipt()
            del predictor
            _unload_cuda()
        output.put({"ok": True, "memory": memory})
    except BaseException:
        output.put({"ok": False, "error": traceback.format_exc()})


def _isolated_vitmatte_phase(
    output: Any,
    generation_root: str,
    frames: list[dict[str, Any]],
    results: list[dict[str, Any]],
    screen_evidence: list[dict[str, Any]],
) -> None:
    sys = __import__("sys")
    sys.stdout = sys.stderr
    calls: list[tuple[str, str, bool]] = []
    try:
        attempted, completed = _apply_vitmatte_refinements(
            layout=FrozenModelLayout.from_environment(),
            generation_root=Path(generation_root),
            frames=frames,
            results=results,
            route="chroma_character",
            screen_evidence=screen_evidence,
            enabled=True,
            check_cancel=lambda: None,
            record_call=lambda frame_id, model_id, roi=False: calls.append(
                (str(frame_id), str(model_id), bool(roi))
            ),
            record_interval=lambda _kind, _start, _end: None,
        )
        output.put({
            "ok": True,
            "attempted": attempted,
            "completed": completed,
            "results": results,
            "calls": calls,
            "memory": _phase_memory_receipt(),
        })
    except BaseException:
        output.put({"ok": False, "error": traceback.format_exc()})


def _run_isolated_phase(
    target: Callable[..., None],
    args: tuple[Any, ...],
    check_cancel: Callable[[], None],
) -> dict[str, Any]:
    context = multiprocessing.get_context("spawn")
    output = context.Queue(maxsize=1)
    process = context.Process(target=target, args=(output, *args), daemon=False)
    process.start()
    try:
        while process.is_alive():
            check_cancel()
            process.join(timeout=0.25)
        process.join(timeout=5)
        try:
            result = output.get(timeout=5)
        except queue.Empty as exc:
            raise RuntimeError(
                f"Isolated CUDA phase exited with code {process.exitcode} without a receipt."
            ) from exc
        if not isinstance(result, dict) or result.get("ok") is not True:
            raise RuntimeError(str((result or {}).get("error") or "Isolated CUDA phase failed."))
        return result
    except BaseException:
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
        raise
    finally:
        output.close()
        output.join_thread()


def _border_pixels(rgb: np.ndarray) -> np.ndarray:
    height, width = rgb.shape[:2]
    band = max(2, int(round(min(height, width) * 0.05)))
    return np.concatenate(
        (
            rgb[:band].reshape(-1, 3),
            rgb[-band:].reshape(-1, 3),
            rgb[band:-band, :band].reshape(-1, 3),
            rgb[band:-band, -band:].reshape(-1, 3),
        ),
        axis=0,
    )


def _screen_evidence(rgb: np.ndarray) -> dict[str, Any]:
    border = _border_pixels(np.clip(rgb, 0.0, 1.0))
    screen = np.median(border, axis=0).astype(np.float32)
    spread = float(np.percentile(np.linalg.norm(border - screen, axis=1), 90))
    green_dominance = float(screen[1] - max(screen[0], screen[2]))
    blue_dominance = float(screen[2] - max(screen[0], screen[1]))

    def confidence(dominance: float) -> float:
        return float(
            np.clip((dominance - 0.035) / 0.30, 0.0, 1.0)
            * np.clip(1.0 - spread / 0.24, 0.0, 1.0)
        )

    return {
        "screen": screen,
        "spread": spread,
        "greenConfidence": confidence(green_dominance),
        "blueConfidence": confidence(blue_dominance),
    }


def _alpha_hint(rgb: np.ndarray, screen: np.ndarray) -> np.ndarray:
    distance = np.linalg.norm(np.clip(rgb, 0.0, 1.0) - screen[None, None, :], axis=2)
    foreground = 1.0 - np.exp(-np.square(distance / 0.11))
    foreground[foreground < 0.08] = 0.0
    foreground = cv2.GaussianBlur(foreground.astype(np.float32), (0, 0), 0.75)
    return np.clip(foreground, 0.0, 1.0)


def _fit_local_screen_plate(
    rgb: np.ndarray,
    alpha: np.ndarray,
    screen_record: dict[str, Any],
    screen_color: str,
) -> np.ndarray:
    """Fit a smooth local clean-screen plate without crossing subject pixels.

    Real generated green/blue screens are rarely spatially constant.  A single
    border median makes harmless vignetting look like a reconstruction failure
    and, more importantly, over-subtracts the screen in soft-effect pixels.
    The small robust grid keeps only low-Alpha, screen-dominant observations;
    missing cells are filled on the grid before it is expanded to full size.
    """

    image = np.clip(np.asarray(rgb, dtype=np.float32), 0.0, 1.0)
    matte = np.clip(np.asarray(alpha, dtype=np.float32).squeeze(), 0.0, 1.0)
    height, width = matte.shape
    rows = int(np.clip(round(height / 80.0), 6, 16))
    cols = int(np.clip(round(width / 80.0), 6, 16))
    global_screen = np.asarray(screen_record["screen"], dtype=np.float32)
    screen_channel = 1 if screen_color == "green" else 2
    other = np.max(np.delete(image, screen_channel, axis=2), axis=2)
    dominance = image[:, :, screen_channel] - other
    distance = np.linalg.norm(image - global_screen[None, None, :], axis=2)
    samples = (matte < 0.03) & (dominance > 0.025) & (distance < 0.28)

    grid = np.tile(global_screen, (rows, cols, 1)).astype(np.float32)
    missing = np.full((rows, cols), 255, dtype=np.uint8)
    for row in range(rows):
        y0, y1 = row * height // rows, (row + 1) * height // rows
        for col in range(cols):
            x0, x1 = col * width // cols, (col + 1) * width // cols
            values = image[y0:y1, x0:x1][samples[y0:y1, x0:x1]]
            if values.shape[0] >= 32:
                grid[row, col] = np.median(values, axis=0)
                missing[row, col] = 0
    if np.any(missing == 0):
        for channel in range(3):
            grid[:, :, channel] = cv2.inpaint(
                grid[:, :, channel], missing, 2.0, cv2.INPAINT_TELEA
            )
    grid = cv2.GaussianBlur(grid, (0, 0), 0.7)
    plate = cv2.resize(grid, (width, height), interpolation=cv2.INTER_CUBIC)
    return np.clip(plate, 0.0, 1.0).astype(np.float32)


def _corridor_screen_authority(
    rgb: np.ndarray,
    alpha: np.ndarray,
    processed: np.ndarray,
    local_plate: np.ndarray,
    screen_color: str,
) -> np.ndarray:
    """Recover K from CorridorKey's unmixing while retaining local plate fallback.

    CorridorKey returns linear premultiplied foreground.  In the unknown band
    this lets us solve K=(C-P)/(1-A).  Final RGB is still reconstructed from the
    original C, this K authority and the final Alpha, including after ViTMatte.
    """

    matte = np.clip(np.asarray(alpha, dtype=np.float32).squeeze(), 0.0, 1.0)
    value = np.asarray(processed, dtype=np.float32)
    if value.ndim != 3 or value.shape[:2] != matte.shape or value.shape[2] != 4:
        raise RuntimeError("CorridorKey processed authority must be linear premultiplied RGBA.")
    premultiplied = np.clip(value[:, :, :3], 0.0, 1.0)
    denominator = np.maximum(1.0 - matte[:, :, None], 1e-4)
    estimated = (np.asarray(rgb, dtype=np.float32) - premultiplied) / denominator
    # CorridorKey is the designated color-unmixing authority.  Its implied K
    # may differ strongly from the visible green/blue plate around translucent
    # cyan/white effects; rejecting that estimate re-introduces the very screen
    # contamination the model recovered.  Use the local clean plate only where
    # Alpha supplies no stable two-layer solution.
    model_supported = (matte > 0.001) & (matte < 0.995)
    # Alpha makes K irrelevant at both exact limits.  Inside the unknown band,
    # blending K back toward the visible green/blue plate would reintroduce a
    # colored halo, so CorridorKey's solved authority is used without a fade.
    weight = model_supported.astype(np.float32)
    authority = (
        local_plate * (1.0 - weight[:, :, None])
        + estimated * weight[:, :, None]
    )
    # Do not clip the model-implied K.  CorridorKey may intentionally return a
    # virtual plate outside display-referred [0, 1] in order to describe
    # decontaminated glow pixels.  Keeping that finite authority makes
    # C-(1-A)K reproduce its premultiplied result exactly; clipping K creates
    # the green/magenta rims this route exists to remove.
    if not np.all(np.isfinite(authority)):
        raise RuntimeError("CorridorKey produced a non-finite screen authority.")
    return authority.astype(np.float32)


def _reconstruct_chroma(
    rgb: np.ndarray,
    alpha: np.ndarray,
    screen_authority: np.ndarray,
    *,
    support_hint: np.ndarray | None = None,
    unmix_authority: tuple[np.ndarray, np.ndarray] | None = None,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Rebuild premultiplied RGB and raise only supported Alpha to feasibility."""

    source = np.asarray(rgb, dtype=np.float32)
    matte = np.clip(np.asarray(alpha, dtype=np.float32).squeeze(), 0.0, 1.0)
    screen = np.asarray(screen_authority, dtype=np.float32)
    if source.shape != screen.shape or source.shape[:2] != matte.shape:
        raise RuntimeError("Chroma reconstruction authorities do not share dimensions.")
    lower = np.where(
        screen > 1e-4,
        1.0 - source / np.maximum(screen, 1e-4),
        0.0,
    )
    upper = np.where(
        screen < 0.9999,
        (source - screen) / np.maximum(1.0 - screen, 1e-4),
        0.0,
    )
    feasible = np.clip(np.max(np.maximum(lower, upper), axis=2), 0.0, 1.0)
    if unmix_authority is not None:
        unmix_alpha_for_feasibility = np.clip(
            np.asarray(unmix_authority[1], dtype=np.float32).squeeze(), 0.0, 1.0
        )
        # CorridorKey's own unknown band is already a complete unmixing
        # solution.  A display-gamut feasibility clamp must not rewrite its
        # Alpha merely because its virtual K sits outside [0, 1].
        model_supported = (
            (unmix_alpha_for_feasibility > 0.001)
            & (unmix_alpha_for_feasibility < 0.995)
        )
        feasible[model_supported] = 0.0
    feasible[feasible < 0.008] = 0.0
    supported = matte > 0.001
    if support_hint is not None:
        supported |= np.asarray(support_hint, dtype=np.float32).squeeze() > 0.001
    feasible[~supported] = 0.0
    final_alpha = np.maximum(matte, feasible)
    raw = source - (1.0 - final_alpha[:, :, None]) * screen
    conflict = np.any(
        (raw < -0.004) | (raw > final_alpha[:, :, None] + 0.004), axis=2
    )
    premultiplied = np.clip(raw, 0.0, final_alpha[:, :, None]).astype(np.float32)
    model_foreground_fallback = np.zeros(matte.shape, dtype=bool)
    if unmix_authority is not None:
        unmix_premultiplied = np.asarray(unmix_authority[0], dtype=np.float32)
        unmix_alpha = np.clip(
            np.asarray(unmix_authority[1], dtype=np.float32).squeeze(), 0.0, 1.0
        )
        if (
            unmix_premultiplied.shape != source.shape
            or unmix_alpha.shape != matte.shape
        ):
            raise RuntimeError("CorridorKey unmix authority dimensions changed.")
        implied_k = (source - unmix_premultiplied) / np.maximum(
            1.0 - unmix_alpha[:, :, None], 1e-4
        )
        model_foreground_fallback = (
            (unmix_alpha > 0.001)
            & (unmix_alpha < 0.995)
            & np.any((implied_k < -0.004) | (implied_k > 1.004), axis=2)
        )
        straight = np.zeros_like(unmix_premultiplied)
        supported_unmix = unmix_alpha > 1e-5
        straight[supported_unmix] = (
            unmix_premultiplied[supported_unmix]
            / unmix_alpha[supported_unmix, None]
        )
        recovered = np.clip(straight, 0.0, 1.0) * final_alpha[:, :, None]
        premultiplied[model_foreground_fallback] = recovered[
            model_foreground_fallback
        ]
        conflict[model_foreground_fallback] = False
    return (
        premultiplied,
        final_alpha.astype(np.float32),
        conflict,
        model_foreground_fallback,
    )


def _suppress_disconnected_low_energy_ghosts(
    premultiplied: np.ndarray,
    alpha: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, int]:
    """Remove large, dim soft silhouettes without deleting real VFX.

    Generated/interpolated video can bake a faint adjacent pose into the
    current opaque frame. Chroma unmixing makes that otherwise inconspicuous
    residue visible on transparency. The residue differs from supported glow:
    it is a large low-Alpha component, separated from the current subject core,
    and has no high-energy reconstructed foreground pixels.

    Small particles, bright glow trails, subject soft edges, and every pixel
    close to the main opaque component remain protected.
    """

    matte = np.clip(np.asarray(alpha, dtype=np.float32).squeeze(), 0.0, 1.0)
    base = np.asarray(premultiplied, dtype=np.float32)
    if base.ndim != 3 or base.shape[:2] != matte.shape or base.shape[2] != 3:
        raise RuntimeError("Ghost suppression authorities do not share dimensions.")
    height, width = matte.shape
    opaque = (matte >= 0.70).astype(np.uint8)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(opaque, 8)
    if count <= 1:
        return base, matte, np.zeros(matte.shape, dtype=bool), 0
    main_label = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    main = (labels == main_label).astype(np.uint8)
    radius = max(3, int(round(min(height, width) * 0.015)))
    protected = cv2.dilate(
        main,
        cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (radius * 2 + 1,) * 2),
    ).astype(bool)
    soft = (matte > 0.015) & (matte < 0.70) & ~protected
    component_count, component_labels, component_stats, _ = (
        cv2.connectedComponentsWithStats(soft.astype(np.uint8), 8)
    )
    minimum_area = max(
        64, int(round(height * width * GHOST_COMPONENT_MIN_IMAGE_RATIO))
    )
    removed = np.zeros(matte.shape, dtype=bool)
    removed_components = 0
    for label in range(1, component_count):
        if int(component_stats[label, cv2.CC_STAT_AREA]) < minimum_area:
            continue
        component = component_labels == label
        component_alpha = matte[component]
        straight = base[component] / np.maximum(component_alpha[:, None], 1e-4)
        straight_energy = np.max(np.clip(straight, 0.0, 1.0), axis=1)
        if (
            float(np.percentile(component_alpha, 95))
            <= GHOST_COMPONENT_MAX_ALPHA_P95
            and float(np.percentile(straight_energy, 90))
            <= GHOST_COMPONENT_MAX_STRAIGHT_RGB_P90
        ):
            removed |= component
            removed_components += 1
    if not removed_components:
        return base, matte, removed, 0
    # Include the sub-threshold antialiased fringe surrounding a confirmed
    # ghost.  Limiting this to a tiny dilation, very low Alpha, and pixels
    # outside the protected subject prevents the characteristic hollow color
    # outline without broadening the suppression decision itself.
    fringe_radius = max(3, int(round(min(height, width) * 0.012)))
    fringe = cv2.dilate(
        removed.astype(np.uint8),
        cv2.getStructuringElement(
            cv2.MORPH_ELLIPSE, (fringe_radius * 2 + 1,) * 2
        ),
    ).astype(bool)
    removed |= fringe & (matte <= 0.05) & ~protected
    cleaned_base = base.copy()
    cleaned_alpha = matte.copy()
    cleaned_base[removed] = 0.0
    cleaned_alpha[removed] = 0.0
    return cleaned_base, cleaned_alpha, removed, removed_components


def _evaluate_chroma_candidate(
    candidate: dict[str, Any],
    source_rgb: np.ndarray,
    screen_authority: np.ndarray,
    screen_confidence: float,
    unmix_authority: tuple[np.ndarray, np.ndarray],
) -> bool:
    premultiplied, alpha = read_linear_exr(Path(candidate["mattePath"]))
    if alpha is None:
        raise RuntimeError("Chroma candidate lost authoritative Alpha.")
    confidence = read_confidence_exr(Path(candidate["confidencePath"]))
    raw = np.asarray(source_rgb, dtype=np.float32) - (
        1.0 - alpha[:, :, None]
    ) * np.asarray(screen_authority, dtype=np.float32)
    conflict = np.any(
        (raw < -0.004) | (raw > alpha[:, :, None] + 0.004), axis=2
    )
    unmix_premultiplied, unmix_alpha = unmix_authority
    implied_k = (
        np.asarray(source_rgb, dtype=np.float32) - unmix_premultiplied
    ) / np.maximum(1.0 - unmix_alpha[:, :, None], 1e-4)
    model_foreground_fallback = (
        (unmix_alpha > 0.001)
        & (unmix_alpha < 0.995)
        & np.any((implied_k < -0.004) | (implied_k > 1.004), axis=2)
    )
    conflict[model_foreground_fallback] = False
    visible = alpha > 0.01
    visible_conflict_ratio = float(
        np.count_nonzero(conflict & visible) / max(np.count_nonzero(visible), 1)
    )
    transparent_conflict_ratio = float(np.mean(conflict & ~visible))
    coverage = float(np.mean(alpha > 0.05))
    uncertain_ratio = float(np.mean(confidence < 0.58))
    foreground_fallback_ratio = float(np.mean(model_foreground_fallback))
    passed = bool(
        screen_confidence >= 0.42
        and 0.0005 < coverage < 0.995
        and uncertain_ratio <= 0.30
        and visible_conflict_ratio <= 0.05
        and foreground_fallback_ratio <= 0.12
    )
    candidate["qc"].update(
        {
            "foregroundCoverage": coverage,
            "uncertainRatio": uncertain_ratio,
            "reconstructionConflictRatio": visible_conflict_ratio,
            "transparentReconstructionConflictRatio": transparent_conflict_ratio,
            "candidatePassed": passed,
            "colorAuthority": (
                "original-C-corridorkey-virtual-K-final-alpha-"
                "with-bounded-model-F-fallback"
            ),
            "modelForegroundFallbackRatio": foreground_fallback_ratio,
        }
    )
    return passed


def _shape_checked_source(
    generation_root: Path, record: dict[str, Any]
) -> tuple[Path, np.ndarray]:
    path = _safe_path(generation_root, record.get("sourceExr"), exists=True)
    rgb, source_alpha = read_linear_exr(path)
    expected = (int(record.get("height") or 0), int(record.get("width") or 0))
    if rgb.shape[:2] != expected:
        raise RuntimeError(f"Linear source dimensions changed for {record.get('frameId')}.")
    if source_alpha is not None and float(np.min(source_alpha)) < 0.999:
        raise RuntimeError("Production video routes expect opaque source authority frames.")
    return path, rgb


def _timed_operation(
    kind: str,
    record_interval: Callable[[str, float, float], None],
    operation: Callable[[], Any],
) -> Any:
    started = time.perf_counter()
    try:
        return operation()
    finally:
        record_interval(kind, started, time.perf_counter())


def _timed_source(
    generation_root: Path,
    record: dict[str, Any],
    record_interval: Callable[[str, float, float], None],
) -> tuple[Path, np.ndarray]:
    return _timed_operation(
        "cpuDecode",
        record_interval,
        lambda: _shape_checked_source(generation_root, record),
    )


def _prefetched_sources(
    generation_root: Path,
    frames: list[dict[str, Any]],
    record_interval: Callable[[str, float, float], None],
) -> Iterator[tuple[dict[str, Any], Path, np.ndarray]]:
    """Decode one frame ahead so CPU I/O can overlap the current GPU inference."""

    if not frames:
        return
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="rotoweave-decode") as pool:
        pending = pool.submit(
            _timed_source,
            generation_root,
            frames[0],
            record_interval,
        )
        for index, record in enumerate(frames):
            source_path, rgb = pending.result()
            if index + 1 < len(frames):
                next_record = frames[index + 1]
                pending = pool.submit(
                    _timed_source,
                    generation_root,
                    next_record,
                    record_interval,
                )
            yield record, source_path, rgb


def _sources(
    generation_root: Path,
    frames: list[dict[str, Any]],
    record_interval: Callable[[str, float, float], None],
    *,
    prefetch: bool,
) -> Iterator[tuple[dict[str, Any], Path, np.ndarray]]:
    if prefetch:
        yield from _prefetched_sources(generation_root, frames, record_interval)
        return
    for record in frames:
        source_path, rgb = _timed_source(generation_root, record, record_interval)
        yield record, source_path, rgb


def _write_base_candidate(
    output_root: Path,
    ordinal: int,
    record: dict[str, Any],
    premultiplied: np.ndarray,
    alpha: np.ndarray,
    confidence: np.ndarray,
    flags: np.ndarray,
    *,
    route: str,
    warnings: list[str],
    qc: dict[str, Any],
) -> dict[str, Any]:
    frame_root = output_root / f"{ordinal:06d}"
    frame_root.mkdir(parents=True, exist_ok=False)
    matte_path = frame_root / "matte.exr"
    confidence_path = frame_root / "confidence.exr"
    uncertainty_path = frame_root / "uncertainty.png"
    delivery_path = frame_root / "delivery-base.png"
    compatibility_path = frame_root / "compatibility-rgba.png"
    write_linear_exr(matte_path, premultiplied, alpha)
    write_confidence_exr(confidence_path, confidence)
    write_uncertainty_png(uncertainty_path, flags)
    write_delivery_base_png(delivery_path, premultiplied, alpha)
    write_compatibility_rgba_png(compatibility_path, premultiplied, alpha)
    reasons = []
    if np.any(np.bitwise_and(flags, ALPHA_UNCERTAINTY)):
        reasons.append("alpha")
    if np.any(np.bitwise_and(flags, RGB_UNCERTAINTY)):
        reasons.append("rgb")
    if np.any(np.bitwise_and(flags, TEMPORAL_UNCERTAINTY)):
        reasons.append("temporal")
    return {
        "frameId": str(record.get("frameId") or ""),
        "route": route,
        "blendMode": "premultiplied",
        "mattePath": str(matte_path),
        "emissionPath": None,
        "confidencePath": str(confidence_path),
        "uncertaintyPath": str(uncertainty_path),
        "deliveryBasePath": str(delivery_path),
        "deliveryEmissionPath": None,
        "compatibilityRgbaPath": str(compatibility_path),
        "confidence": float(np.mean(confidence)),
        "uncertaintyReasons": reasons,
        "warnings": sorted(set(warnings)),
        "qc": qc,
    }


def _flag_reasons(flags: np.ndarray) -> list[str]:
    reasons: list[str] = []
    if np.any(np.bitwise_and(flags, ALPHA_UNCERTAINTY)):
        reasons.append("alpha")
    if np.any(np.bitwise_and(flags, RGB_UNCERTAINTY)):
        reasons.append("rgb")
    if np.any(np.bitwise_and(flags, TEMPORAL_UNCERTAINTY)):
        reasons.append("temporal")
    return reasons


def _unknown_roi(alpha: np.ndarray) -> tuple[int, int, int, int] | None:
    unknown = (alpha > 0.02) & (alpha < 0.98)
    ratio = float(np.mean(unknown))
    if ratio < 1e-5 or ratio > 0.35:
        return None
    ys, xs = np.nonzero(unknown)
    if not len(xs):
        return None
    height, width = alpha.shape
    padding = max(32, int(round(min(height, width) * 0.04)))
    x0 = max(0, int(xs.min()) - padding)
    y0 = max(0, int(ys.min()) - padding)
    x1 = min(width, int(xs.max()) + padding + 1)
    y1 = min(height, int(ys.max()) + padding + 1)
    if ((x1 - x0) * (y1 - y0)) / float(width * height) > 0.85:
        return None
    return x0, y0, x1, y1


def _apply_vitmatte_refinements(
    *,
    layout: FrozenModelLayout,
    generation_root: Path,
    frames: list[dict[str, Any]],
    results: list[dict[str, Any]],
    route: str,
    screen_evidence: list[dict[str, Any]] | None,
    enabled: bool,
    check_cancel: Callable[[], None],
    record_call: Callable[..., None],
    record_interval: Callable[[str, float, float], None],
) -> tuple[int, int]:
    if not enabled:
        return 0, 0
    refinable: list[tuple[int, tuple[int, int, int, int]]] = []
    for index, candidate in enumerate(results):
        _, alpha = read_linear_exr(Path(candidate["mattePath"]))
        if alpha is None:
            raise RuntimeError("ViTMatte ROI requires authoritative Alpha.")
        roi = _unknown_roi(alpha)
        if roi is not None:
            refinable.append((index, roi))
        else:
            candidate["qc"]["roiRefined"] = False
            candidate["qc"]["roiSkipReason"] = "no-localized-unknown-band"
    if not refinable:
        return 0, 0
    check_cancel()
    model = load_vitmatte_base(layout)
    attempted = 0
    completed = 0
    try:
        for index, (x0, y0, x1, y1) in refinable:
            check_cancel()
            record = frames[index]
            candidate = results[index]
            _, source_rgb = _timed_source(
                generation_root, record, record_interval
            )
            old_premultiplied, old_alpha = read_linear_exr(Path(candidate["mattePath"]))
            if old_alpha is None:
                raise RuntimeError("ViTMatte ROI lost authoritative Alpha.")
            crop_alpha = old_alpha[y0:y1, x0:x1]
            trimap = np.full(crop_alpha.shape, 0.5, dtype=np.float32)
            trimap[crop_alpha <= 0.02] = 0.0
            trimap[crop_alpha >= 0.98] = 1.0
            crop_srgb = (
                linear_rgb_to_srgb_u8(source_rgb[y0:y1, x0:x1]).astype(np.float32)
                / 255.0
            )
            refined_crop = _timed_operation(
                "gpuInference",
                record_interval,
                lambda: infer_vitmatte_alpha(model, crop_srgb, trimap),
            )
            attempted += 1
            unknown = (crop_alpha > 0.02) & (crop_alpha < 0.98)
            change = np.abs(refined_crop - crop_alpha)
            mean_change = float(np.mean(change[unknown]))
            p95_change = float(np.percentile(change[unknown], 95))
            if route == "chroma_character" and (
                mean_change > 0.04 or p95_change > 0.20
            ):
                candidate["qc"].update(
                    {
                        "roiAttempted": True,
                        "roiRefined": False,
                        "roiRejectedReason": "alpha-change-exceeds-chroma-authority",
                        "roiRect": [x0, y0, x1, y1],
                        "roiUnknownRatio": float(np.mean(unknown)),
                        "roiAlphaChangeMean": mean_change,
                        "roiAlphaChangeP95": p95_change,
                        "roiCoreChangedRatio": 0.0,
                    }
                )
                record_call(
                    str(record.get("frameId") or ""), "vitmatte-base-roi", roi=True
                )
                check_cancel()
                continue
            final_alpha = old_alpha.copy()
            final_crop = final_alpha[y0:y1, x0:x1]
            final_crop[unknown] = refined_crop[unknown]
            final_alpha[y0:y1, x0:x1] = np.clip(final_crop, 0.0, 1.0)
            if route == "chroma_character":
                if screen_evidence is None:
                    raise RuntimeError("Chroma ROI refinement lost screen evidence.")
                authority_path = Path(
                    str(screen_evidence[index].get("screenAuthorityPath") or "")
                )
                screen_authority, authority_alpha = read_linear_exr(authority_path)
                if authority_alpha is not None:
                    raise RuntimeError("Chroma screen authority must be RGB-only EXR.")
                unmix_path = Path(
                    str(screen_evidence[index].get("unmixAuthorityPath") or "")
                )
                unmix_premultiplied, unmix_alpha = read_linear_exr(unmix_path)
                if unmix_alpha is None:
                    raise RuntimeError("CorridorKey unmix authority lost Alpha.")
                premultiplied, final_alpha, _, _ = _reconstruct_chroma(
                    source_rgb,
                    final_alpha,
                    screen_authority,
                    support_hint=old_alpha,
                    unmix_authority=(unmix_premultiplied, unmix_alpha),
                )
            else:
                premultiplied = np.clip(source_rgb, 0.0, 1.0) * final_alpha[:, :, None]
            confidence = read_confidence_exr(Path(candidate["confidencePath"]))
            roi_confidence = np.clip(0.55 + 0.40 * (1.0 - change), 0.0, 1.0)
            confidence_crop = confidence[y0:y1, x0:x1]
            confidence_crop[unknown] = np.maximum(
                confidence_crop[unknown], roi_confidence[unknown]
            )
            confidence[y0:y1, x0:x1] = confidence_crop
            flags = cv2.imread(candidate["uncertaintyPath"], cv2.IMREAD_UNCHANGED)
            if flags is None or flags.dtype != np.uint16 or flags.shape != final_alpha.shape:
                raise RuntimeError("ViTMatte ROI uncertainty authority is invalid.")
            resolved = np.zeros(flags.shape, dtype=bool)
            resolved[y0:y1, x0:x1] = unknown & (roi_confidence >= 0.62)
            flags[resolved] &= np.uint16(0xFFFF ^ int(ALPHA_UNCERTAINTY))
            still_uncertain = np.zeros(flags.shape, dtype=bool)
            still_uncertain[y0:y1, x0:x1] = unknown & (roi_confidence < 0.62)
            flags[still_uncertain] |= ALPHA_UNCERTAINTY
            write_linear_exr(Path(candidate["mattePath"]), premultiplied, final_alpha)
            write_confidence_exr(Path(candidate["confidencePath"]), confidence)
            write_uncertainty_png(Path(candidate["uncertaintyPath"]), flags)
            write_delivery_base_png(
                Path(candidate["deliveryBasePath"]), premultiplied, final_alpha
            )
            write_compatibility_rgba_png(
                Path(candidate["compatibilityRgbaPath"]), premultiplied, final_alpha
            )
            candidate["confidence"] = float(np.mean(confidence))
            candidate["uncertaintyReasons"] = _flag_reasons(flags)
            candidate["qc"].update(
                {
                    "roiRefined": True,
                    "roiAttempted": True,
                    "roiRect": [x0, y0, x1, y1],
                    "roiUnknownRatio": float(np.mean(unknown)),
                    "roiAlphaChangeMean": mean_change,
                    "roiAlphaChangeP95": p95_change,
                    "roiCoreChangedRatio": 0.0,
                }
            )
            record_call(
                str(record.get("frameId") or ""), "vitmatte-base-roi", roi=True
            )
            completed += 1
            check_cancel()
    finally:
        del model
        _unload_cuda()
    return attempted, completed


def _load_context(params: dict[str, Any], expected_route: str) -> tuple[
    FrozenModelLayout, Path, Path, dict[str, Any], list[dict[str, Any]]
]:
    input_path = Path(str(params.get("inputManifest") or "")).resolve(strict=False)
    generation_root = input_path.parent.resolve()
    if not input_path.is_file() or input_path.is_symlink():
        raise RuntimeError("Signed adapter input manifest is unavailable.")
    manifest = _read_json(input_path)
    if manifest.get("schemaVersion") != 1 or manifest.get("route") != expected_route:
        raise RuntimeError("Signed adapter route/input manifest mismatch.")
    frames = manifest.get("frames")
    if not isinstance(frames, list) or not frames or not all(
        isinstance(item, dict) for item in frames
    ):
        raise RuntimeError("Signed adapter input has no frames.")
    output_root = _safe_path(
        generation_root, params.get("outputDirectory"), exists=False
    )
    if output_root.exists():
        raise RuntimeError("Signed adapter output generation already exists.")
    output_root.mkdir(parents=True, exist_ok=False)
    return FrozenModelLayout.from_environment(), generation_root, output_root, manifest, frames


def _process_chroma(
    params: dict[str, Any],
    check_cancel: Callable[[], None],
    record_call: Callable[..., None],
    record_interval: Callable[[str, float, float], None],
) -> dict[str, Any]:
    layout, generation_root, output_root, manifest, frames = _load_context(
        params, "chroma_character"
    )
    profile = str(params.get("profile") or "high").strip().lower()
    memory_mode = str(params.get("memoryMode") or "full").strip().lower()
    if memory_mode not in {"full", "balanced", "constrained", "minimal"}:
        raise RuntimeError("Unsupported CUDA memory mode.")
    prefetch = memory_mode == "full"
    corridor_device = "cpu" if memory_mode in {"constrained", "minimal"} else "cuda"
    isolated_phase_memory: list[dict[str, float]] = []
    evidence: list[dict[str, Any]] = []
    for record in frames:
        check_cancel()
        _, rgb = _timed_source(generation_root, record, record_interval)
        evidence.append(_screen_evidence(rgb))
    green = float(np.median([item["greenConfidence"] for item in evidence]))
    blue = float(np.median([item["blueConfidence"] for item in evidence]))
    screen_color = "green" if green >= blue else "blue"
    screen_confidence = max(green, blue)
    intermediate = output_root / ".alpha-evidence"
    intermediate.mkdir()
    screen_authority_root = output_root / ".screen-authority"
    screen_authority_root.mkdir()
    unmix_authority_root = output_root / ".unmix-authority"
    unmix_authority_root.mkdir()

    main_model_id = "sam3" if profile == "ultra" else "sam2matting-bplus"
    if memory_mode == "minimal":
        started = time.perf_counter()
        isolated_sam = _run_isolated_phase(
            _isolated_sam_phase,
            (profile, str(generation_root), frames, evidence, str(intermediate)),
            check_cancel,
        )
        isolated_phase_memory.append(dict(isolated_sam.get("memory") or {}))
        record_interval("gpuInference", started, time.perf_counter())
    else:
        predictor = load_sam3(layout) if profile == "ultra" else load_sam2matting_bplus(layout)
        try:
            prefetched = _sources(
                generation_root, frames, record_interval, prefetch=prefetch
            )
            for ordinal, ((record, _, rgb), screen_record) in enumerate(
                zip(prefetched, evidence)
            ):
                check_cancel()
                hint = _alpha_hint(rgb, screen_record["screen"])
                alpha = _timed_operation(
                    "gpuInference",
                    record_interval,
                    lambda: (
                        infer_sam3_alpha(predictor, linear_rgb_to_srgb_u8(rgb), hint)
                        if profile == "ultra"
                        else infer_sam2matting_alpha(
                            predictor, linear_rgb_to_srgb_u8(rgb), hint
                        )
                    ),
                )
                np.save(intermediate / f"{ordinal:06d}.npy", alpha.astype(np.float16))
                check_cancel()
        finally:
            del predictor
            _unload_cuda()

    if memory_mode == "full":
        engine = load_corridorkey(layout, screen_color)
    else:
        engine = load_corridorkey(
            layout,
            screen_color,
            device=corridor_device,
            compile_model=False,
        )
    results: list[dict[str, Any]] = []
    try:
        prefetched = _sources(
            generation_root, frames, record_interval, prefetch=prefetch
        )
        for ordinal, ((record, _, rgb), screen_record) in enumerate(
            zip(prefetched, evidence)
        ):
            check_cancel()
            alpha_path = intermediate / f"{ordinal:06d}.npy"
            sam_alpha = np.asarray(np.load(alpha_path), dtype=np.float32)
            alpha_path.unlink(missing_ok=True)
            corridor = _timed_operation(
                "cpuInference" if corridor_device == "cpu" else "gpuInference",
                record_interval,
                lambda: infer_corridorkey(
                    engine,
                    rgb,
                    sam_alpha,
                    screen_color=screen_color,
                ),
            )
            alpha = np.asarray(corridor["alpha"], dtype=np.float32).squeeze()
            if alpha.shape != rgb.shape[:2]:
                raise RuntimeError("CorridorKey Alpha dimensions do not match the source.")
            alpha = np.clip(alpha, 0.0, 1.0)
            local_plate = _fit_local_screen_plate(
                rgb, alpha, screen_record, screen_color
            )
            screen_authority = _corridor_screen_authority(
                rgb,
                alpha,
                corridor["processed"],
                local_plate,
                screen_color,
            )
            authority_path = screen_authority_root / f"{ordinal:06d}.exr"
            write_linear_exr(authority_path, screen_authority)
            screen_record["screenAuthorityPath"] = str(authority_path)
            processed = np.asarray(corridor["processed"], dtype=np.float32)
            unmix_path = unmix_authority_root / f"{ordinal:06d}.exr"
            write_linear_exr(unmix_path, processed[:, :, :3], processed[:, :, 3])
            screen_record["unmixAuthorityPath"] = str(unmix_path)
            (
                premultiplied,
                alpha,
                reconstruction_conflict,
                foreground_fallback,
            ) = _reconstruct_chroma(
                rgb,
                alpha,
                screen_authority,
                unmix_authority=(processed[:, :, :3], processed[:, :, 3]),
            )
            (
                premultiplied,
                alpha,
                ghost_suppressed,
                ghost_component_count,
            ) = _suppress_disconnected_low_energy_ghosts(premultiplied, alpha)
            reconstruction_conflict[ghost_suppressed] = False
            foreground_fallback[ghost_suppressed] = False
            agreement = 1.0 - np.abs(alpha - sam_alpha)
            confidence = np.clip(
                0.70 * agreement + 0.30 * float(screen_confidence), 0.0, 1.0
            ).astype(np.float32)
            flags = np.zeros(alpha.shape, dtype=np.uint16)
            flags[confidence < 0.58] |= ALPHA_UNCERTAINTY
            flags[reconstruction_conflict] |= RGB_UNCERTAINTY
            flags[foreground_fallback] |= RGB_UNCERTAINTY
            coverage = float(np.mean(alpha > 0.05))
            uncertain_ratio = float(np.mean(confidence < 0.58))
            visible = alpha > 0.01
            reconstruction_conflict_ratio = float(
                np.count_nonzero(reconstruction_conflict & visible)
                / max(np.count_nonzero(visible), 1)
            )
            warnings = [] if screen_confidence >= 0.42 else ["unsupported-screen-color"]
            results.append(
                _write_base_candidate(
                    output_root,
                    ordinal,
                    record,
                    premultiplied,
                    alpha,
                    confidence,
                    flags,
                    route="chroma_character",
                    warnings=warnings,
                    qc={
                        "finite": True,
                        "screenColor": screen_color,
                        "screenConfidence": float(screen_confidence),
                        "screenBorderP90Spread": float(screen_record["spread"]),
                        "foregroundCoverage": coverage,
                        "uncertainRatio": uncertain_ratio,
                        "reconstructionConflictRatio": reconstruction_conflict_ratio,
                        "ghostSuppressedPixelRatio": float(np.mean(ghost_suppressed)),
                        "ghostSuppressedComponentCount": ghost_component_count,
                        "candidatePassed": False,
                        "colorAuthority": (
                            "original-C-corridorkey-virtual-K-final-alpha-"
                            "with-bounded-model-F-fallback"
                        ),
                    },
                )
            )
            record_call(
                str(record.get("frameId") or ""),
                f"{main_model_id}+corridorkey-{screen_color}",
                roi=False,
            )
            check_cancel()
    finally:
        del engine
        _unload_cuda()
        try:
            intermediate.rmdir()
        except OSError:
            pass

    roi_enabled = int(params.get("maxRoiRefinements") or 0) >= 1
    if memory_mode == "minimal" and roi_enabled:
        started = time.perf_counter()
        isolated_roi = _run_isolated_phase(
            _isolated_vitmatte_phase,
            (str(generation_root), frames, results, evidence),
            check_cancel,
        )
        isolated_phase_memory.append(dict(isolated_roi.get("memory") or {}))
        record_interval("gpuInference", started, time.perf_counter())
        results = [dict(item) for item in isolated_roi.get("results") or []]
        roi_attempt_count = int(isolated_roi.get("attempted") or 0)
        roi_count = int(isolated_roi.get("completed") or 0)
        for frame_id, model_id, roi in isolated_roi.get("calls") or []:
            record_call(frame_id, model_id, roi=bool(roi))
    else:
        roi_attempt_count, roi_count = _apply_vitmatte_refinements(
            layout=layout,
            generation_root=generation_root,
            frames=frames,
            results=results,
            route="chroma_character",
            screen_evidence=evidence,
            enabled=roi_enabled,
            check_cancel=check_cancel,
            record_call=record_call,
            record_interval=record_interval,
        )

    frame_passes: list[bool] = []
    for index, (record, candidate, screen_record) in enumerate(
        zip(frames, results, evidence)
    ):
        _, source_rgb = _timed_source(generation_root, record, record_interval)
        authority_path = Path(str(screen_record.get("screenAuthorityPath") or ""))
        screen_authority, authority_alpha = read_linear_exr(authority_path)
        if authority_alpha is not None:
            raise RuntimeError("Chroma screen authority must be RGB-only EXR.")
        unmix_path = Path(str(screen_record.get("unmixAuthorityPath") or ""))
        unmix_premultiplied, unmix_alpha = read_linear_exr(unmix_path)
        if unmix_alpha is None:
            raise RuntimeError("CorridorKey unmix authority lost Alpha.")
        frame_passes.append(
            _evaluate_chroma_candidate(
                candidate,
                source_rgb,
                screen_authority,
                screen_confidence,
                (unmix_premultiplied, unmix_alpha),
            )
        )
        authority_path.unlink(missing_ok=True)
        unmix_path.unlink(missing_ok=True)
    try:
        screen_authority_root.rmdir()
    except OSError:
        pass
    try:
        unmix_authority_root.rmdir()
    except OSError:
        pass

    provenance = {
        "sourceColorAuthority": "original-linear-rec709",
        "premultipliedRgbReconstruction": (
            "original-C-corridorkey-virtual-K-final-alpha-"
            "with-bounded-model-F-fallback"
        ),
        "temporalRgbPropagation": False,
        "generativeRepaint": False,
        "algorithm": f"{main_model_id}-corridorkey-v1",
        "requestedProfile": profile,
        "publishedCandidate": "sam3-ultra" if profile == "ultra" else "sam2-bplus-high",
        "mainAlphaModel": main_model_id,
        "runtimeContract": (
            "rotoweave-sam3-alpha-v1" if profile == "ultra" else None
        ),
        "screenColor": screen_color,
        "modelCallsPerFrame": 1,
        "roiRefinementsPerFrame": 1 if roi_attempt_count else 0,
        "roiAttemptedFrameCount": roi_attempt_count,
        "roiRefinedFrameCount": roi_count,
        "inputManifestSha256": hashlib.sha256(
            json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
        "memoryMode": memory_mode,
        "cpuStages": ["corridorkey"] if corridor_device == "cpu" else [],
        "qualityParameters": {
            "corridorImageSize": 2048,
            "corridorRefiner": True,
            "maxRoiRefinements": int(params.get("maxRoiRefinements") or 0),
            "profile": profile,
        },
        "cudaContextIsolation": (
            "subprocess-per-cuda-stage" if memory_mode == "minimal" else "resident-worker"
        ),
        "isolatedPhaseMemory": isolated_phase_memory,
    }
    return {
        "schemaVersion": 1,
        "route": "chroma_character",
        "blendMode": "premultiplied",
        "qcPassed": bool(all(frame_passes)),
        "frames": results,
        "provenance": provenance,
    }


def process_route(
    *,
    route: str,
    params: dict[str, Any],
    check_cancel: Callable[[], None],
    record_call: Callable[..., None],
    record_interval: Callable[[str, float, float], None] | None = None,
) -> dict[str, Any]:
    interval_recorder = record_interval or (lambda _kind, _start, _end: None)
    if str(params.get("profile") or "high").strip().lower() not in {"high", "ultra"}:
        raise RuntimeError("The production adapter supports only High or approved Ultra.")
    if int(params.get("maxRoiRefinements") or 0) > 1:
        raise RuntimeError("The production adapter permits at most one ROI refinement.")
    if route == "chroma_character":
        return _process_chroma(
            params,
            check_cancel,
            record_call,
            interval_recorder,
        )
    raise RuntimeError(f"Unsupported signed-adapter route: {route}.")


__all__ = ["process_route"]
