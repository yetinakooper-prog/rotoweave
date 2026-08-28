from __future__ import annotations

from types import SimpleNamespace

import pytest

from contracts import hardware


@pytest.mark.parametrize(
    "name",
    [
        "NVIDIA GeForce RTX 3060",
        "NVIDIA GeForce RTX 3070",
        "NVIDIA GeForce RTX 3090",
        "NVIDIA GeForce RTX 4060",
        "NVIDIA GeForce RTX 4090",
        "NVIDIA GeForce RTX 5090",
        "NVIDIA Future Architecture 123",
    ],
)
def test_cuda_probe_accepts_names_without_sku_control_flow(monkeypatch, name: str) -> None:
    monkeypatch.setattr(hardware.shutil, "which", lambda _name: "nvidia-smi.exe")
    monkeypatch.setattr(
        hardware.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout=f"0, GPU-a, {name}, 600.1, 8.9, 12288, 1024\n",
            stderr="",
        ),
    )
    result = hardware.probe_cuda_hardware()
    assert result.available is True
    assert result.selected is not None
    assert result.selected.name == name


def test_cuda_probe_prefers_bound_uuid_then_vram_and_index(monkeypatch) -> None:
    monkeypatch.setattr(hardware.shutil, "which", lambda _name: "nvidia-smi.exe")
    monkeypatch.setattr(
        hardware.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout=(
                "2, GPU-small, NVIDIA A, 600.1, 8.6, 8192, 100\n"
                "1, GPU-large-b, NVIDIA B, 600.1, 12.0, 24576, 200\n"
                "0, GPU-large-a, NVIDIA C, 600.1, 12.0, 24576, 300\n"
            ),
            stderr="",
        ),
    )
    assert hardware.probe_cuda_hardware().selected_uuid == "GPU-large-a"
    assert hardware.probe_cuda_hardware("GPU-small").selected_uuid == "GPU-small"


def test_cuda_probe_missing_tool_is_warning_not_exception(monkeypatch) -> None:
    monkeypatch.setattr(hardware.shutil, "which", lambda _name: None)
    result = hardware.probe_cuda_hardware()
    assert result.available is False
    assert result.warnings[0].code == "gpu_not_detected"


def test_memory_plan_selects_fastest_mode_that_fits_vram_and_ram() -> None:
    receipts = [
        {"mode": "full", "state": "passed", "peakReservedMiB": 10000, "peakWorkingSetMiB": 2000},
        {"mode": "balanced", "state": "passed", "peakReservedMiB": 7000, "peakWorkingSetMiB": 2500},
        {"mode": "constrained", "state": "passed", "peakReservedMiB": 4000, "peakWorkingSetMiB": 6000},
        {"mode": "minimal", "state": "passed", "peakReservedMiB": 3000, "peakWorkingSetMiB": 7000},
    ]
    plan = hardware.memory_mode_plan(
        receipts,
        total_vram_mib=12288,
        free_vram_mib=8000,
        free_ram_mib=16384,
    )
    assert plan["selectedMode"] == "constrained"
    assert plan["headroomMiB"] == 2458


def test_memory_plan_blocks_cpu_modes_when_ram_is_too_low() -> None:
    plan = hardware.memory_mode_plan(
        [{"mode": "constrained", "state": "passed", "peakReservedMiB": 2000, "peakWorkingSetMiB": 8000}],
        total_vram_mib=8192,
        free_vram_mib=7000,
        free_ram_mib=10000,
    )
    assert plan["state"] == "unavailable"
    assert plan["selectedMode"] is None
