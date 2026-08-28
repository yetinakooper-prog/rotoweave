from __future__ import annotations

import json
import threading
from pathlib import Path

import cv2
import numpy as np
import pytest

from backend.app.linear_media import read_linear_exr, write_linear_exr
from worker.cuda_matting import rotoweave_adapter as adapter


def test_source_prefetch_starts_next_decode_before_current_inference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    second_started = threading.Event()
    release_second = threading.Event()
    frames = [{"frameId": "frm_0"}, {"frameId": "frm_1"}]

    def fake_source(
        _root: Path,
        record: dict[str, object],
        _record_interval: object,
    ) -> tuple[Path, np.ndarray]:
        if record["frameId"] == "frm_1":
            second_started.set()
            assert release_second.wait(timeout=2.0)
        return tmp_path / f"{record['frameId']}.exr", np.zeros((2, 2, 3))

    monkeypatch.setattr(adapter, "_timed_source", fake_source)
    sources = adapter._prefetched_sources(tmp_path, frames, lambda *_args: None)
    first_record, _, _ = next(sources)
    assert first_record["frameId"] == "frm_0"
    assert second_started.wait(timeout=2.0), (
        "the next CPU decode must begin while the caller can run current-frame inference"
    )
    release_second.set()
    second_record, _, _ = next(sources)
    assert second_record["frameId"] == "frm_1"


def _generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    route: str,
    rgb: np.ndarray,
) -> tuple[Path, dict[str, object]]:
    monkeypatch.setattr(
        adapter.FrozenModelLayout,
        "from_environment",
        classmethod(lambda _cls: object()),
    )
    generation = tmp_path / "generation"
    inputs = generation / "inputs"
    inputs.mkdir(parents=True)
    source = inputs / "000000.exr"
    write_linear_exr(source, rgb)
    height, width = rgb.shape[:2]
    manifest = {
        "schemaVersion": 1,
        "route": route,
        "sourceSha256": "a" * 64,
        "constraintsHash": "b" * 64,
        "frames": [
            {
                "frameId": "frm_0",
                "frameIndex": 0,
                "sourceExr": str(source),
                "sourceSha256": "c" * 64,
                "width": width,
                "height": height,
                "timeUs": 0,
            }
        ],
    }
    manifest_path = generation / "worker-input.manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return generation, manifest


def test_chroma_adapter_returns_complete_failed_qc_candidate_for_non_chroma_screen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    height = width = 64
    alpha = np.zeros((height, width), dtype=np.float32)
    alpha[16:48, 20:44] = 1.0
    rgb = np.full((height, width, 3), 0.25, dtype=np.float32)
    rgb[alpha > 0.5] = (0.7, 0.1, 0.4)
    generation, _ = _generation(
        tmp_path, monkeypatch, route="chroma_character", rgb=rgb
    )
    monkeypatch.setattr(adapter, "load_sam2matting_bplus", lambda _layout: object())
    monkeypatch.setattr(
        adapter,
        "infer_sam2matting_alpha",
        lambda _model, _image, _hint: alpha.copy(),
    )
    monkeypatch.setattr(adapter, "load_corridorkey", lambda _layout, _screen: object())
    monkeypatch.setattr(
        adapter,
        "infer_corridorkey",
        lambda _model, image, hint, **_kwargs: {
            "alpha": hint.copy(),
            "fg": image.copy(),
            "processed": np.dstack((image * hint[:, :, None], hint)),
        },
    )
    monkeypatch.setattr(adapter, "_unload_cuda", lambda *_args: None)
    calls: list[tuple[str, str, bool]] = []
    intervals: list[tuple[str, float, float]] = []
    result = adapter.process_route(
        route="chroma_character",
        params={
            "profile": "high",
            "inputManifest": str(generation / "worker-input.manifest.json"),
            "outputDirectory": str(generation / "candidate"),
            "maxRoiRefinements": 1,
        },
        check_cancel=lambda: None,
        record_call=lambda frame_id, model_id, roi=False: calls.append(
            (frame_id, model_id, roi)
        ),
        record_interval=lambda kind, started, ended: intervals.append(
            (kind, started, ended)
        ),
    )
    assert result["qcPassed"] is False
    assert result["frames"][0]["warnings"] == ["unsupported-screen-color"]
    assert Path(result["frames"][0]["mattePath"]).is_file()
    assert len(calls) == 1 and calls[0][0] == "frm_0" and calls[0][2] is False
    assert {item[0] for item in intervals} == {"cpuDecode", "gpuInference"}
    assert all(item[2] > item[1] for item in intervals)
    assert result["provenance"]["premultipliedRgbReconstruction"] == (
        "original-C-corridorkey-virtual-K-final-alpha-with-bounded-model-F-fallback"
    )


