from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class CurrentModel(BaseModel):
    """Reject fields outside the single current public contract."""

    model_config = {"extra": "forbid"}


class ScreenSample(CurrentModel):
    rgb: tuple[int, int, int]
    color_space: Literal["srgb"] = "srgb"
    x: float | None = Field(default=None, ge=0, le=1)
    y: float | None = Field(default=None, ge=0, le=1)
    source_timeline_ordinal: int | None = Field(default=None, ge=0)

    @field_validator("rgb")
    @classmethod
    def valid_rgb(cls, value: tuple[int, int, int]) -> tuple[int, int, int]:
        if any(channel < 0 or channel > 255 for channel in value):
            raise ValueError("幕色采样必须是 0-255 的 RGB。")
        return value


class ChromaSettings(CurrentModel):
    screen_samples: list[ScreenSample] = Field(default_factory=list, max_length=16)
    threshold_low: float = Field(default=18.0, ge=0, le=255)
    threshold_high: float = Field(default=62.0, ge=1, le=255)
    feather: int = Field(default=3, ge=0, le=31)
    cleanup_radius: int = Field(default=2, ge=0, le=15)
    spill_strength: float = Field(default=0.72, ge=0, le=1)
    key_mode: Literal["clean_screen", "preserve_subject_screen_color"] = "clean_screen"

    @field_validator("threshold_high")
    @classmethod
    def high_must_exceed_zero(cls, value: float) -> float:
        return max(1.0, value)

    @model_validator(mode="after")
    def normalize_threshold_order(self) -> "ChromaSettings":
        if self.threshold_high <= self.threshold_low:
            self.threshold_high = min(255.0, self.threshold_low + 1.0)
        return self


class BasicMaterialSettings(CurrentModel):
    quality: Literal["basic"] = "basic"
    material_type: Literal["character", "effect"]
    chroma: ChromaSettings = Field(default_factory=ChromaSettings)
    ai_assist: bool = True


class SizeProfileCreate(CurrentModel):
    name: str = Field(min_length=1, max_length=80)
    width_world: float = Field(gt=0, le=1000)
    height_world: float = Field(gt=0, le=1000)
    unit_mode: Literal["pixels", "unity"] = "unity"


class SizeProfileUpdate(CurrentModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    width_world: float | None = Field(default=None, gt=0, le=1000)
    height_world: float | None = Field(default=None, gt=0, le=1000)
    unit_mode: Literal["pixels", "unity"] | None = None
