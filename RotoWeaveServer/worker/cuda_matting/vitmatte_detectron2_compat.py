from __future__ import annotations

import importlib.util
import sys
import types
from dataclasses import dataclass
from typing import Any, Callable


def install_detectron2_compat() -> bool:
    """Install the minimal inference-only Detectron2 surface ViTMatte-B uses.

    Detectron2 does not publish a supported Windows wheel.  The frozen ViTMatte
    revision only needs a small set of tensor helpers for inference, so the
    production pack supplies those helpers instead of building the complete
    training framework.  Return ``True`` when the compatibility modules were
    installed and ``False`` when a real Detectron2 installation already exists.
    """

    if "detectron2" in sys.modules:
        return True
    if importlib.util.find_spec("detectron2") is not None:
        return False

    import torch
    import torch.nn as nn
    import torch.nn.functional as functional

    @dataclass
    class ShapeSpec:
        channels: int | None = None
        height: int | None = None
        width: int | None = None
        stride: int | None = None

    class CNNBlockBase(nn.Module):
        def __init__(self, in_channels: int, out_channels: int, stride: int) -> None:
            super().__init__()
            self.in_channels = in_channels
            self.out_channels = out_channels
            self.stride = stride

    class Conv2d(nn.Conv2d):
        def __init__(
            self,
            *args: Any,
            norm: nn.Module | None = None,
            activation: Callable[[torch.Tensor], torch.Tensor] | None = None,
            **kwargs: Any,
        ) -> None:
            super().__init__(*args, **kwargs)
            self.norm = norm
            self.activation = activation

        def forward(self, value: torch.Tensor) -> torch.Tensor:
            value = super().forward(value)
            if self.norm is not None:
                value = self.norm(value)
            if self.activation is not None:
                value = self.activation(value)
            return value

    class LayerNorm2d(nn.Module):
        def __init__(self, channels: int, eps: float = 1e-6) -> None:
            super().__init__()
            self.weight = nn.Parameter(torch.ones(channels))
            self.bias = nn.Parameter(torch.zeros(channels))
            self.eps = eps

        def forward(self, value: torch.Tensor) -> torch.Tensor:
            value = value.permute(0, 2, 3, 1)
            value = functional.layer_norm(
                value, (value.shape[-1],), self.weight, self.bias, self.eps
            )
            return value.permute(0, 3, 1, 2)

    def get_norm(spec: str | Callable[[int], nn.Module] | None, channels: int) -> nn.Module | None:
        if spec in {None, ""}:
            return None
        if callable(spec):
            return spec(channels)
        factories: dict[str, Callable[[], nn.Module]] = {
            "BN": lambda: nn.BatchNorm2d(channels),
            "SyncBN": lambda: nn.BatchNorm2d(channels),
            "GN": lambda: nn.GroupNorm(32, channels),
            "LN": lambda: LayerNorm2d(channels),
        }
        try:
            return factories[str(spec)]()
        except KeyError as exc:
            raise ValueError(f"Unsupported inference normalization: {spec}") from exc

    def _assert_strides_are_log2_contiguous(strides: list[int]) -> None:
        for previous, current in zip(strides, strides[1:]):
            if current != previous * 2:
                raise AssertionError(f"Strides are not log2 contiguous: {strides}")

    class ImageList:
        def __init__(self, tensor: torch.Tensor, image_sizes: list[tuple[int, int]]) -> None:
            self.tensor = tensor
            self.image_sizes = image_sizes

        @staticmethod
        def from_tensors(
            tensors: list[torch.Tensor], size_divisibility: int = 0
        ) -> "ImageList":
            if not tensors:
                raise ValueError("ImageList requires at least one tensor.")
            sizes = [(int(item.shape[-2]), int(item.shape[-1])) for item in tensors]
            max_height = max(height for height, _ in sizes)
            max_width = max(width for _, width in sizes)
            if size_divisibility > 1:
                max_height = (
                    (max_height + size_divisibility - 1) // size_divisibility
                ) * size_divisibility
                max_width = (
                    (max_width + size_divisibility - 1) // size_divisibility
                ) * size_divisibility
            padded = [
                functional.pad(
                    item,
                    (0, max_width - item.shape[-1], 0, max_height - item.shape[-2]),
                )
                for item in tensors
            ]
            return ImageList(torch.stack(padded), sizes)

    root = types.ModuleType("detectron2")
    root.__path__ = []  # type: ignore[attr-defined]
    layers = types.ModuleType("detectron2.layers")
    layers.ShapeSpec = ShapeSpec
    layers.CNNBlockBase = CNNBlockBase
    layers.Conv2d = Conv2d
    layers.get_norm = get_norm
    modeling = types.ModuleType("detectron2.modeling")
    modeling.__path__ = []  # type: ignore[attr-defined]
    backbone = types.ModuleType("detectron2.modeling.backbone")
    backbone.__path__ = []  # type: ignore[attr-defined]
    fpn = types.ModuleType("detectron2.modeling.backbone.fpn")
    fpn._assert_strides_are_log2_contiguous = _assert_strides_are_log2_contiguous
    structures = types.ModuleType("detectron2.structures")
    structures.ImageList = ImageList

    root.layers = layers
    root.modeling = modeling
    root.structures = structures
    modeling.backbone = backbone
    backbone.fpn = fpn
    sys.modules.update(
        {
            "detectron2": root,
            "detectron2.layers": layers,
            "detectron2.modeling": modeling,
            "detectron2.modeling.backbone": backbone,
            "detectron2.modeling.backbone.fpn": fpn,
            "detectron2.structures": structures,
        }
    )
    return True


__all__ = ["install_detectron2_compat"]