def test_ultra_chroma_uses_sam3_as_its_only_main_alpha(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ROTOWEAVE_SAM3_LOCAL_ROOT", raising=False)
    rgb = np.zeros((64, 64, 3), dtype=np.float32)
    rgb[:, :, 1] = 0.8
    alpha = np.zeros((64, 64), dtype=np.float32)
    alpha[16:48, 20:44] = 1.0
    generation, _ = _generation(
        tmp_path, monkeypatch, route="chroma_character", rgb=rgb
    )
    monkeypatch.setattr(adapter, "load_sam3", lambda _layout: object())
    monkeypatch.setattr(adapter, "load_sam2matting_bplus", lambda _layout: pytest.fail("Ultra called SAM2"))
    monkeypatch.setattr(adapter, "infer_sam3_alpha", lambda _model, _image, _hint: alpha.copy())
    monkeypatch.setattr(adapter, "load_corridorkey", lambda _layout, _screen: object())
    monkeypatch.setattr(
        adapter,
        "infer_corridorkey",
        lambda _model, image, hint, **_kwargs: {
            "alpha": hint.copy(),
            "fg": image.copy(),
            "processed": np.dstack((image * hint[:, :, None], hint)),
        },
    )
    monkeypatch.setattr(adapter, "_unload_cuda", lambda *_args: None)
    calls: list[tuple[str, str, bool]] = []
    result = adapter.process_route(
        route="chroma_character",
        params={
            "profile": "ultra",
            "inputManifest": str(generation / "worker-input.manifest.json"),
            "outputDirectory": str(generation / "candidate"),
            "maxRoiRefinements": 0,
        },
        check_cancel=lambda: None,
        record_call=lambda frame_id, model_id, roi=False: calls.append((frame_id, model_id, roi)),
    )
    assert calls == [("frm_0", "sam3+corridorkey-green", False)]
    assert result["provenance"]["requestedProfile"] == "ultra"
    assert result["provenance"]["publishedCandidate"] == "sam3-ultra"
    assert result["provenance"]["runtimeContract"] == "rotoweave-sam3-alpha-v1"


def test_legacy_local_ultra_environment_cannot_change_frozen_candidate_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rgb = np.zeros((32, 32, 3), dtype=np.float32)
    rgb[:, :, 1] = 0.8
    alpha = np.zeros((32, 32), dtype=np.float32)
    alpha[8:24, 10:22] = 1.0
    generation, _ = _generation(
        tmp_path, monkeypatch, route="chroma_character", rgb=rgb
    )
    monkeypatch.setenv("ROTOWEAVE_SAM3_LOCAL_ROOT", str(tmp_path / "local-ultra"))
    monkeypatch.setattr(adapter, "load_sam3", lambda _layout: object())
    monkeypatch.setattr(adapter, "infer_sam3_alpha", lambda *_args: alpha.copy())
    monkeypatch.setattr(adapter, "load_corridorkey", lambda *_args: object())
    monkeypatch.setattr(
        adapter,
        "infer_corridorkey",
        lambda _model, image, hint, **_kwargs: {
            "alpha": hint.copy(),
            "fg": image.copy(),
            "processed": np.dstack((image * hint[:, :, None], hint)),
        },
    )
    monkeypatch.setattr(adapter, "_unload_cuda", lambda *_args: None)

    result = adapter.process_route(
        route="chroma_character",
        params={
            "profile": "ultra",
            "inputManifest": str(generation / "worker-input.manifest.json"),
            "outputDirectory": str(generation / "candidate"),
            "maxRoiRefinements": 0,
        },
        check_cancel=lambda: None,
        record_call=lambda *_args, **_kwargs: None,
    )

    assert result["provenance"]["publishedCandidate"] == "sam3-ultra"


def test_chroma_adapter_uses_corridorkey_local_screen_authority_for_soft_effect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    height = width = 96
    yy, xx = np.mgrid[:height, :width]
    local_screen = np.zeros((height, width, 3), dtype=np.float32)
    local_screen[:, :, 1] = 0.36 + 0.10 * (xx / max(width - 1, 1))
    subject_alpha = (((xx - 42) ** 2 + (yy - 50) ** 2) <= 20**2).astype(
        np.float32
    )
    effect_alpha = np.clip(1.0 - np.sqrt((xx - 70) ** 2 + (yy - 46) ** 2) / 22, 0, 1)
    alpha = np.maximum(subject_alpha, effect_alpha * 0.55).astype(np.float32)
    foreground = np.zeros_like(local_screen)
    foreground[subject_alpha > 0.5] = (0.28, 0.04, 0.42)
    foreground[effect_alpha > 0] = np.maximum(
        foreground[effect_alpha > 0], np.array((0.72, 0.92, 1.0), dtype=np.float32)
    )
    premultiplied = foreground * alpha[:, :, None]
    rgb = premultiplied + (1.0 - alpha[:, :, None]) * local_screen
    generation, _ = _generation(
        tmp_path, monkeypatch, route="chroma_character", rgb=rgb
    )
    monkeypatch.setattr(adapter, "load_sam2matting_bplus", lambda _layout: object())
    monkeypatch.setattr(
        adapter,
        "infer_sam2matting_alpha",
        lambda _model, _image, _hint: alpha.copy(),
    )
    monkeypatch.setattr(adapter, "load_corridorkey", lambda _layout, _screen: object())
    monkeypatch.setattr(
        adapter,
        "infer_corridorkey",
        lambda _model, _image, _hint, **_kwargs: {
            "alpha": alpha.copy(),
            "fg": foreground.copy(),
            "processed": np.dstack((premultiplied, alpha)),
        },
    )
    monkeypatch.setattr(adapter, "_unload_cuda", lambda *_args: None)
    result = adapter.process_route(
        route="chroma_character",
        params={
            "profile": "high",
            "inputManifest": str(generation / "worker-input.manifest.json"),
            "outputDirectory": str(generation / "candidate"),
            "maxRoiRefinements": 0,
        },
        check_cancel=lambda: None,
        record_call=lambda *_args, **_kwargs: None,
    )
    assert result["qcPassed"] is True
    frame = result["frames"][0]
    recovered, recovered_alpha = read_linear_exr(Path(frame["mattePath"]))
    assert recovered_alpha is not None
    assert frame["qc"]["reconstructionConflictRatio"] <= 0.01
    assert frame["qc"]["transparentReconstructionConflictRatio"] >= 0.0
    visible = alpha > 0.05
    assert float(np.mean(np.abs(recovered[visible] - premultiplied[visible]))) < 0.02
    assert float(np.mean(np.abs(recovered_alpha - alpha))) < 0.01


def test_ghost_suppression_removes_dim_detached_pose_but_keeps_bright_vfx() -> None:
    height = width = 256
    yy, xx = np.mgrid[:height, :width]
    alpha = np.zeros((height, width), dtype=np.float32)
    subject = ((xx - 112) ** 2 / 42**2 + (yy - 145) ** 2 / 68**2) <= 1
    ghost = ((xx - 205) ** 2 / 30**2 + (yy - 145) ** 2 / 52**2) <= 1
    glow = ((xx - 35) ** 2 + (yy - 112) ** 2) <= 27**2
    alpha[subject] = 1.0
    alpha[ghost] = 0.08
    alpha[glow] = 0.14
    straight = np.zeros((height, width, 3), dtype=np.float32)
    straight[subject] = (0.30, 0.08, 0.45)
    straight[ghost] = (0.20, 0.24, 0.28)
    straight[glow] = (0.82, 0.94, 1.0)
    fringe_radius = max(3, int(round(min(height, width) * 0.012)))
    ghost_fringe = cv2.dilate(
        ghost.astype(np.uint8),
        cv2.getStructuringElement(
            cv2.MORPH_ELLIPSE, (fringe_radius * 2 + 1,) * 2
        ),
    ).astype(bool) & ~ghost
    alpha[ghost_fringe] = 0.008
    straight[ghost_fringe] = (0.18, 0.22, 0.26)
    premultiplied = straight * alpha[:, :, None]

    cleaned_rgb, cleaned_alpha, removed, component_count = (
        adapter._suppress_disconnected_low_energy_ghosts(premultiplied, alpha)
    )

    assert component_count == 1
    assert float(cleaned_alpha[ghost].max()) == 0.0
    assert float(cleaned_rgb[ghost].max()) == 0.0
    assert float(cleaned_alpha[ghost_fringe].max()) == 0.0
    assert np.array_equal(cleaned_alpha[glow], alpha[glow])
    assert np.array_equal(cleaned_rgb[glow], premultiplied[glow])
    assert not np.any(removed[subject | glow])


def test_ghost_suppression_preserves_subject_soft_edge() -> None:
    height = width = 192
    yy, xx = np.mgrid[:height, :width]
    distance = np.sqrt((xx - 96) ** 2 + (yy - 96) ** 2)
    alpha = np.clip((72.0 - distance) / 12.0, 0.0, 1.0).astype(np.float32)
    straight = np.full((height, width, 3), (0.16, 0.10, 0.24), dtype=np.float32)
    premultiplied = straight * alpha[:, :, None]

    cleaned_rgb, cleaned_alpha, removed, component_count = (
        adapter._suppress_disconnected_low_energy_ghosts(premultiplied, alpha)
    )

    assert component_count == 0
    assert not np.any(removed)
    assert np.array_equal(cleaned_alpha, alpha)
    assert np.array_equal(cleaned_rgb, premultiplied)


def test_adapter_rejects_output_path_outside_private_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rgb = np.zeros((16, 16, 3), dtype=np.float32)
    generation, _ = _generation(
        tmp_path, monkeypatch, route="chroma_character", rgb=rgb
    )
    with pytest.raises(RuntimeError, match="escapes its generation"):
        adapter.process_route(
            route="chroma_character",
            params={
                "profile": "high",
                "inputManifest": str(generation / "worker-input.manifest.json"),
                "outputDirectory": str(tmp_path / "outside"),
                "maxRoiRefinements": 1,
            },
            check_cancel=lambda: None,
            record_call=lambda *_args, **_kwargs: None,
        )
