from __future__ import annotations

import json
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from .product import (
    PRODUCT_VERSION,
    REMOTE_MATTING_API_PREFIX,
    REMOTE_MATTING_API_VERSION,
    ProductContractError,
    require_contract_version,
)


class RemoteProtocolModel(BaseModel):
    model_config = {"extra": "forbid"}


class RemoteQuality(StrEnum):
    HIGH = "high"
    ULTRA = "ultra"


class RemoteInputFrame(RemoteProtocolModel):
    frameId: str = Field(min_length=1, max_length=160)
    ordinal: int = Field(ge=0)
    ptsUs: int = Field(ge=0)
    durationUs: int = Field(gt=0)
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    archivePath: str = Field(pattern=r"^frames/[0-9]{6}\.[A-Za-z0-9]+$")
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class RemoteOutputFrame(RemoteProtocolModel):
    sourceFrameId: str = Field(min_length=1, max_length=160)
    ordinal: int = Field(ge=0)
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    rgbaPath: str = Field(pattern=r"^rgba/[0-9]{6}\.png$")
    rgbaSha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    emissionPath: str | None = Field(
        default=None, pattern=r"^emission/[0-9]{6}\.png$"
    )
    emissionSha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_optional_layer_pair(self) -> "RemoteOutputFrame":
        if (self.emissionPath is None) != (self.emissionSha256 is None):
            raise ValueError("特效层路径与 SHA-256 必须同时提供。")
        return self


class RemoteJobState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RemoteErrorCode(StrEnum):
    INCOMPATIBLE_PROTOCOL = "incompatible_protocol"
    INVALID_REQUEST = "invalid_request"
    UNAUTHORIZED = "unauthorized"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    CANCELLED = "cancelled"
    MODEL_UNAVAILABLE = "model_unavailable"
    GPU_OUT_OF_MEMORY = "gpu_out_of_memory"
    INTEGRITY_FAILED = "integrity_failed"
    INTERNAL_ERROR = "internal_error"


class RemoteJobSubmission(RemoteProtocolModel):
    protocolVersion: Literal[REMOTE_MATTING_API_VERSION]
    materialId: str = Field(min_length=1, max_length=160)
    materialSha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    quality: RemoteQuality
    frameCount: int = Field(ge=1)
    framesManifestSha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    archiveSha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    frames: list[RemoteInputFrame] = Field(min_length=1)
    settings: dict[str, Any]

    @model_validator(mode="after")
    def validate_frames(self) -> "RemoteJobSubmission":
        if self.frameCount != len(self.frames):
            raise ValueError("提交帧数与帧元数据不一致。")
        if [item.ordinal for item in self.frames] != list(range(self.frameCount)):
            raise ValueError("提交帧序号必须从零连续排列。")
        if len({item.frameId for item in self.frames}) != self.frameCount:
            raise ValueError("提交帧身份不能重复。")
        return self


class RemoteError(RemoteProtocolModel):
    protocolVersion: Literal[REMOTE_MATTING_API_VERSION]
    code: RemoteErrorCode
    message: str = Field(min_length=1)
    retryable: bool
    detail: dict[str, Any] | None = None


class RemoteServiceStatus(RemoteProtocolModel):
    protocolVersion: Literal[REMOTE_MATTING_API_VERSION]
    service: Literal["RotoWeave Remote Matting 4.0"]
    ready: bool
    startupState: Literal["starting", "ready", "failed"]
    workerState: str = Field(min_length=1, max_length=80)
    ownership: Literal["short-lived-remote-jobs-only"]


class RemoteJobStatus(RemoteProtocolModel):
    protocolVersion: Literal[REMOTE_MATTING_API_VERSION]
    jobId: str = Field(min_length=1)
    state: RemoteJobState
    progress: float = Field(ge=0, le=1)
    stage: str | None = None
    error: RemoteError | None = None


class RemoteProgressEvent(RemoteProtocolModel):
    protocolVersion: Literal[REMOTE_MATTING_API_VERSION]
    jobId: str = Field(min_length=1)
    sequence: int = Field(ge=0)
    state: RemoteJobState
    progress: float = Field(ge=0, le=1)
    stage: str | None = None
    message: str | None = None


class RemoteResultManifest(RemoteProtocolModel):
    protocolVersion: Literal[REMOTE_MATTING_API_VERSION]
    jobId: str = Field(min_length=1)
    materialId: str = Field(min_length=1)
    materialSha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    quality: RemoteQuality
    frameCount: int = Field(ge=1)
    frameMappingSha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    archiveSha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    frames: list[RemoteOutputFrame] = Field(min_length=1)
    model: dict[str, Any]
    settings: dict[str, Any]

    @model_validator(mode="after")
    def validate_frames(self) -> "RemoteResultManifest":
        if self.frameCount != len(self.frames):
            raise ValueError("结果帧数与帧映射不一致。")
        if [item.ordinal for item in self.frames] != list(range(self.frameCount)):
            raise ValueError("结果帧序号必须从零连续排列。")
        if len({item.sourceFrameId for item in self.frames}) != self.frameCount:
            raise ValueError("结果源帧身份不能重复。")
        return self


@lru_cache(maxsize=1)
def load_protocol_manifest() -> dict[str, Any]:
    path = Path(__file__).resolve().parent / "protocols.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        local = value["localApi"]
        remote = value["remoteMattingApi"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ProductContractError(f"公共协议清单无效：{path}") from exc
    require_contract_version("产品", value.get("productVersion"), PRODUCT_VERSION)
    require_contract_version("本地 API", local.get("version"), 4)
    require_contract_version("远程抠图 API", remote.get("version"), REMOTE_MATTING_API_VERSION)
    require_contract_version("本地 API 前缀", local.get("prefix"), "/api/v4")
    require_contract_version("远程抠图 API 前缀", remote.get("prefix"), REMOTE_MATTING_API_PREFIX)
    if remote.get("transport") != "http" or remote.get("authentication") != {"scheme": "none"}:
        raise ProductContractError("远程抠图协议必须使用可信局域网 HTTP 且不启用客户端认证。")
    return value


PROTOCOLS = load_protocol_manifest()
