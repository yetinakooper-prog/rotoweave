from __future__ import annotations

import re
from typing import Any


class QualityReviewRequired(RuntimeError):
    """A complete candidate generation exists but must not be published."""

    def __init__(self, message: str, *, review: dict[str, Any]):
        super().__init__(message)
        self.review = review


def build_job_failure(
    error: BaseException | str,
    *,
    job_type: str = "",
    failed_stage: str = "failed",
) -> dict[str, Any]:
    """Turn a current worker failure into actionable UI data."""

    details = str(error).strip() or "未知错误"
    lowered = details.casefold()
    failure: dict[str, Any] = {
        "code": "internal_error",
        "title": "处理未完成",
        "message": "后台处理遇到内部错误，当前已有产物不会被删除。",
        "guidance": "打开技术详情复制诊断信息；若问题持续，请保留任务记录。",
        "failedStage": failed_stage,
        "retryable": False,
        "targetStep": (
            "atlas"
            if job_type in {"character_atlas", "character_export"}
            else "matte"
            if job_type in {"matte", "material_basic", "material_remote", "photoshop_sheet_export", "photoshop_sheet_import"}
            else None
        ),
        "details": details,
    }

    if isinstance(error, QualityReviewRequired):
        failure.update(
            code="quality_review_required",
            title="候选结果需要人工复核",
            message="主候选与独立回退候选均未通过自动质量门禁；旧成果保持不变。",
            guidance="查看保留的候选结果与逐帧 QC，调整素材路线或约束后重新运行。",
            targetStep="matte",
            retryable=False,
            review=error.review,
        )
        return failure

    if "超过图集" in details or ("frame" in lowered and "atlas" in lowered and "exceed" in lowered):
        failure.update(
            code="frame_too_large",
            title="单帧超过图集上限",
            message="至少一帧在加入边距后大于当前图集上限。",
            guidance="打开图集步骤，降低输出比例或把图集上限提高到 4096，然后重新生成。",
            targetStep="atlas",
        )
    elif "未生成任何有效帧" in details or "原始帧无法读取" in details:
        failure.update(
            code="source_frames_unreadable",
            title="原始帧无法读取",
            message="抠图没有读到任何有效原始帧，因此未生成蒙版。",
            guidance="打开抠图步骤，确认原始帧文件仍存在且可读取；必要时重新导入或重新分帧。",
            targetStep="matte",
        )
    elif "尚未生成完整的对齐帧" in details or "对齐帧" in details and "缺" in details:
        failure.update(
            code="aligned_frames_missing",
            title="缺少对齐帧",
            message="部分帧还没有完成锚点对齐，无法继续装箱。",
            guidance="打开动画校准步骤检查角色全局 Pivot 与逐帧纹理位移，再生成图集。",
            targetStep="anchor",
        )
    elif "正式帧与当前帧取舍不一致" in details or "正式帧数量已变化" in details:
        failure.update(
            code="timeline_selection_stale",
            title="正式帧与当前选择不一致",
            message="范围、帧率或逐帧取舍已变化，现有正式帧不能继续使用。",
            guidance="打开范围步骤，确认当前保留帧并重新提取原始帧。",
            targetStep="range",
        )
    elif "Photoshop 拼图超过安全上限" in details:
        failure.update(
            code="photoshop_sheet_too_large",
            title="Photoshop 拼图超过安全上限",
            message="当前保留帧无法在安全内存和单边尺寸内生成一张原尺寸拼图。",
            guidance="打开范围步骤，排除更多候选帧，再重新提取和生成拼图。",
            targetStep="range",
        )
    elif any(token in details for token in ("导回 PNG 尺寸不正确", "缺少 Alpha", "全透明", "完全不透明", "拼图已失效", "拼图已因")):
        failure.update(
            code="photoshop_sheet_invalid",
            title="Photoshop PNG 无法导回",
            message="导回文件未满足当前拼图的尺寸、透明通道或帧内容要求。",
            guidance="打开抠图步骤查看具体帧号；保持原画布尺寸，以带 Alpha 的 PNG 重新导出后再导回。",
            targetStep="matte",
        )
    elif "pivot_error" in lowered or "导出自校验失败" in details:
        failure.update(
            code="export_validation_failed",
            title="导出内部一致性校验失败",
            message="图集、裁剪区域或 Pivot 数据未能通过一致性校验。",
            guidance="打开图集步骤重新生成；若仍失败，请复制技术详情。",
            targetStep="atlas",
        )
    elif any(
        token in lowered
        for token in (
            "gpu_out_of_memory",
            "cuda out of memory",
            "cuda_error_out_of_memory",
            "显存不足",
        )
    ):
        failure.update(
            code="gpu_out_of_memory",
            title="本地显存不足",
            message="本地 Basic 处理没有足够显存完成当前风险帧，未发布半成品版本。",
            guidance="关闭占用显存的程序后重试；若仍失败，可关闭 AI 辅助后使用确定性色幕 Basic。",
            targetStep="matte",
            retryable=True,
        )
    elif any(
        token in lowered
        for token in (
            "emissive_vfx input does not have a uniformly black boundary",
            "emissive_vfx source contains no usable emission energy",
            "emissive_vfx source energy is clipped",
            "emissive_vfx expects an opaque source video frame",
        )
    ):
        failure.update(
            code="emissive_source_invalid",
            title="发光素材未通过输入检查",
            message="素材不满足黑底、有效发光能量或未裁剪要求，旧成果保持不变。",
            guidance="重新确认素材类型；请使用边界均匀黑、包含有效且未裁剪发光能量的不透明源视频。",
            targetStep="matte",
            retryable=False,
        )
    elif any(
        token in lowered
        for token in (
            "birefnet",
            "vitmatte",
            "raft-small",
            "sam3",
            "rotoweave-sam3-alpha-v1",
            "onnxruntime",
            "model unavailable",
            "模型不存在",
            "模型缺失",
        )
    ):
        failure.update(
            code="matte_runtime_unavailable",
            title="抠图运行环境不完整",
            message="高质量抠图所需的本地模型或运行库不可用。",
            guidance="修复 RotoWeave 安装环境后重试；现有素材和人工修订会保留。",
            targetStep="matte",
            retryable=False,
        )
    elif any(token in lowered for token in ("permissionerror", "being used", "资源占用", "temporarily", "timeout", "timed out", "临时文件")):
        failure.update(
            code="temporary_resource_error",
            title="临时资源不可用",
            message="文件、进程或系统资源暂时不可用。",
            guidance="关闭占用该资源的程序，稍后重试。",
            retryable=True,
        )

    match = re.search(r"(?:frame[_ ]?|_f)(\d+)", details, re.IGNORECASE)
    if match:
        failure["frameIndex"] = int(match.group(1))
    return failure


__all__ = [
    "QualityReviewRequired",
    "build_job_failure",
]
