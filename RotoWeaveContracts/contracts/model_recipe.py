from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .integrity import canonical_sha256


MODEL_RECIPE_SCHEMA_VERSION = 1
MODEL_RECIPE_ID = "matting-high-ultra-v1"


@dataclass(frozen=True, slots=True)
class RecipeAsset:
    role: str
    model_id: str
    display_name: str
    filename: str
    bytes: int
    sha256: str
    revision: str
    source_url: str
    license_id: str
    profiles: tuple[str, ...]
    runtime_contract: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "modelId": self.model_id,
            "displayName": self.display_name,
            "filename": self.filename,
            "bytes": self.bytes,
            "sha256": self.sha256,
            "revision": self.revision,
            "sourceUrl": self.source_url,
            "licenseId": self.license_id,
            "profiles": list(self.profiles),
            "runtimeContract": self.runtime_contract,
        }


ASSETS: tuple[RecipeAsset, ...] = (
    RecipeAsset(
        role="alpha_and_tracking",
        model_id="sam2matting-bplus",
        display_name="SAM2Matting B+",
        filename="SAM2Matting-SAM2.1Base+.pt",
        bytes=383_180_506,
        sha256="1f0eb2eda3e8bc9101eafc0b30b8b8fcae1ff83d8fd3adc18e2f3b410fdaae60",
        revision="73dd721d77b56749248aefe5e8824d7f61b9d13c",
        source_url="https://github.com/FudanCVL/SAM2Matting",
        license_id="CC-BY-NC-SA-4.0-upstream-terms-apply",
        profiles=("high", "ultra"),
        runtime_contract="rotoweave-sam2matting-bplus-v1",
    ),
    RecipeAsset(
        role="green_unmix",
        model_id="corridorkey-green",
        display_name="CorridorKey Green",
        filename="CorridorKey_v1.0.safetensors",
        bytes=398_849_256,
        sha256="74d614f7d92fc559a118c30a7deadedc3cacd8ef83dcb85a030d0bed7af8b20b",
        revision="97e55a453060745bead1befd293f6e523c4b845c",
        source_url="https://github.com/nikopueringer/CorridorKey",
        license_id="CC-BY-NC-SA-4.0-upstream-terms-apply",
        profiles=("high", "ultra"),
        runtime_contract="rotoweave-corridorkey-v1",
    ),
    RecipeAsset(
        role="blue_unmix",
        model_id="corridorkey-blue",
        display_name="CorridorKey Blue",
        filename="CorridorKeyBlue_1.0.safetensors",
        bytes=398_849_256,
        sha256="43bc5f6a08a9e5effe5d633d0d84bb0aff91037b35ab85d16cd812b38c5cac23",
        revision="97e55a453060745bead1befd293f6e523c4b845c",
        source_url="https://github.com/nikopueringer/CorridorKey",
        license_id="CC-BY-NC-SA-4.0-upstream-terms-apply",
        profiles=("high", "ultra"),
        runtime_contract="rotoweave-corridorkey-v1",
    ),
    RecipeAsset(
        role="roi_refine",
        model_id="vitmatte-base",
        display_name="ViTMatte Base",
        filename="ViTMatte_B_Com.pth",
        bytes=386_835_645,
        sha256="469adc97e89689f47a2bb005e8937ebe7913e36249f2f45b1a86664480b9c59d",
        revision="8cd7ef068380977c3962c4cb733cb1fe7f2241a5",
        source_url="https://github.com/hustvl/ViTMatte",
        license_id="MIT-upstream-terms-apply",
        profiles=("high", "ultra"),
        runtime_contract="rotoweave-vitmatte-base-v1",
    ),
    RecipeAsset(
        role="ultra_alpha",
        model_id="sam3",
        display_name="SAM3",
        filename="sam3.pt",
        bytes=3_450_062_241,
        sha256="9999e2341ceef5e136daa386eecb55cb414446a00ac2b55eb2dfd2f7c3cf8c9e",
        revision="96914d2425f90a64f45ca977c2b5165418099543",
        source_url="https://github.com/facebookresearch/sam3",
        license_id="SAM-License-gated-access-upstream-terms-apply",
        profiles=("ultra",),
        runtime_contract="rotoweave-sam3-alpha-v1",
    ),
)

ASSET_BY_ROLE = {item.role: item for item in ASSETS}
ASSET_BY_SHA256 = {item.sha256: item for item in ASSETS}
PROFILE_ROLES = {
    profile: tuple(item.role for item in ASSETS if profile in item.profiles)
    for profile in ("high", "ultra")
}


def recipe_payload() -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schemaVersion": MODEL_RECIPE_SCHEMA_VERSION,
        "id": MODEL_RECIPE_ID,
        "displayName": "High + Ultra 官方抠图 Recipe",
        "assets": [item.as_dict() for item in ASSETS],
        "profiles": {
            profile: {"requiredRoles": list(roles)}
            for profile, roles in PROFILE_ROLES.items()
        },
        "distribution": "user-owned-local-assets",
        "qualityComparisonRequired": False,
    }
    payload["digest"] = canonical_sha256(payload)
    return payload


RECIPE = recipe_payload()
RECIPE_DIGEST = str(RECIPE["digest"])
