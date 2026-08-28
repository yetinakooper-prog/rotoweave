from __future__ import annotations

from contracts.legacy_compat import compatible_environment_value

import contextlib
import gc
import hashlib
import importlib
import inspect
import json
import os
import sys
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any, Iterator

import numpy as np

from contracts.integrity import canonical_sha256
from contracts.model_recipe import ASSET_BY_ROLE, MODEL_RECIPE_ID, PROFILE_ROLES, RECIPE_DIGEST
from contracts.model_compatibility import (
    MODEL_COMPATIBILITY_POLICY_DIGEST,
    profile_configuration_digest,
)

try:
    from .vitmatte_detectron2_compat import install_detectron2_compat
except ImportError:
    # The signed adapter is loaded as a top-level module from runtime/src.
    # Resolve the compatibility function while that verified directory is on
    # sys.path; later ROI execution must not depend on the transient import
    # path still being present.
    from vitmatte_detectron2_compat import install_detectron2_compat


def _protocol_safe_print(*values: object, **kwargs: Any) -> None:
    """Route unavoidable upstream diagnostics away from NDJSON stdout."""

    options = dict(kwargs)
    options["file"] = sys.stderr
    print(*values, **options)


@dataclass(frozen=True, slots=True)
class FrozenModelLayout:
    pack_root: Path
    sam2_source: Path
    corridor_source: Path
    vitmatte_source: Path
    sam2_checkpoint: Path
    corridor_green_checkpoint: Path
    corridor_blue_checkpoint: Path
    vitmatte_checkpoint: Path
    sam3_source: Path | None = None
    sam3_checkpoint: Path | None = None
    sam3_runtime_module: str | None = None

    @classmethod
    def from_configuration(cls, configuration_path: str | Path) -> "FrozenModelLayout":
        """Build the fixed adapter layout from a server-owned binding file.

        The binding file may contain user-owned weight paths. Schema 1 keeps
        the exact official Recipe contract; schema 2 also accepts a structural
        receipt bound to the actual SHA and compatibility policy. Source trees
        and the adapter module remain product-owned inputs written by the server.
        """

        path = Path(configuration_path).resolve(strict=True)
        if not path.is_file() or path.is_symlink():
            raise RuntimeError("The model configuration file is unavailable.")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError("The model configuration file is invalid.") from exc
        if not isinstance(payload, dict) or payload.get("schemaVersion") not in {1, 2}:
            raise RuntimeError("Unsupported model configuration schema.")
        schema_version = int(payload["schemaVersion"])
        if payload.get("recipeId") != MODEL_RECIPE_ID or payload.get("recipeDigest") != RECIPE_DIGEST:
            raise RuntimeError("The model configuration does not match the built-in Recipe.")
        assets = payload.get("assets")
        sources = payload.get("sources")
        if not isinstance(assets, dict) or not isinstance(sources, dict):
            raise RuntimeError("The model configuration has no assets or fixed sources.")
        profile = str(payload.get("profile") or "").lower()
        if profile not in PROFILE_ROLES:
            raise RuntimeError("The model configuration Profile is invalid.")
        if schema_version == 1:
            expected_digest = canonical_sha256(
                {
                    "recipeId": MODEL_RECIPE_ID,
                    "recipeDigest": RECIPE_DIGEST,
                    "assets": {
                        role: {
                            "assetId": str((assets.get(role) or {}).get("assetId") or ""),
                            "sha256": ASSET_BY_ROLE[role].sha256,
                        }
                        for role in sorted(ASSET_BY_ROLE)
                    },
                }
            )
            if payload.get("configurationDigest") != expected_digest:
                raise RuntimeError("The model configuration digest is invalid.")
        elif payload.get("compatibilityPolicyDigest") != MODEL_COMPATIBILITY_POLICY_DIGEST:
            raise RuntimeError("The model compatibility policy is invalid.")

        verified: dict[str, Path] = {}
        digest_assets: dict[str, dict[str, Any]] = {}
        for role in PROFILE_ROLES[profile]:
            recipe = ASSET_BY_ROLE[role]
            record = assets.get(role)
            if not isinstance(record, dict) or not str(record.get("assetId") or ""):
                raise RuntimeError(f"The model configuration is missing role: {role}.")
            if record.get("modelId") != recipe.model_id:
                raise RuntimeError(f"Configured model metadata is invalid: {role}.")
            expected_sha = str(record.get("sha256") or "")
            expected_bytes = int(record.get("bytes") or recipe.bytes)
            if schema_version == 1 and (
                expected_sha != recipe.sha256 or expected_bytes != recipe.bytes
            ):
                raise RuntimeError(f"Configured official model metadata is invalid: {role}.")
            if schema_version == 2:
                kind = str(record.get("verificationKind") or "")
                if kind == "official":
                    if expected_sha != recipe.sha256 or expected_bytes != recipe.bytes:
                        raise RuntimeError(f"Configured official model identity is invalid: {role}.")
                elif kind == "structural":
                    receipt = record.get("verificationReceipt")
                    receipt_digest = str(record.get("verificationReceiptDigest") or "")
                    if (
                        record.get("verificationContractDigest") != MODEL_COMPATIBILITY_POLICY_DIGEST
                        or not isinstance(receipt, dict)
                        or canonical_sha256(receipt) != receipt_digest
                        or receipt.get("state") != "passed"
                        or receipt.get("role") != role
                        or receipt.get("sha256") != expected_sha
                        or int(receipt.get("bytes") or -1) != expected_bytes
                        or receipt.get("compatibilityPolicyDigest") != MODEL_COMPATIBILITY_POLICY_DIGEST
                    ):
                        raise RuntimeError(f"Configured structural receipt is invalid: {role}.")
                else:
                    raise RuntimeError(f"Configured verification kind is invalid: {role}.")
                digest_assets[role] = {
                    "id": str(record["assetId"]),
                    "bytes": expected_bytes,
                    "sha256": expected_sha,
                    "verification_kind": kind,
                    "verification_receipt_digest": record.get("verificationReceiptDigest"),
                }
            candidate = Path(str(record.get("path") or "")).resolve(strict=True)
            if (
                not candidate.is_file()
                or candidate.is_symlink()
                or candidate.stat().st_size != expected_bytes
            ):
                raise RuntimeError(f"Configured model identity is invalid: {role}.")
            digest = hashlib.sha256()
            with candidate.open("rb") as stream:
                for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
                    digest.update(chunk)
            if digest.hexdigest() != expected_sha:
                raise RuntimeError(f"Configured model hash changed: {role}.")
            verified[role] = candidate
        if schema_version == 2:
            expected_digest = profile_configuration_digest(profile, digest_assets)
            if (
                payload.get("configurationDigest") != expected_digest
                or payload.get("profileConfigurationDigest") != expected_digest
            ):
                raise RuntimeError("The Profile model configuration digest is invalid.")

        fixed_sources: dict[str, Path] = {}
        required_sources = ("sam2", "corridor", "vitmatte", "sam3") if profile == "ultra" else ("sam2", "corridor", "vitmatte")
        for name in required_sources:
            source = Path(str(sources.get(name) or "")).resolve(strict=True)
            if not source.is_dir() or source.is_symlink():
                raise RuntimeError(f"Fixed runtime source is unavailable: {name}.")
            fixed_sources[name] = source
        runtime = payload.get("runtime")
        if not isinstance(runtime, dict) or runtime.get("adapter") != "worker.cuda_matting.rotoweave_adapter":
            raise RuntimeError("The product adapter identity is invalid.")
        return cls(
            pack_root=path.parent,
            sam2_source=fixed_sources["sam2"],
            corridor_source=fixed_sources["corridor"],
            vitmatte_source=fixed_sources["vitmatte"],
            sam2_checkpoint=verified["alpha_and_tracking"],
            corridor_green_checkpoint=verified["green_unmix"],
            corridor_blue_checkpoint=verified["blue_unmix"],
            vitmatte_checkpoint=verified["roi_refine"],
            sam3_source=fixed_sources.get("sam3"),
            sam3_checkpoint=verified.get("ultra_alpha"),
            sam3_runtime_module=(
                "worker.cuda_matting.sam3_local_runtime" if profile == "ultra" else None
            ),
        )

    @classmethod
    def from_environment(cls) -> "FrozenModelLayout":
        configuration = compatible_environment_value("ROTOWEAVE_MODEL_CONFIGURATION")
        if not configuration:
            raise RuntimeError("ROTOWEAVE_MODEL_CONFIGURATION is required.")
        return cls.from_configuration(configuration)


