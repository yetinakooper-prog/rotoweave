from __future__ import annotations

import math
import shutil
import subprocess
from dataclasses import dataclass
from typing import Any, Iterable, Mapping


EXECUTION_MODES = ("full", "balanced", "constrained", "minimal")


@dataclass(frozen=True, slots=True)
class HardwareWarning:
    code: str
    message: str
    action: str
    scope: str = "host"
    profile: str | None = None

    def as_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "code": self.code,
            "severity": "warning",
            "scope": self.scope,
            "message": self.message,
            "action": self.action,
        }
        if self.profile:
            value["profile"] = self.profile
        return value


@dataclass(frozen=True, slots=True)
class CudaDevice:
    index: int
    uuid: str
    name: str
    driver_version: str
    compute_capability: str | None
    vram_total_mib: int
    vram_used_mib: int

    @property
    def vram_free_mib(self) -> int:
        return max(0, self.vram_total_mib - self.vram_used_mib)

    def as_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "uuid": self.uuid,
            "gpuName": self.name,
            "driverVersion": self.driver_version,
            "computeCapability": self.compute_capability,
            "vramTotalMiB": self.vram_total_mib,
            "vramUsedMiB": self.vram_used_mib,
            "vramFreeMiB": self.vram_free_mib,
        }


@dataclass(frozen=True, slots=True)
class HardwareProbe:
    devices: tuple[CudaDevice, ...]
    selected_uuid: str | None
    warnings: tuple[HardwareWarning, ...] = ()
    reason: str | None = None

    @property
    def available(self) -> bool:
        return bool(self.devices)

    @property
    def selected(self) -> CudaDevice | None:
        if self.selected_uuid:
            match = next(
                (item for item in self.devices if item.uuid == self.selected_uuid), None
            )
            if match is not None:
                return match
        return self.devices[0] if self.devices else None

    def as_dict(self) -> dict[str, Any]:
        selected = self.selected
        return {
            "available": self.available,
            "compatibilityState": "detected" if selected else "unavailable",
            "selectedDevice": selected.as_dict() if selected else None,
            "devices": [item.as_dict() for item in self.devices],
            "warnings": [item.as_dict() for item in self.warnings],
            "reason": self.reason,
            "gpuName": selected.name if selected else None,
            "gpuUuid": selected.uuid if selected else None,
            "driverVersion": selected.driver_version if selected else None,
            "computeCapability": selected.compute_capability if selected else None,
            "vramTotalMiB": selected.vram_total_mib if selected else None,
            "vramUsedMiB": selected.vram_used_mib if selected else None,
            "vramFreeMiB": selected.vram_free_mib if selected else None,
        }


def _unavailable(code: str, message: str, action: str, reason: str) -> HardwareProbe:
    return HardwareProbe(
        (),
        None,
        (HardwareWarning(code=code, message=message, action=action),),
        reason,
    )


def _parse_int(value: str) -> int:
    return int(float(value.strip()))


