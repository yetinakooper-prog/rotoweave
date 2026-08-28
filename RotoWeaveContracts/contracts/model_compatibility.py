from __future__ import annotations

from typing import Any, Mapping

from .integrity import canonical_sha256
from .model_recipe import ASSET_BY_ROLE, MODEL_RECIPE_ID, PROFILE_ROLES, RECIPE_DIGEST


MODEL_COMPATIBILITY_POLICY_SCHEMA_VERSION = 1
PYTORCH_CHECKPOINT_EXTENSIONS = (".pt", ".pth", ".ckpt", ".bin")
SAFETENSORS_EXTENSIONS = (".safetensors",)


def _container_for_role(role: str) -> str:
    if role in {"green_unmix", "blue_unmix"}:
        return "safetensors"
    return "pytorch-weights-only"


MODEL_COMPATIBILITY_POLICY: dict[str, Any] = {
    "schemaVersion": MODEL_COMPATIBILITY_POLICY_SCHEMA_VERSION,
    "id": "model-local-compatibility-v1",
    "recipeId": MODEL_RECIPE_ID,
    "runtimeOwnership": "fixed-product-runtime-only",
    "activation": "manual-partial-only",
    "qualityCertification": False,
    "roles": {
        role: {
            "container": _container_for_role(role),
            "extensions": list(
                SAFETENSORS_EXTENSIONS
                if _container_for_role(role) == "safetensors"
                else PYTORCH_CHECKPOINT_EXTENSIONS
            ),
            "allowUserCode": False,
        }
        for role in sorted(ASSET_BY_ROLE)
    },
}
MODEL_COMPATIBILITY_POLICY_DIGEST = canonical_sha256(MODEL_COMPATIBILITY_POLICY)


def role_accepts_extension(role: str, extension: str) -> bool:
    definition = MODEL_COMPATIBILITY_POLICY["roles"].get(role)
    return bool(definition and extension.casefold() in definition["extensions"])


def profile_configuration_digest(
    profile: str,
    assets: Mapping[str, Mapping[str, Any]],
) -> str:
    roles = PROFILE_ROLES[profile]
    return canonical_sha256(
        {
            "schemaVersion": 2,
            "profile": profile,
            "recipeId": MODEL_RECIPE_ID,
            "recipeDigest": RECIPE_DIGEST,
            "compatibilityPolicyDigest": MODEL_COMPATIBILITY_POLICY_DIGEST,
            "assets": {
                role: {
                    "assetId": str(assets[role]["id"]),
                    "bytes": int(assets[role]["bytes"]),
                    "sha256": str(assets[role]["sha256"]),
                    "verificationKind": str(assets[role].get("verification_kind") or ""),
                    "verificationReceiptDigest": assets[role].get("verification_receipt_digest"),
                }
                for role in roles
            },
        }
    )