@contextlib.contextmanager
def _prepend_import_path(path: Path) -> Iterator[None]:
    value = str(path.resolve())
    sys.path.insert(0, value)
    try:
        yield
    finally:
        try:
            sys.path.remove(value)
        except ValueError:
            pass


def enforce_offline_runtime() -> None:
    """Block model libraries from treating the production pack as an online client."""

    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    os.environ.setdefault("DIFFUSERS_OFFLINE", "1")


def release_cuda() -> dict[str, float]:
    import torch

    if torch.cuda.is_available():
        torch.cuda.synchronize()
        allocated = float(torch.cuda.max_memory_allocated() / 2**20)
        reserved = float(torch.cuda.max_memory_reserved() / 2**20)
        torch.cuda.empty_cache()
        torch.cuda.ipc_collect()
        torch.cuda.reset_peak_memory_stats()
    else:
        allocated = 0.0
        reserved = 0.0
    gc.collect()
    return {"peakAllocatedMiB": allocated, "peakReservedMiB": reserved}


def _require_cuda() -> Any:
    import torch

    if not torch.cuda.is_available():
        raise RuntimeError("The production model runtime requires a working NVIDIA CUDA device.")
    try:
        smoke = torch.ones(1, device="cuda", dtype=torch.float32) + 1.0
        torch.cuda.synchronize()
        if float(smoke.item()) != 2.0:
            raise RuntimeError("CUDA smoke computation returned an invalid result.")
    except Exception as exc:
        raise RuntimeError(f"CUDA smoke computation failed: {exc}") from exc
    return torch