def probe_cuda_hardware(preferred_uuid: str | None = None) -> HardwareProbe:
    """Enumerate NVIDIA devices without imposing a SKU or VRAM allow-list."""

    executable = shutil.which("nvidia-smi") or shutil.which("nvidia-smi.exe")
    if not executable:
        return _unavailable(
            "gpu_not_detected",
            "未检测到 NVIDIA 设备工具；服务仍可安装和启动。",
            "安装兼容 NVIDIA 驱动后重新执行 Profile 自检。",
            "nvidia-smi-not-found",
        )
    try:
        completed = subprocess.run(
            [
                executable,
                "--query-gpu=index,uuid,name,driver_version,compute_cap,memory.total,memory.used",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return _unavailable(
            "nvidia_smi_failed",
            "NVIDIA 设备查询失败；服务仍可安装和启动。",
            "检查驱动后重新执行 Profile 自检。",
            str(exc),
        )
    if completed.returncode != 0:
        reason = completed.stderr.strip() or f"nvidia-smi-exit-{completed.returncode}"
        return _unavailable(
            "nvidia_smi_failed",
            "NVIDIA 设备查询失败；服务仍可安装和启动。",
            "检查驱动后重新执行 Profile 自检。",
            reason,
        )
    devices: list[CudaDevice] = []
    invalid_lines: list[str] = []
    for raw in completed.stdout.splitlines():
        if not raw.strip():
            continue
        values = [value.strip() for value in raw.split(",")]
        if len(values) != 7:
            invalid_lines.append(raw)
            continue
        try:
            devices.append(
                CudaDevice(
                    index=_parse_int(values[0]),
                    uuid=values[1],
                    name=values[2],
                    driver_version=values[3],
                    compute_capability=(
                        values[4]
                        if values[4] and values[4].casefold() not in {"n/a", "[not supported]"}
                        else None
                    ),
                    vram_total_mib=_parse_int(values[5]),
                    vram_used_mib=_parse_int(values[6]),
                )
            )
        except ValueError:
            invalid_lines.append(raw)
    if not devices:
        return _unavailable(
            "gpu_not_detected",
            "未枚举到可识别的 NVIDIA GPU；服务仍可安装和启动。",
            "检查驱动和设备状态后重新执行 Profile 自检。",
            "invalid-nvidia-smi-output" if invalid_lines else "no-nvidia-device",
        )
    ranked = tuple(sorted(devices, key=lambda item: (-item.vram_total_mib, item.index)))
    selected_uuid = (
        preferred_uuid
        if preferred_uuid and any(item.uuid == preferred_uuid for item in ranked)
        else ranked[0].uuid
    )
    warnings: list[HardwareWarning] = []
    if invalid_lines:
        warnings.append(
            HardwareWarning(
                code="nvidia_smi_partial",
                message="部分 NVIDIA 设备记录无法解析，已忽略异常记录。",
                action="检查驱动状态；可用设备仍将通过运行时自检确认。",
            )
        )
    return HardwareProbe(ranked, selected_uuid, tuple(warnings))


def vram_headroom_mib(total_vram_mib: int) -> int:
    return max(2048, min(6144, math.ceil(max(0, total_vram_mib) * 0.20)))


def memory_mode_plan(
    mode_receipts: Iterable[Mapping[str, Any]],
    *,
    total_vram_mib: int,
    free_vram_mib: int,
    free_ram_mib: int,
) -> dict[str, Any]:
    """Select the fastest measured mode that fits current VRAM and RAM."""

    by_mode = {
        str(item.get("mode") or ""): item
        for item in mode_receipts
        if str(item.get("state") or "") == "passed"
    }
    headroom = vram_headroom_mib(total_vram_mib)
    candidates: list[dict[str, Any]] = []
    for mode in EXECUTION_MODES:
        receipt = by_mode.get(mode)
        if receipt is None:
            continue
        peak_vram = int(
            max(
                float(receipt.get("peakReservedMiB") or 0),
                float(receipt.get("peakAllocatedMiB") or 0),
            )
        )
        peak_ram = int(float(receipt.get("peakWorkingSetMiB") or 0))
        required_vram = peak_vram + headroom
        required_ram = (
            math.ceil(peak_ram * 1.25) + 1024
            if mode in {"constrained", "minimal"}
            else 0
        )
        candidate = {
            "mode": mode,
            "requiredVramMiB": required_vram,
            "requiredRamMiB": required_ram,
            "peakReservedMiB": peak_vram,
            "peakWorkingSetMiB": peak_ram,
            "fitsVram": free_vram_mib >= required_vram,
            "fitsRam": free_ram_mib >= required_ram,
        }
        candidates.append(candidate)
        if candidate["fitsVram"] and candidate["fitsRam"]:
            return {
                "state": "ready",
                "selectedMode": mode,
                "headroomMiB": headroom,
                "freeVramMiB": free_vram_mib,
                "freeRamMiB": free_ram_mib,
                "candidates": candidates,
            }
    return {
        "state": "unavailable",
        "selectedMode": None,
        "headroomMiB": headroom,
        "freeVramMiB": free_vram_mib,
        "freeRamMiB": free_ram_mib,
        "candidates": candidates,
    }
