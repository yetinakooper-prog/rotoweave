from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import sys
import time
from importlib import metadata
from pathlib import Path
from typing import Any


def ensure_minimum_timeout(name: str, minimum_seconds: int) -> None:
    """Set Hugging Face timeouts before any library can import huggingface_hub."""

    try:
        configured = int(os.environ.get(name, ""))
    except ValueError:
        configured = 0
    if configured < minimum_seconds:
        os.environ[name] = str(minimum_seconds)


ensure_minimum_timeout("HF_HUB_DOWNLOAD_TIMEOUT", 120)
ensure_minimum_timeout("HF_HUB_ETAG_TIMEOUT", 60)

import numpy as np
import torch
from torchvision.ops import deform_conv2d


MODEL_ID = "ZhengPeng7/BiRefNet_lite-matting"
MODEL_REVISION = "99c33412e3f58e1f33187abdc8c435c645243690"
SOURCE_FILES = {
    "model.safetensors": "ce8bcfc045e336322c0424a5863dcfb7e9ce8fed0a5fd4d1b2b20adf12d97243",
    "birefnet.py": "af8568b5be406bf4d2a68a7ed6d72e40f73b37a1fb6fc9ebd71b5b3cbcd069c9",
    "BiRefNet_config.py": "e7b8c2a74f6cea6a59553d517f71d47f2c1d90e670a13416af17c25fe2f3dc52",
    "config.json": "2050e7f1d76417bb167d86a22a52737de2a6e114c0f26c8df85c188366819d72",
}
OPSET = 22
EXPECTED_DEFORM_CONV_LAYERS = 20
SOURCE_DOWNLOAD_ATTEMPTS = 6


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(4 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def package_version(name: str) -> str:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return "unknown"


def download_pinned_source_file(name: str, hub_cache_dir: Path) -> Path:
    from huggingface_hub import hf_hub_download
    from requests.exceptions import ChunkedEncodingError, ConnectionError, Timeout

    retryable_errors = (ChunkedEncodingError, ConnectionError, Timeout, TimeoutError)
    for attempt in range(1, SOURCE_DOWNLOAD_ATTEMPTS + 1):
        try:
            return Path(
                hf_hub_download(
                    MODEL_ID,
                    name,
                    revision=MODEL_REVISION,
                    cache_dir=str(hub_cache_dir),
                    etag_timeout=60,
                )
            )
        except retryable_errors as exc:
            if attempt >= SOURCE_DOWNLOAD_ATTEMPTS:
                raise RuntimeError(
                    f"BiRefNet source download failed after {attempt} resumable attempts: {name}"
                ) from exc
            delay = min(2 ** (attempt - 1), 15)
            print(
                f"BiRefNet source download interrupted for {name}; "
                f"resuming in {delay}s ({attempt}/{SOURCE_DOWNLOAD_ATTEMPTS})...",
                file=sys.stderr,
            )
            time.sleep(delay)
    raise AssertionError("unreachable")


def resolve_source(
    source_dir: Path | None, hub_cache_dir: Path | None = None
) -> tuple[str, dict[str, Path]]:
    if source_dir is not None:
        root = source_dir.resolve()
        files = {name: root / name for name in SOURCE_FILES}
    else:
        if hub_cache_dir is None:
            raise RuntimeError("A dedicated Hugging Face cache is required for source download")
        hub_cache_dir.mkdir(parents=True, exist_ok=True)
        files = {
            name: download_pinned_source_file(name, hub_cache_dir)
            for name in SOURCE_FILES
        }
        root = files["config.json"].parent
    for name, expected_hash in SOURCE_FILES.items():
        path = files[name]
        if not path.is_file():
            raise RuntimeError(f"BiRefNet source file is missing: {path}")
        actual_hash = sha256_file(path)
        if actual_hash != expected_hash:
            raise RuntimeError(
                f"BiRefNet source SHA-256 mismatch for {name}: {actual_hash}"
            )
    return str(root), files


class MatteLogits(torch.nn.Module):
    def __init__(self, model: torch.nn.Module):
        super().__init__()
        self.model = model

    def forward(self, image: torch.Tensor) -> torch.Tensor:
        return self.model(image)[0]


class NativeDeformConvFunction(torch.autograd.Function):
    """PyTorch reference with a standard ONNX DeformConv export symbol."""

    @staticmethod
    def forward(
        ctx: Any,
        image: torch.Tensor,
        offset: torch.Tensor,
        weight: torch.Tensor,
        bias: torch.Tensor,
        mask: torch.Tensor,
        stride_y: int,
        stride_x: int,
        padding_y: int,
        padding_x: int,
        dilation_y: int,
        dilation_x: int,
        groups: int,
        offset_groups: int,
    ) -> torch.Tensor:
        del ctx, groups, offset_groups
        return deform_conv2d(
            image,
            offset,
            weight,
            bias,
            (stride_y, stride_x),
            (padding_y, padding_x),
            (dilation_y, dilation_x),
            mask,
        )

    @staticmethod
    def symbolic(
        graph: Any,
        image: Any,
        offset: Any,
        weight: Any,
        bias: Any,
        mask: Any,
        stride_y: int,
        stride_x: int,
        padding_y: int,
        padding_x: int,
        dilation_y: int,
        dilation_x: int,
        groups: int,
        offset_groups: int,
    ) -> Any:
        kernel_y, kernel_x = weight.type().sizes()[-2:]
        return graph.op(
            "DeformConv",
            image,
            weight,
            offset,
            bias,
            mask,
            kernel_shape_i=[kernel_y, kernel_x],
            strides_i=[stride_y, stride_x],
            pads_i=[padding_y, padding_x, padding_y, padding_x],
            dilations_i=[dilation_y, dilation_x],
            group_i=groups,
            offset_group_i=offset_groups,
        )


def pair(value: Any) -> tuple[int, int]:
    if isinstance(value, tuple):
        return int(value[0]), int(value[1])
    return int(value), int(value)


class NativeDeformableConv2d(torch.nn.Module):
    def __init__(self, source: torch.nn.Module):
        super().__init__()
        self.offset_conv = source.offset_conv
        self.modulator_conv = source.modulator_conv
        self.regular_conv = source.regular_conv
        self.stride = pair(source.stride)
        self.padding = pair(source.padding)
        self.dilation = pair(self.regular_conv.dilation)
        self.groups = int(self.regular_conv.groups)
        kernel_y, kernel_x = pair(self.regular_conv.kernel_size)
        self.offset_groups = int(
            self.offset_conv.out_channels // (2 * kernel_y * kernel_x)
        )
        if self.regular_conv.bias is None:
            self.register_buffer(
                "zero_bias",
                torch.zeros(
                    self.regular_conv.out_channels,
                    dtype=self.regular_conv.weight.dtype,
                ),
            )
        else:
            self.zero_bias = None

    def forward(self, image: torch.Tensor) -> torch.Tensor:
        offset = self.offset_conv(image)
        mask = 2.0 * torch.sigmoid(self.modulator_conv(image))
        bias = (
            self.regular_conv.bias
            if self.regular_conv.bias is not None
            else self.zero_bias
        )
        return NativeDeformConvFunction.apply(
            image,
            offset,
            self.regular_conv.weight,
            bias,
            mask,
            self.stride[0],
            self.stride[1],
            self.padding[0],
            self.padding[1],
            self.dilation[0],
            self.dilation[1],
            self.groups,
            self.offset_groups,
        )


def replace_deformable_convolutions(module: torch.nn.Module) -> int:
    replaced = 0
    for name, child in list(module.named_children()):
        if child.__class__.__name__ == "DeformableConv2d":
            setattr(module, name, NativeDeformableConv2d(child))
            replaced += 1
        else:
            replaced += replace_deformable_convolutions(child)
    return replaced


def deterministic_input() -> torch.Tensor:
    vertical = torch.linspace(-1.0, 1.0, 1024).reshape(1, 1, 1024, 1)
    horizontal = torch.linspace(-1.0, 1.0, 1024).reshape(1, 1, 1, 1024)
    image = torch.empty((1, 3, 1024, 1024), dtype=torch.float32)
    image[:, 0] = horizontal
    image[:, 1] = vertical
    image[:, 2] = torch.sin(horizontal * 7.0) * torch.cos(vertical * 5.0)
    return image


def export(
    output: Path,
    source_dir: Path | None,
    cache_dir: Path,
    contract_path: Path,
    license_path: Path,
    requirements_path: Path,
) -> None:
    contract_path = contract_path.resolve(strict=True)
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    contract_sources = {
        name: str(value["sha256"])
        for name, value in contract.get("sourceFiles", {}).items()
    }
    expected_identity = {
        "artifactPolicy": "build-from-pinned-source",
        "modelId": MODEL_ID,
        "nativeDeformConvLayers": EXPECTED_DEFORM_CONV_LAYERS,
        "opset": OPSET,
        "revision": MODEL_REVISION,
        "sourceFiles": SOURCE_FILES,
    }
    actual_identity = {
        "artifactPolicy": contract.get("artifactPolicy"),
        "modelId": contract.get("modelId"),
        "nativeDeformConvLayers": contract.get("nativeDeformConvLayers"),
        "opset": contract.get("opset"),
        "revision": contract.get("revision"),
        "sourceFiles": contract_sources,
    }
    if actual_identity != expected_identity:
        raise RuntimeError("Basic versioned contract does not match the pinned exporter identity")
    license_path = license_path.resolve(strict=True)
    if str(contract.get("license")) != "MIT":
        raise RuntimeError("Basic versioned contract must declare the pinned MIT license")
    if license_path.name != contract.get("licenseFile") or sha256_file(license_path) != contract.get("licenseSha256"):
        raise RuntimeError("Basic pinned license file does not match the versioned contract")
    requirements_path = requirements_path.resolve(strict=True)

    cache_root = cache_dir.resolve()
    cache_root.mkdir(parents=True, exist_ok=True)
    huggingface_root = cache_root / "huggingface"
    hub_cache_dir = huggingface_root / "hub"
    os.environ["HF_HOME"] = str(huggingface_root)
    os.environ["HF_MODULES_CACHE"] = str(cache_root / "hf_modules")

    model_reference, source_files = resolve_source(source_dir, hub_cache_dir)
    from transformers import AutoModelForImageSegmentation

    load_options: dict[str, Any] = {
        "trust_remote_code": True,
        "dtype": torch.float32,
        "local_files_only": True,
    }
    model = AutoModelForImageSegmentation.from_pretrained(
        model_reference, **load_options
    ).eval()
    if hasattr(model, "config"):
        model.config.compile = False
        model.config.SDPA_enabled = False

    sample = deterministic_input()
    with torch.inference_mode():
        reference = np.ascontiguousarray(model(sample)[0].cpu().numpy())
    replaced = replace_deformable_convolutions(model)
    if replaced != EXPECTED_DEFORM_CONV_LAYERS:
        raise RuntimeError(
            "Unexpected BiRefNet deformable convolution count: "
            f"{replaced} (expected {EXPECTED_DEFORM_CONV_LAYERS})"
        )

    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    wrapper = MatteLogits(model).eval()
    torch.onnx.export(
        wrapper,
        sample,
        str(output),
        input_names=["image"],
        output_names=["matte_logits"],
        opset_version=OPSET,
        dynamo=False,
        do_constant_folding=True,
    )

    import onnx
    import onnxruntime as ort

    graph = onnx.load(str(output), load_external_data=True)
    onnx.checker.check_model(graph)
    deform_count = sum(
        node.domain == "" and node.op_type == "DeformConv"
        for node in graph.graph.node
    )
    if deform_count != EXPECTED_DEFORM_CONV_LAYERS:
        raise RuntimeError(
            f"Exported ONNX contains {deform_count} DeformConv nodes; "
            f"expected {EXPECTED_DEFORM_CONV_LAYERS}."
        )
    session = ort.InferenceSession(str(output), providers=["CPUExecutionProvider"])
    actual = np.asarray(
        session.run(None, {"image": np.ascontiguousarray(sample.numpy())})[0]
    )
    maximum_error = float(np.max(np.abs(reference - actual)))
    if maximum_error > 1e-4:
        raise RuntimeError(
            f"BiRefNet PyTorch/ONNX CPU comparison failed: maxAbs={maximum_error}"
        )

    self_test = output.with_name("birefnet-lite-matting-selftest.npz")
    np.savez_compressed(
        self_test,
        image=np.ascontiguousarray(sample.numpy()),
        matte_logits=reference,
    )
    output_hash = sha256_file(output)
    self_test_hash = sha256_file(self_test)
    export_environment = {
        "einops": package_version("einops"),
        "kornia": package_version("kornia"),
        "onnx": package_version("onnx"),
        "onnxruntime": ort.__version__,
        "python": platform.python_version(),
        "timm": package_version("timm"),
        "torch": torch.__version__,
        "torchvision": package_version("torchvision"),
        "transformers": package_version("transformers"),
    }
    if export_environment != contract.get("exportEnvironment"):
        raise RuntimeError(
            "Basic export environment does not match the versioned contract: "
            f"expected={contract.get('exportEnvironment')} actual={export_environment}"
        )
    maximum_allowed_error = float(
        contract.get("validation", {}).get("pytorchOnnxCpuMaxAbsMax", -1)
    )
    if maximum_allowed_error < 0 or maximum_error > maximum_allowed_error:
        raise RuntimeError(
            "BiRefNet PyTorch/ONNX CPU comparison exceeds the versioned contract: "
            f"maxAbs={maximum_error} limit={maximum_allowed_error}"
        )
    manifest = {
        "schemaVersion": contract["schemaVersion"],
        "artifactPolicy": contract["artifactPolicy"],
        "contractSha256": sha256_file(contract_path),
        "exportEnvironment": export_environment,
        "input": contract["input"],
        "license": contract["license"],
        "licenseFile": contract["licenseFile"],
        "licenseSha256": contract["licenseSha256"],
        "modelId": contract["modelId"],
        "nativeDeformConvLayers": replaced,
        "onnxFile": contract["onnxFile"],
        "onnxRuntime": contract["onnxRuntime"],
        "onnxSha256": output_hash,
        "opset": contract["opset"],
        "output": contract["output"],
        "precision": contract["precision"],
        "pytorchOnnxCpuMaxAbs": maximum_error,
        "requirementsSha256": sha256_file(requirements_path),
        "revision": contract["revision"],
        "selfTest": {
            **contract["selfTest"],
            "sha256": self_test_hash,
        },
        "sourceFile": contract["sourceFile"],
        "sourceFiles": {
            name: {"sha256": SOURCE_FILES[name]}
            for name in sorted(source_files)
        },
        "sourceSha256": contract["sourceSha256"],
    }
    shutil.copyfile(license_path, output.with_name(contract["licenseFile"]))
    manifest_path = output.with_suffix(".manifest.json")
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    output.with_suffix(output.suffix + ".sha256").write_text(
        f"{output_hash}  {output.name}\n", encoding="ascii"
    )
    print(f"Exported {output} ({output.stat().st_size} bytes)")
    print(f"SHA-256 {output_hash}")
    print(f"PyTorch/ONNX CPU maxAbs {maximum_error}")
    print(f"Self-test SHA-256 {self_test_hash}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).resolve().parent.parent
        / "release"
        / "models"
        / "birefnet-lite-matting.onnx",
    )
    parser.add_argument(
        "--source-dir",
        type=Path,
        help="Offline Hugging Face snapshot directory for the pinned revision.",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=Path(__file__).resolve().parent.parent
        / "Temp"
        / "UltraModelExport"
        / "cache",
    )
    parser.add_argument(
        "--contract",
        type=Path,
        default=Path(__file__).resolve().parents[2]
        / "RotoWeaveContracts"
        / "basic-assets.json",
    )
    parser.add_argument(
        "--license",
        type=Path,
        default=Path(__file__).resolve().parent.parent
        / "licenses"
        / "LICENSE-BiRefNet.txt",
    )
    parser.add_argument(
        "--requirements",
        type=Path,
        default=Path(__file__).resolve().parent.parent
        / "requirements-basic-export-lock.txt",
    )
    args = parser.parse_args()
    export(
        args.output,
        args.source_dir,
        args.cache_dir,
        args.contract,
        args.license,
        args.requirements,
    )