def load_sam2matting_bplus(layout: FrozenModelLayout) -> Any:
    enforce_offline_runtime()
    _require_cuda()
    with _prepend_import_path(layout.sam2_source):
        build_module = importlib.import_module("sam2.build_sam")
        predictor_module = importlib.import_module("sam2.sam2matting_image_predictor")
    # Upstream build_sam prints checkpoint key diagnostics. stdout is the
    # Worker's protocol stream, so override only that module's global print
    # without redirecting process-wide stdout (the heartbeat thread still
    # needs to emit NDJSON while a model is loading).
    build_module.print = _protocol_safe_print
    model = build_module.build_sam2matting(
        "configs/sam2matting-sam2.1base+.yaml",
        str(layout.sam2_checkpoint),
        device="cuda",
    )
    return predictor_module.SAM2MattingImagePredictor(model)


def infer_sam2matting_alpha(
    predictor: Any, image_srgb_u8: np.ndarray, alpha_hint: np.ndarray
) -> np.ndarray:
    from PIL import Image

    torch = _require_cuda()
    image = np.asarray(image_srgb_u8, dtype=np.uint8)
    hint = np.clip(np.asarray(alpha_hint, dtype=np.float32), 0.0, 1.0)
    if image.ndim != 3 or image.shape[2] != 3 or hint.shape != image.shape[:2]:
        raise RuntimeError("SAM2Matting image and AlphaHint dimensions do not match.")
    raw_mask = torch.from_numpy(hint) > 0.005
    mask_input = (torch.from_numpy(hint) > 0.005).float() * 20.0 - 10.0
    mask_input = torch.nn.functional.interpolate(
        mask_input[None, None],
        size=(256, 256),
        mode="bilinear",
        align_corners=False,
    )
    with torch.inference_mode(), torch.autocast("cuda", dtype=torch.float16):
        encoded = predictor.set_image(Image.fromarray(image, mode="RGB"))
        _, alpha, _ = predictor.predict(
            img=encoded,
            raw_mask=raw_mask,
            mask_input=mask_input,
            multimask_output=False,
        )
    result = np.asarray(alpha, dtype=np.float32).squeeze()
    if result.shape != hint.shape or not np.isfinite(result).all():
        raise RuntimeError("SAM2Matting returned an invalid Alpha tensor.")
    return np.clip(result, 0.0, 1.0)


