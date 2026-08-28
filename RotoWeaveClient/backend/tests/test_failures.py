from backend.app.failures import (
    QualityReviewRequired,
    build_job_failure,
)


def test_quality_review_required_preserves_candidate_review_contract() -> None:
    review = {
        "schemaVersion": 1,
        "frameCount": 5,
        "endpoint": "/api/v4/jobs/job_1/matte-review",
        "candidates": [],
    }
    failure = build_job_failure(
        QualityReviewRequired("both candidates failed", review=review),
        job_type="matte",
        failed_stage="matte-arbitration",
    )

    assert failure["code"] == "quality_review_required"
    assert failure["title"] == "候选结果需要人工复核"
    assert failure["targetStep"] == "matte"
    assert failure["retryable"] is False
    assert failure["review"] == review


def test_deterministic_pivot_failure_is_actionable_but_not_retryable() -> None:
    failure = build_job_failure(
        "导出自校验失败：frame_12 pivot_error=0.746997px",
        job_type="character_export",
        failed_stage="validation",
    )

    assert failure["code"] == "export_validation_failed"
    assert failure["targetStep"] == "atlas"
    assert failure["failedStage"] == "validation"
    assert failure["frameIndex"] == 12
    assert failure["retryable"] is False
    assert "重新生成" in failure["guidance"]


def test_temporary_resource_failure_allows_retry() -> None:
    failure = build_job_failure(
        "PermissionError: 文件 being used by another process",
        job_type="matte",
        failed_stage="write",
    )

    assert failure["code"] == "temporary_resource_error"
    assert failure["retryable"] is True


def test_unreadable_source_frames_point_back_to_matte_without_retry() -> None:
    failure = build_job_failure(
        "抠图未生成任何有效帧，请检查原始帧文件。",
        job_type="matte",
        failed_stage="matte",
    )

    assert failure["code"] == "source_frames_unreadable"
    assert failure["targetStep"] == "matte"
    assert failure["retryable"] is False


def test_missing_ultra_model_is_reported_as_runtime_problem() -> None:
    failure = build_job_failure(
        "极致质量模型缺失：vitmatte-small-matting.onnx、raft-small-flow.onnx",
        job_type="matte",
        failed_stage="starting",
    )

    assert failure["code"] == "matte_runtime_unavailable"
    assert failure["title"] == "抠图运行环境不完整"
    assert failure["targetStep"] == "matte"
    assert failure["retryable"] is False
    assert "现有素材和人工修订会保留" in failure["guidance"]


def test_empty_emissive_sequence_requests_material_reconfirmation() -> None:
    failure = build_job_failure(
        "emissive_vfx source contains no usable emission energy; reconfirm the material type.",
        job_type="matte",
        failed_stage="matte-preflight",
    )

    assert failure["code"] == "emissive_source_invalid"
    assert failure["targetStep"] == "matte"
    assert failure["retryable"] is False
    assert "重新确认素材类型" in failure["guidance"]
    assert "旧成果保持不变" in failure["message"]


def test_photoshop_import_failure_explains_how_to_fix_the_png() -> None:
    failure = build_job_failure(
        "导回 PNG 尺寸不正确：需要 4096×4096，实际为 2048×2048。",
        job_type="photoshop_sheet_import",
        failed_stage="photoshop_sheet_import",
    )

    assert failure["code"] == "photoshop_sheet_invalid"
    assert failure["targetStep"] == "matte"
    assert failure["retryable"] is False
    assert "原画布尺寸" in failure["guidance"]