def load_sam3(layout: FrozenModelLayout) -> Any:
    """Load the approved SAM3 candidate through its signed, stable ABI."""

    enforce_offline_runtime()
    _require_cuda()
    if (
        layout.sam3_source is None
        or layout.sam3_checkpoint is None
        or not layout.sam3_runtime_module
    ):
        raise RuntimeError("The independent model configuration has no approved SAM3 runtime.")
    with _prepend_import_path(layout.sam3_source):
        module = importlib.import_module(layout.sam3_runtime_module)
        module_file = Path(str(getattr(module, "__file__", ""))).resolve(strict=False)
        trusted_product_module = layout.sam3_runtime_module == "worker.cuda_matting.sam3_local_runtime"
        if not trusted_product_module:
            try:
                module_file.relative_to(layout.sam3_source.resolve())
            except ValueError as exc:
                raise RuntimeError("SAM3 runtimeModule did not load from the fixed source.") from exc
        load_model = getattr(module, "load_model", None)
        infer_alpha = getattr(module, "infer_alpha", None)
        if not callable(load_model) or not callable(infer_alpha):
            raise RuntimeError(
                "SAM3 runtime must export load_model and infer_alpha for "
                "rotoweave-sam3-alpha-v1."
            )
        parameters = inspect.signature(load_model).parameters
        if "source_path" not in parameters:
            raise RuntimeError("SAM3 runtime must accept the fixed source_path contract.")
        model = load_model(
            str(layout.sam3_checkpoint),
            "cuda",
            "fp16",
            source_path=str(layout.sam3_source),
        )
    return {
        "model": model,
        "inferAlpha": infer_alpha,
        "sourcePath": layout.sam3_source,
    }


def infer_sam3_alpha(
    runtime: Any, image_srgb_u8: np.ndarray, alpha_hint: np.ndarray
) -> np.ndarray:
    image = np.asarray(image_srgb_u8, dtype=np.uint8)
    hint = np.clip(np.asarray(alpha_hint, dtype=np.float32), 0.0, 1.0)
    if image.ndim != 3 or image.shape[2] != 3 or hint.shape != image.shape[:2]:
        raise RuntimeError("SAM3 image and AlphaHint dimensions do not match.")
    source_path = runtime.get("sourcePath")
    if source_path is None:
        raw_result = runtime["inferAlpha"](runtime["model"], image, hint)
    else:
        with _prepend_import_path(Path(source_path)):
            raw_result = runtime["inferAlpha"](runtime["model"], image, hint)
    result = np.asarray(raw_result, dtype=np.float32).squeeze()
    if result.shape != hint.shape:
        raise RuntimeError("SAM3 returned an Alpha tensor with invalid dimensions.")
    if not np.isfinite(result).all():
        raise RuntimeError("SAM3 returned a non-finite Alpha tensor.")
    if float(result.min()) < 0.0 or float(result.max()) > 1.0:
        raise RuntimeError("SAM3 returned Alpha outside the required [0, 1] range.")
    return np.ascontiguousarray(result, dtype=np.float32)


def load_corridorkey(
    layout: FrozenModelLayout,
    screen_color: str,
    *,
    device: str = "cuda",
    compile_model: bool = True,
) -> Any:
    enforce_offline_runtime()
    if device not in {"cuda", "cpu"}:
        raise RuntimeError("CorridorKey device must be cuda or cpu.")
    torch = _require_cuda() if device == "cuda" else importlib.import_module("torch")
    normalized = str(screen_color).strip().lower()
    if normalized not in {"green", "blue"}:
        raise RuntimeError("CorridorKey screen color must be green or blue.")
    checkpoint = (
        layout.corridor_green_checkpoint
        if normalized == "green"
        else layout.corridor_blue_checkpoint
    )
    with _prepend_import_path(layout.corridor_source):
        module = importlib.import_module("CorridorKeyModule.inference_engine")
    # CorridorKey can print checkpoint resize/key warnings during its
    # constructor. Keep those diagnostics on stderr as required by the Worker
    # protocol while leaving the upstream frozen source unchanged.
    module.print = _protocol_safe_print
    previous_skip_compile = os.environ.get("CORRIDORKEY_SKIP_COMPILE")
    if not compile_model or device == "cpu":
        os.environ["CORRIDORKEY_SKIP_COMPILE"] = "1"
    try:
        return module.CorridorKeyEngine(
            str(checkpoint),
            device=device,
            img_size=2048,
            use_refiner=True,
            mixed_precision=device == "cuda",
            model_precision=torch.float32,
        )
    finally:
        if previous_skip_compile is None:
            os.environ.pop("CORRIDORKEY_SKIP_COMPILE", None)
        else:
            os.environ["CORRIDORKEY_SKIP_COMPILE"] = previous_skip_compile


def infer_corridorkey(
    engine: Any,
    image_linear: np.ndarray,
    alpha_hint: np.ndarray,
    *,
    screen_color: str,
) -> dict[str, np.ndarray]:
    image = np.asarray(image_linear, dtype=np.float32)
    hint = np.clip(np.asarray(alpha_hint, dtype=np.float32), 0.0, 1.0)
    if image.ndim != 3 or image.shape[2] != 3 or hint.shape != image.shape[:2]:
        raise RuntimeError("CorridorKey image and AlphaHint dimensions do not match.")
    screen_channel = 1 if screen_color == "green" else 2
    result = engine.process_frame(
        image,
        hint,
        input_is_linear=True,
        fg_is_straight=True,
        despill_strength=0.0,
        auto_despeckle=False,
        generate_comp=False,
        post_process_on_gpu=True,
        screen_channel=screen_channel,
    )
    normalized = {
        key: np.asarray(result[key], dtype=np.float32)
        for key in ("alpha", "fg", "processed")
    }
    if any(not value.size or not np.isfinite(value).all() for value in normalized.values()):
        raise RuntimeError("CorridorKey returned a non-finite tensor.")
    return normalized


def _install_vitmatte_compat() -> None:
    install_detectron2_compat()


def load_vitmatte_base(layout: FrozenModelLayout) -> Any:
    enforce_offline_runtime()
    torch = _require_cuda()
    import torch.nn as nn

    _install_vitmatte_compat()
    with _prepend_import_path(layout.vitmatte_source):
        module = importlib.import_module("modeling")
    backbone = module.ViT(
        in_chans=4,
        img_size=512,
        patch_size=16,
        embed_dim=768,
        depth=12,
        num_heads=12,
        drop_path_rate=0,
        window_size=14,
        mlp_ratio=4,
        qkv_bias=True,
        norm_layer=partial(nn.LayerNorm, eps=1e-6),
        window_block_indexes=[0, 1, 3, 4, 6, 7, 9, 10],
        residual_block_indexes=[2, 5, 8, 11],
        use_rel_pos=True,
        out_feature="last_feat",
    )
    model = module.ViTMatte(
        backbone=backbone,
        criterion=nn.Identity(),
        pixel_mean=[123.675 / 255.0, 116.280 / 255.0, 103.530 / 255.0],
        pixel_std=[58.395 / 255.0, 57.120 / 255.0, 57.375 / 255.0],
        input_format="RGB",
        size_divisibility=32,
        decoder=module.Detail_Capture(in_chans=768),
    )
    state = torch.load(layout.vitmatte_checkpoint, map_location="cpu", weights_only=True)
    missing, unexpected = model.load_state_dict(state, strict=False)
    if missing or unexpected:
        raise RuntimeError(
            f"ViTMatte checkpoint mismatch: missing={missing}, unexpected={unexpected}"
        )
    return model.to(device="cuda", dtype=torch.float16).eval()


def infer_vitmatte_alpha(
    model: Any, image_srgb: np.ndarray, trimap: np.ndarray
) -> np.ndarray:
    torch = _require_cuda()
    image = np.asarray(image_srgb, dtype=np.float32)
    matte = np.clip(np.asarray(trimap, dtype=np.float32), 0.0, 1.0)
    if image.ndim != 3 or image.shape[2] != 3 or matte.shape != image.shape[:2]:
        raise RuntimeError("ViTMatte image and trimap dimensions do not match.")
    image_tensor = torch.from_numpy(image).permute(2, 0, 1)[None].to(
        "cuda", dtype=torch.float16
    )
    trimap_tensor = torch.from_numpy(matte)[None, None].to("cuda", dtype=torch.float16)
    with torch.inference_mode(), torch.autocast("cuda", dtype=torch.float16):
        alpha = model({"image": image_tensor, "trimap": trimap_tensor})["phas"]
    result = alpha.float().cpu().numpy().squeeze()
    if result.shape != matte.shape or not np.isfinite(result).all():
        raise RuntimeError("ViTMatte returned an invalid Alpha tensor.")
    return np.clip(result, 0.0, 1.0)


__all__ = [
    "FrozenModelLayout",
    "enforce_offline_runtime",
    "infer_corridorkey",
    "infer_sam3_alpha",
    "infer_sam2matting_alpha",
    "infer_vitmatte_alpha",
    "load_corridorkey",
    "load_sam3",
    "load_sam2matting_bplus",
    "load_vitmatte_base",
    "release_cuda",
]
