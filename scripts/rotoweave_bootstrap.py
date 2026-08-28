from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import http.client
import json
import locale
import math
import os
import platform
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable


SCHEMA_VERSION = 2
SOURCE_SCHEMA_VERSION = 1
TREE_ALGORITHM = "sha256-tree-v1"
MANIFEST_NAME = "RotoWeave-DEPLOYMENT.json"
LEGACY_MANIFEST_NAME = "AIFrameTools-DEPLOYMENT.json"
RETIRED_MANIFEST_NAME = "AIFrameTools-ARTIFACTS.json"
PLATFORM_ID = "windows-x64"
MAX_ZIP_RATIO = 200
MAX_ZIP_ENTRIES = 500_000
COMPONENT_DOWNLOAD_ATTEMPTS = 4
DOWNLOAD_CHUNK_BYTES = 1024 * 1024
SEGMENT_TARGET_BYTES = 32 * 1024 * 1024
RUNTIME_HEARTBEAT_SECONDS = 15.0
MINIMUM_NODE = (22, 13, 0)
ROLE_COMPONENTS = {
    "client": (),
    "server": ("server-runtimes",),
    "all": ("server-runtimes",),
}
COMPONENT_TARGETS = {
    "client-basic": "RotoWeaveModels/application/basic",
    "server-runtimes": "RotoWeaveServer/server-runtimes",
    "server-models": "RotoWeaveModels/library",
}
ROLE_ENVIRONMENTS = {
    "client": ("client-python", "client-node"),
    "server": ("server-python", "server-node"),
    "all": ("client-python", "client-node", "server-python", "server-node"),
}
ENVIRONMENT_INPUTS = {
    "client-python": ("RotoWeaveClient/requirements-win-lock.txt", "RotoWeaveClient/.venv/Scripts/python.exe"),
    "client-node": ("RotoWeaveClient/package-lock.json", "RotoWeaveClient"),
    "server-python": ("RotoWeaveServer/requirements-win-lock.txt", "RotoWeaveServer/.venv/Scripts/python.exe"),
    "server-node": ("RotoWeaveServer/server-admin/package-lock.json", "RotoWeaveServer/server-admin"),
}
COMMON_COMPATIBILITY_INPUTS = (
    "RotoWeaveContracts/product.json",
    "RotoWeaveContracts/deployment-protocol.json",
    "RotoWeaveContracts/build-requirements-lock.txt",
)
ROLE_COMPATIBILITY_INPUTS = {
    "client": (
        "RotoWeaveContracts/basic-assets.json",
        "RotoWeaveClient/requirements-win-lock.txt",
        "RotoWeaveClient/requirements-basic-export-lock.txt",
        "RotoWeaveClient/package-lock.json",
    ),
    "server": (
        "RotoWeaveContracts/contracts/model_recipe.py",
        "RotoWeaveContracts/contracts/model_runtime_recipe.py",
        "RotoWeaveContracts/server-runtime-sources.json",
        "RotoWeaveServer/requirements-win-lock.txt",
        "RotoWeaveServer/requirements-high-runtime-lock.txt",
        "RotoWeaveServer/requirements-ultra-overlay-lock.txt",
        "RotoWeaveServer/server-admin/package-lock.json",
    ),
}
ROLE_COMPATIBILITY_INPUTS["all"] = tuple(
    dict.fromkeys((*ROLE_COMPATIBILITY_INPUTS["client"], *ROLE_COMPATIBILITY_INPUTS["server"]))
)
ProgressCallback = Callable[[str, float, str, dict[str, Any] | None], None]


class BootstrapError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class CheckResult:
    key: str
    status: str
    detail: str


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_file_with_progress(
    path: Path,
    *,
    progress: ProgressCallback | None,
    stage: str,
    progress_start: float,
    progress_end: float,
    source_id: str,
) -> str:
    total = path.stat().st_size
    hashed = 0
    started = time.monotonic()
    digest = hashlib.sha256()
    _emit(
        progress,
        stage,
        progress_start,
        f"正在校验缓存 {source_id}",
        {"id": source_id, "downloadedBytes": 0, "expectedBytes": total, "force": True},
    )
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
            hashed += len(chunk)
            elapsed = max(0.001, time.monotonic() - started)
            speed = hashed / elapsed
            _emit(
                progress,
                stage,
                progress_start + (progress_end - progress_start) * (hashed / max(1, total)),
                f"正在校验缓存 {source_id}",
                {
                    "id": source_id,
                    "downloadedBytes": hashed,
                    "expectedBytes": total,
                    "bytesPerSecond": speed,
                    "etaSeconds": max(0.0, total - hashed) / speed if speed > 0 else None,
                },
            )
    _emit(
        progress,
        stage,
        progress_end,
        f"缓存校验完成 {source_id}",
        {"id": source_id, "downloadedBytes": total, "expectedBytes": total, "force": True},
    )
    return digest.hexdigest()


def _is_reparse(path: Path) -> bool:
    file_attributes = int(getattr(path.lstat(), "st_file_attributes", 0))
    return bool(file_attributes & int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)))


def _assert_regular_tree(root: Path) -> Path:
    resolved = root.resolve(strict=True)
    if not resolved.is_dir() or resolved.is_symlink() or _is_reparse(resolved):
        raise BootstrapError(f"资产根不是普通目录: {root}")
    for current, directories, files in os.walk(resolved, followlinks=False):
        current_path = Path(current)
        for name in directories:
            child = current_path / name
            if child.is_symlink() or _is_reparse(child):
                raise BootstrapError(f"资产目录包含链接或重解析点: {child}")
        for name in files:
            child = current_path / name
            if child.is_symlink() or _is_reparse(child) or not child.is_file():
                raise BootstrapError(f"资产目录包含非普通文件: {child}")
    return resolved


def tree_summary(root: Path) -> dict[str, Any]:
    resolved = _assert_regular_tree(root)
    entries: list[tuple[str, Path]] = []
    for current, _, files in os.walk(resolved, followlinks=False):
        current_path = Path(current)
        for name in files:
            path = current_path / name
            entries.append((path.relative_to(resolved).as_posix(), path))
    entries.sort(key=lambda item: item[0])
    tree_digest = hashlib.sha256()
    total_bytes = 0
    for relative, path in entries:
        size = path.stat().st_size
        file_digest = _sha256_file(path)
        tree_digest.update(f"{relative}\0{size}\0{file_digest}\n".encode("utf-8"))
        total_bytes += size
    return {
        "algorithm": TREE_ALGORITHM,
        "fileCount": len(entries),
        "bytes": total_bytes,
        "sha256": tree_digest.hexdigest(),
    }


def tree_size(root: Path) -> tuple[int, int]:
    """Count a regular tree without hashing large payloads; used only for UI estimates."""
    resolved = _assert_regular_tree(root)
    count = 0
    total = 0
    for current, _, files in os.walk(resolved, followlinks=False):
        current_path = Path(current)
        for name in files:
            count += 1
            total += (current_path / name).stat().st_size
    return count, total


def _tree_summary_without(root: Path, excluded_names: set[str]) -> dict[str, Any]:
    resolved = _assert_regular_tree(root)
    entries: list[tuple[str, Path]] = []
    for current, _, files in os.walk(resolved, followlinks=False):
        current_path = Path(current)
        for name in files:
            path = current_path / name
            relative = path.relative_to(resolved).as_posix()
            if relative not in excluded_names:
                entries.append((relative, path))
    entries.sort(key=lambda item: item[0])
    digest = hashlib.sha256()
    total = 0
    for relative, path in entries:
        size = path.stat().st_size
        digest.update(f"{relative}\0{size}\0{_sha256_file(path)}\n".encode("utf-8"))
        total += size
    return {"algorithm": TREE_ALGORITHM, "fileCount": len(entries), "bytes": total, "sha256": digest.hexdigest()}


def _safe_target(root: Path, relative: str) -> Path:
    normalized = relative.replace("\\", "/")
    if normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized):
        raise BootstrapError(f"清单包含绝对路径: {relative}")
    target = (root / normalized).resolve(strict=False)
    try:
        target.relative_to(root.resolve(strict=False))
    except ValueError as exc:
        raise BootstrapError(f"清单路径越界: {relative}") from exc
    return target


def _load_product_version(project_root: Path) -> str:
    product = json.loads((project_root / "RotoWeaveContracts" / "product.json").read_text(encoding="utf-8"))
    return str(product["version"])


def _load_contracts(project_root: Path) -> tuple[Any, Any]:
    contracts_root = str((project_root / "RotoWeaveContracts").resolve())
    if contracts_root not in sys.path:
        sys.path.insert(0, contracts_root)
    from contracts.model_recipe import ASSETS
    from contracts.model_runtime_recipe import runtime_recipe

    return ASSETS, runtime_recipe


def _basic_contract_error(project_root: Path, manifest: dict[str, Any]) -> str | None:
    contract_path = project_root / "RotoWeaveContracts" / "basic-assets.json"
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    if manifest.get("contractSha256") != _sha256_file(contract_path):
        return "Basic 本机 Manifest 引用的版本化契约已变化"
    requirements_path = project_root / "RotoWeaveClient" / "requirements-basic-export-lock.txt"
    if manifest.get("requirementsSha256") != _sha256_file(requirements_path):
        return "Basic 本机 Manifest 引用的导出依赖锁已变化"
    exact_keys = (
        "schemaVersion",
        "artifactPolicy",
        "exportEnvironment",
        "input",
        "license",
        "licenseFile",
        "licenseSha256",
        "modelId",
        "nativeDeformConvLayers",
        "onnxFile",
        "onnxRuntime",
        "opset",
        "output",
        "precision",
        "revision",
        "sourceFile",
        "sourceFiles",
        "sourceSha256",
    )
    for key in exact_keys:
        if manifest.get(key) != contract.get(key):
            return f"Basic 本机 Manifest 字段与版本化契约不匹配: {key}"
    expected_self_test = contract.get("selfTest")
    actual_self_test = manifest.get("selfTest")
    if not isinstance(expected_self_test, dict) or not isinstance(actual_self_test, dict):
        return "Basic 自检契约缺失"
    for key, value in expected_self_test.items():
        if actual_self_test.get(key) != value:
            return f"Basic 自检字段与版本化契约不匹配: {key}"
    digest_pattern = re.compile(r"[0-9a-f]{64}")
    if not digest_pattern.fullmatch(str(manifest.get("onnxSha256") or "")):
        return "Basic ONNX 本机 SHA-256 无效"
    if not digest_pattern.fullmatch(str(actual_self_test.get("sha256") or "")):
        return "Basic 自检文件本机 SHA-256 无效"
    maximum = float(contract.get("validation", {}).get("pytorchOnnxCpuMaxAbsMax", -1))
    observed = float(manifest.get("pytorchOnnxCpuMaxAbs", float("inf")))
    if not math.isfinite(maximum) or not math.isfinite(observed) or maximum < 0 or observed < 0 or observed > maximum:
        return f"Basic PyTorch/ONNX 数值误差超限: maxAbs={observed} limit={maximum}"
    return None


def _basic_asset_path(root: Path, filename: Any, label: str) -> Path:
    value = str(filename or "")
    if not value or Path(value).name != value or Path(value).is_absolute():
        raise BootstrapError(f"Basic {label} 文件名无效: {value!r}")
    return root / value


def _check_basic_root(project_root: Path, root: Path, *, full_hash: bool) -> CheckResult:
    manifest_path = root / "birefnet-lite-matting.manifest.json"
    try:
        _assert_regular_tree(root)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        contract_error = _basic_contract_error(project_root, manifest)
        if contract_error:
            return CheckResult("client-basic", "invalid", contract_error)
        onnx = _basic_asset_path(root, manifest["onnxFile"], "ONNX")
        self_test = _basic_asset_path(root, manifest["selfTest"]["file"], "自检")
        license_file = _basic_asset_path(root, manifest["licenseFile"], "许可")
        required = (onnx, self_test, license_file)
        missing = [str(path) for path in required if not path.is_file()]
        if missing:
            return CheckResult("client-basic", "missing", "缺少: " + ", ".join(missing))
        if full_hash:
            expected = {
                onnx: str(manifest["onnxSha256"]),
                self_test: str(manifest["selfTest"]["sha256"]),
                license_file: str(manifest["licenseSha256"]),
            }
            for path, digest in expected.items():
                actual = _sha256_file(path)
                if actual != digest:
                    return CheckResult("client-basic", "invalid", f"SHA-256 不匹配: {path}")
        return CheckResult("client-basic", "ready", f"Basic 模型完整 ({onnx.stat().st_size:,} bytes)")
    except FileNotFoundError:
        return CheckResult("client-basic", "missing", f"未安装 Basic 模型: {root}")
    except (BootstrapError, KeyError, TypeError, ValueError, json.JSONDecodeError, OSError) as exc:
        return CheckResult("client-basic", "invalid", f"Basic 模型清单无效: {exc}")


def check_basic(project_root: Path, full_hash: bool = False) -> CheckResult:
    return _check_basic_root(
        project_root,
        project_root / COMPONENT_TARGETS["client-basic"],
        full_hash=full_hash,
    )


def check_server_models(project_root: Path, full_hash: bool = False) -> CheckResult:
    root = project_root / COMPONENT_TARGETS["server-models"]
    try:
        _assert_regular_tree(root)
        assets, _ = _load_contracts(project_root)
        failures: list[str] = []
        for asset in assets:
            path = root / asset.filename
            if not path.is_file():
                failures.append(f"missing:{asset.filename}")
                continue
            if path.stat().st_size != asset.bytes:
                failures.append(f"bytes:{asset.filename}")
                continue
            if full_hash and _sha256_file(path) != asset.sha256:
                failures.append(f"sha256:{asset.filename}")
        if failures:
            return CheckResult("server-models", "missing", "五模型未就绪: " + ", ".join(failures))
        return CheckResult("server-models", "ready", f"五个独立模型完整: {root}")
    except (BootstrapError, ImportError, OSError, KeyError) as exc:
        return CheckResult("server-models", "invalid", f"模型 Recipe 校验失败: {exc}")


def _is_forbidden_runtime_weight(path: Path, runtime_root: Path) -> bool:
    suffix = path.suffix.casefold()
    if suffix in {".pt", ".safetensors", ".ckpt"}:
        return True
    if suffix != ".pth":
        return False
    try:
        relative_parts = [part.casefold() for part in path.relative_to(runtime_root).parts]
        site_index = next(
            index
            for index in range(len(relative_parts) - 2)
            if relative_parts[index:index + 3] == ["runtime", "lib", "site-packages"]
        )
        if site_index >= 1 and path.stat().st_size <= 64 * 1024:
            payload = path.read_bytes()
            payload.decode("utf-8")
            return b"\0" in payload
    except (FileNotFoundError, OSError, UnicodeDecodeError, ValueError, StopIteration):
        pass
    return True


def check_server_runtimes(project_root: Path) -> CheckResult:
    root = project_root / COMPONENT_TARGETS["server-runtimes"]
    try:
        _assert_regular_tree(root)
        _, runtime_recipe = _load_contracts(project_root)
        source_contract = load_server_runtime_source_contract(project_root)
        sources_by_id = {str(item["id"]): item for item in source_contract["sources"]}
        for profile in ("high", "ultra"):
            expected = runtime_recipe(profile)
            manifest_path = root / profile / "runtime-manifest.json"
            actual = json.loads(manifest_path.read_text(encoding="utf-8"))
            if actual != expected:
                return CheckResult("server-runtimes", "invalid", f"{profile} 运行时清单与当前契约不一致")
            python = root / profile / str(expected["pythonRelativePath"])
            if not python.is_file():
                return CheckResult("server-runtimes", "missing", f"缺少 {profile} 固定 Python: {python}")
            receipt = json.loads((root / profile / "runtime-build.json").read_text(encoding="utf-8"))
            profile_contract = source_contract["profiles"][profile]
            if (
                receipt.get("schemaVersion") != 1
                or receipt.get("profile") != profile
                or receipt.get("runtimeDigest") != expected["digest"]
                or receipt.get("requirementsSha256") != expected["requirementsSha256"]
                or receipt.get("runtimeSourceContractSha256") != expected["runtimeSourceContractSha256"]
            ):
                return CheckResult("server-runtimes", "invalid", f"{profile} 运行时生成回执与当前契约不一致")
            pth = (root / profile / "runtime" / "python310._pth").read_text(encoding="ascii").splitlines()
            required_pth = ["Lib", "python310.zip", ".", "Lib\\site-packages"]
            expected_pth = [*required_pth]
            if profile == "ultra":
                expected_pth.append("..\\..\\high\\runtime\\Lib\\site-packages")
            expected_pth.extend(str(item) for item in source_contract["projectSearchPaths"])
            expected_pth.append("import site")
            if pth != expected_pth:
                return CheckResult("server-runtimes", "invalid", f"{profile} embedded Python 搜索路径无效")
            if profile == "high" and "..\\..\\high\\runtime\\Lib\\site-packages" in pth:
                return CheckResult("server-runtimes", "invalid", "High 运行时意外继承其他环境")
            if profile == "ultra" and "..\\..\\high\\runtime\\Lib\\site-packages" not in pth:
                return CheckResult("server-runtimes", "invalid", "Ultra overlay 未按相对路径只读继承 High")
            for source in source_contract["standardLibrarySources"]:
                relative = Path(str(source["targetPath"]).replace("/", os.sep))
                target = root / profile / "runtime" / relative
                if (
                    not target.is_file()
                    or target.is_symlink()
                    or target.stat().st_size != int(source["bytes"])
                    or _sha256_file(target) != str(source["sha256"])
                ):
                    return CheckResult(
                        "server-runtimes",
                        "invalid",
                        f"{profile} 标准库源码投影无效: {source['id']}",
                    )
            for source_id in profile_contract["sources"]:
                source = sources_by_id[str(source_id)]
                actual_tree = tree_summary(root / profile / "sources" / str(source["targetPath"]))
                if actual_tree != source["tree"]:
                    return CheckResult("server-runtimes", "invalid", f"{profile} 固定源码树不匹配: {source_id}")
            probe = _probe_server_runtime(root, profile)
            if probe["python"] != expected["pythonVersion"] or probe["torch"] != expected["torch"] or probe["cuda"] != expected["cuda"]:
                return CheckResult("server-runtimes", "invalid", f"{profile} Python/PyTorch/CUDA 版本不匹配")
            if profile == "high" and probe.get("inheritsHigh"):
                return CheckResult("server-runtimes", "invalid", "High 运行时不得继承其他 Profile")
            if profile == "ultra" and not probe.get("inheritsHigh"):
                return CheckResult("server-runtimes", "invalid", "Ultra overlay 未从 High 加载公共 PyTorch")
        forbidden = [
            path for path in root.rglob("*")
            if path.is_file() and _is_forbidden_runtime_weight(path, root)
        ]
        if forbidden:
            return CheckResult("server-runtimes", "invalid", f"固定运行时混入模型权重: {forbidden[0]}")
        return CheckResult("server-runtimes", "ready", "High/Ultra 固定运行时完整")
    except FileNotFoundError as exc:
        return CheckResult("server-runtimes", "missing", f"固定运行时未安装: {exc.filename}")
    except (BootstrapError, ImportError, OSError, KeyError, json.JSONDecodeError, subprocess.SubprocessError) as exc:
        return CheckResult("server-runtimes", "invalid", f"固定运行时校验失败: {exc}")


def check_environments(project_root: Path, role: str) -> list[CheckResult]:
    results: list[CheckResult] = []
    if role in {"client", "all"}:
        required = (
            project_root / "RotoWeaveClient" / ".venv" / "Scripts" / "python.exe",
            project_root / "RotoWeaveClient" / "node_modules" / ".bin" / "vite.cmd",
        )
        missing = [str(path) for path in required if not path.is_file()]
        results.append(CheckResult("client-environment", "ready" if not missing else "missing", "Client Python/Node 环境完整" if not missing else "缺少: " + ", ".join(missing)))
    if role in {"server", "all"}:
        required = (
            project_root / "RotoWeaveServer" / ".venv" / "Scripts" / "python.exe",
            project_root / "RotoWeaveServer" / "server-admin" / "node_modules" / ".bin" / "vite.cmd",
        )
        missing = [str(path) for path in required if not path.is_file()]
        results.append(CheckResult("server-environment", "ready" if not missing else "missing", "Server Python/Node 环境完整" if not missing else "缺少: " + ", ".join(missing)))
    return results


def _decode_native_output(value: bytes | str | None) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError:
        native_encoding = locale.getencoding() or "utf-8"
        try:
            return value.decode(native_encoding)
        except (LookupError, UnicodeDecodeError):
            return value.decode("utf-8", errors="replace")


def _command_version(command: str, arguments: Iterable[str]) -> str | None:
    executable = shutil.which(command)
    if not executable:
        return None
    completed = subprocess.run([executable, *arguments], capture_output=True, text=False, check=False, timeout=15)
    if completed.returncode != 0:
        return None
    return _decode_native_output(completed.stdout).strip() or _decode_native_output(completed.stderr).strip()


def check_host(project_root: Path) -> list[CheckResult]:
    machine = platform.machine().casefold()
    architecture_ready = machine in {"amd64", "x86_64"}
    results = [CheckResult("windows", "ready" if os.name == "nt" else "invalid", "Windows 主机" if os.name == "nt" else "仅支持 Windows")]
    results.append(CheckResult("architecture", "ready" if architecture_ready else "invalid", f"主机架构 {platform.machine()}（要求 x64）"))
    python_ready = sys.version_info[:2] == (3, 12)
    results.append(CheckResult("python", "ready" if python_ready else "invalid", f"Python {sys.version.split()[0]}（要求 3.12.x）"))
    node_raw = _command_version("node.exe", ("--version",)) or _command_version("node", ("--version",))
    node_match = re.search(r"(\d+)\.(\d+)\.(\d+)", node_raw or "")
    node_version = tuple(int(item) for item in node_match.groups()) if node_match else None
    node_ready = bool(node_version and node_version >= MINIMUM_NODE)
    results.append(CheckResult("node", "ready" if node_ready else "missing", f"Node.js {node_raw or '未找到'}（要求 >=22.13.0）"))
    npm_raw = _command_version("npm.cmd", ("--version",)) or _command_version("npm", ("--version",))
    results.append(CheckResult("npm", "ready" if npm_raw else "missing", f"npm {npm_raw or '未找到'}"))
    vc_ready = False
    if os.name == "nt":
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64") as key:
                vc_ready = int(winreg.QueryValueEx(key, "Installed")[0]) == 1
        except (OSError, ImportError, ValueError):
            vc_ready = False
    results.append(CheckResult("vc-runtime", "ready" if vc_ready else "missing", "Microsoft Visual C++ x64 runtime" if vc_ready else "未检测到 Microsoft Visual C++ x64 runtime"))
    try:
        version = _load_product_version(project_root)
        product_ready = version == "4.0.0"
        results.append(CheckResult("contracts", "ready" if product_ready else "invalid", f"RotoWeaveContracts product {version}"))
    except (FileNotFoundError, KeyError, json.JSONDecodeError) as exc:
        results.append(CheckResult("contracts", "missing", f"公共契约不可用: {exc}"))
    return results


def collect_checks(project_root: Path, role: str, *, full_hash: bool, skip_environments: bool, strict_profiles: bool) -> list[CheckResult]:
    results = check_host(project_root)
    if role in {"client", "all"}:
        results.append(check_basic(project_root, full_hash=full_hash))
    if role in {"server", "all"}:
        results.append(check_server_runtimes(project_root))
        if strict_profiles:
            results.append(check_server_models(project_root, full_hash=full_hash))
    if not skip_environments:
        results.extend(check_environments(project_root, role))
    return results


def _component_ready(project_root: Path, component: str, full_hash: bool = False) -> bool:
    if component == "client-basic":
        return check_basic(project_root, full_hash=full_hash).status == "ready"
    if component == "server-runtimes":
        return check_server_runtimes(project_root).status == "ready"
    if component == "server-models":
        return check_server_models(project_root, full_hash=full_hash).status == "ready"
    raise BootstrapError(f"未知资产组件: {component}")


def _canonical_json_sha256(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def compatibility_digest(project_root: Path, role: str = "all") -> str:
    if role not in ROLE_COMPATIBILITY_INPUTS:
        raise BootstrapError(f"未知兼容摘要角色: {role}")
    entries: list[dict[str, Any]] = []
    for relative in (*COMMON_COMPATIBILITY_INPUTS, *ROLE_COMPATIBILITY_INPUTS[role]):
        path = project_root / relative
        entries.append(
            {
                "path": relative,
                "sha256": _sha256_file(path) if path.is_file() else "missing",
            }
        )
    return _canonical_json_sha256(
        {
            "bundleSchemaVersion": SCHEMA_VERSION,
            "platform": PLATFORM_ID,
            "role": role,
            "inputs": entries,
        }
    )


def _source_revision(project_root: Path) -> str:
    try:
        completed = subprocess.run(
            ["svn", "info", "--show-item", "revision", str(project_root)],
            capture_output=True,
            text=False,
            check=False,
            timeout=20,
        )
        stdout = _decode_native_output(completed.stdout).strip()
        if completed.returncode == 0 and stdout.isdigit():
            return stdout
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        completed = subprocess.run(
            ["git", "-C", str(project_root), "rev-parse", "--short=12", "HEAD"],
            capture_output=True,
            text=False,
            check=False,
            timeout=20,
        )
        stdout = _decode_native_output(completed.stdout).strip()
        if completed.returncode == 0 and re.fullmatch(r"[0-9a-fA-F]{7,12}", stdout):
            return stdout.casefold()
    except (OSError, subprocess.SubprocessError):
        pass
    return "unknown"


def _emit(progress: ProgressCallback | None, stage: str, value: float, message: str, detail: dict[str, Any] | None = None) -> None:
    if progress is not None:
        progress(stage, max(0.0, min(1.0, value)), message, detail)


def _format_binary_bytes(value: int | float) -> str:
    amount = float(max(0, value))
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    for unit in units:
        if amount < 1024.0 or unit == units[-1]:
            return f"{amount:.0f} {unit}" if unit == "B" else f"{amount:.2f} {unit}"
        amount /= 1024.0
    return f"{amount:.2f} TiB"


def _format_duration(seconds: float) -> str:
    total = max(0, int(round(seconds)))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours:d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


class _ConsoleProgressPrinter:
    def __init__(self, *, stream: Any = None, minimum_interval: float = 1.0) -> None:
        self._stream = stream or sys.stderr
        self._minimum_interval = minimum_interval
        self._last_emitted: dict[str, tuple[float, int | None]] = {}
        self._lock = threading.Lock()

    def __call__(self, stage: str, value: float, message: str, detail: dict[str, Any] | None) -> None:
        payload = detail or {}
        source_id = str(payload.get("id") or "")
        key = f"{stage}:{source_id}"
        downloaded_value = payload.get("downloadedBytes")
        expected_value = payload.get("expectedBytes")
        downloaded = int(downloaded_value) if isinstance(downloaded_value, (int, float)) else None
        expected = int(expected_value) if isinstance(expected_value, (int, float)) else None
        percent_bucket = int(downloaded * 100 / expected) if downloaded is not None and expected and expected > 0 else None
        force = bool(payload.get("force")) or (downloaded is not None and expected is not None and downloaded >= expected)
        now = time.monotonic()
        with self._lock:
            previous = self._last_emitted.get(key)
            if (
                not force
                and previous is not None
                and now - previous[0] < self._minimum_interval
                and percent_bucket == previous[1]
            ):
                return
            self._last_emitted[key] = (now, percent_bucket)

            fields: list[str] = []
            if downloaded is not None:
                if expected is not None and expected > 0:
                    percent = downloaded * 100.0 / expected
                    fields.append(f"{_format_binary_bytes(downloaded)} / {_format_binary_bytes(expected)} ({percent:.1f}%)")
                else:
                    fields.append(_format_binary_bytes(downloaded))
            speed_value = payload.get("bytesPerSecond")
            if isinstance(speed_value, (int, float)) and speed_value > 0:
                fields.append(f"{_format_binary_bytes(speed_value)}/s")
            eta_value = payload.get("etaSeconds")
            if isinstance(eta_value, (int, float)) and eta_value >= 0:
                fields.append(f"预计剩余 {_format_duration(eta_value)}")
            completed_segments = payload.get("completedSegments")
            total_segments = payload.get("totalSegments")
            if isinstance(completed_segments, int) and isinstance(total_segments, int):
                fields.append(f"分段 {completed_segments}/{total_segments}")
            elapsed_value = payload.get("elapsedSeconds")
            if isinstance(elapsed_value, (int, float)) and elapsed_value >= 0:
                fields.append(f"已运行 {_format_duration(elapsed_value)}")
            retry_value = payload.get("retryInSeconds")
            if isinstance(retry_value, (int, float)) and retry_value > 0:
                fields.append(f"{retry_value:g} 秒后重试")
            suffix = f" | {' | '.join(fields)}" if fields else ""
            print(f"[Server Runtime {value * 100:5.1f}%] {message}{suffix}", file=self._stream, flush=True)


def _deployment_cache_root(project_root: Path, digest: str) -> Path:
    return project_root / "Temp" / "Codex" / "DeploymentCache" / "v2" / digest


def load_source_catalog(project_root: Path) -> dict[str, Any]:
    path = project_root / "RotoWeaveContracts" / "deployment-sources.json"
    try:
        catalog = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BootstrapError(f"受控部署源清单不可用: {path}: {exc}") from exc
    if catalog.get("schemaVersion") != SOURCE_SCHEMA_VERSION or catalog.get("platform") != PLATFORM_ID:
        raise BootstrapError("受控部署源清单 schema 或平台不受支持。")
    if not isinstance(catalog.get("bundleSources"), list) or not isinstance(catalog.get("componentSources"), list):
        raise BootstrapError("受控部署源清单缺少 bundleSources/componentSources。")
    return catalog


def _write_partial_metadata(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.writing-{uuid.uuid4().hex}")
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _download_verified_file(
    source: dict[str, Any],
    target: Path,
    *,
    progress: ProgressCallback | None = None,
    stage: str = "component-download",
    progress_start: float = 0.0,
    progress_end: float = 1.0,
) -> None:
    expected_bytes = int(source.get("bytes", -1))
    expected_sha256 = str(source.get("sha256") or "").casefold()
    source_id = str(source.get("id") or target.name)
    if expected_bytes < 0 or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise BootstrapError(f"受控来源缺少精确 bytes/SHA-256: {source_id}")
    url = str(source.get("url") or "")
    if urllib.parse.urlparse(url).scheme != "https":
        raise BootstrapError(f"公共组件来源只允许 HTTPS: {source_id}")
    if target.exists():
        raise BootstrapError(f"组件下载目标已存在，拒绝覆盖: {target}")
    credential = str(source.get("credentialEnvironment") or "")
    temporary = target.with_suffix(target.suffix + ".partial")
    metadata_path = target.with_suffix(target.suffix + ".partial.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    identity = {
        "schemaVersion": 1,
        "id": source_id,
        "url": url,
        "expectedBytes": expected_bytes,
        "expectedSha256": expected_sha256,
    }
    prior_etag = ""
    if temporary.exists():
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            if any(metadata.get(key) != value for key, value in identity.items()) or temporary.stat().st_size > expected_bytes:
                raise ValueError("partial identity mismatch")
            prior_etag = str(metadata.get("etag") or "")
        except (OSError, ValueError, json.JSONDecodeError):
            temporary.unlink(missing_ok=True)
            metadata_path.unlink(missing_ok=True)
    elif metadata_path.exists():
        metadata_path.unlink(missing_ok=True)

    failures: list[str] = []
    retryable_errors = (
        urllib.error.URLError,
        http.client.HTTPException,
        socket.timeout,
        TimeoutError,
        ConnectionError,
    )
    for attempt in range(1, COMPONENT_DOWNLOAD_ATTEMPTS + 1):
        offset = temporary.stat().st_size if temporary.exists() else 0
        if offset == expected_bytes:
            actual_sha256 = _sha256_file(temporary)
            if actual_sha256 == expected_sha256:
                os.replace(temporary, target)
                metadata_path.unlink(missing_ok=True)
                _emit(
                    progress,
                    stage,
                    progress_end,
                    f"{source_id} 下载与 SHA-256 校验完成",
                    {
                        "id": source_id,
                        "downloadedBytes": expected_bytes,
                        "expectedBytes": expected_bytes,
                        "force": True,
                    },
                )
                return
            failures.append(f"attempt={attempt} SHA-256 expected={expected_sha256} actual={actual_sha256}")
            temporary.unlink(missing_ok=True)
            metadata_path.unlink(missing_ok=True)
            offset = 0
            prior_etag = ""

        request = urllib.request.Request(url, headers={"User-Agent": "RotoWeave-Exporter/4.0"})
        if credential:
            token = os.getenv(credential)
            if not token:
                raise BootstrapError(f"下载 {source_id} 需要本机环境变量 {credential}。")
            request.add_header("Authorization", f"Bearer {token}")
        if offset:
            request.add_header("Range", f"bytes={offset}-")
            if prior_etag:
                request.add_header("If-Range", prior_etag)
        _emit(
            progress,
            stage,
            progress_start + (progress_end - progress_start) * (offset / max(1, expected_bytes)),
            f"正在下载 {source_id}（第 {attempt}/{COMPONENT_DOWNLOAD_ATTEMPTS} 次）",
            {
                "id": source_id,
                "attempt": attempt,
                "downloadedBytes": offset,
                "expectedBytes": expected_bytes,
                "force": True,
            },
        )
        failure_count_before_attempt = len(failures)
        attempt_started = time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                status = int(getattr(response, "status", 200))
                response_etag = str(response.headers.get("ETag") or "")
                content_encoding = str(response.headers.get("Content-Encoding") or "identity").casefold()
                if content_encoding not in {"", "identity"}:
                    raise BootstrapError(f"{source_id} 返回不受支持的 Content-Encoding={content_encoding}")
                append = offset > 0 and status == 206
                if status == 206:
                    content_range = str(response.headers.get("Content-Range") or "")
                    match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", content_range)
                    if not match or int(match.group(1)) != offset or int(match.group(3)) != expected_bytes:
                        raise BootstrapError(
                            f"{source_id} 断点响应无效: expected-start={offset} expected-total={expected_bytes} actual={content_range or '<missing>'}"
                        )
                    if prior_etag and response_etag and response_etag != prior_etag:
                        temporary.unlink(missing_ok=True)
                        metadata_path.unlink(missing_ok=True)
                        prior_etag = ""
                        raise BootstrapError(f"{source_id} 来源 ETag 已变化，已丢弃旧 partial，禁止拼接新旧字节")
                elif status == 200:
                    append = False
                    offset = 0
                    content_length = response.headers.get("Content-Length")
                    if content_length is not None and int(content_length) != expected_bytes:
                        raise BootstrapError(
                            f"{source_id} 远端长度与受控清单不匹配: expected={expected_bytes} remote={content_length}"
                        )
                else:
                    raise BootstrapError(f"{source_id} 返回不受支持的 HTTP status={status}")

                prior_etag = response_etag or prior_etag
                _write_partial_metadata(metadata_path, {**identity, "etag": prior_etag})
                downloaded = offset
                with temporary.open("ab" if append else "wb") as stream:
                    while True:
                        chunk = response.read(DOWNLOAD_CHUNK_BYTES)
                        if not chunk:
                            break
                        stream.write(chunk)
                        downloaded += len(chunk)
                        if downloaded > expected_bytes:
                            raise BootstrapError(
                                f"{source_id} 下载超过受控长度: expected={expected_bytes} actual>{downloaded}"
                            )
                        elapsed = max(0.001, time.monotonic() - attempt_started)
                        speed = max(0, downloaded - offset) / elapsed
                        _emit(
                            progress,
                            stage,
                            progress_start + (progress_end - progress_start) * (downloaded / max(1, expected_bytes)),
                            f"正在下载 {source_id}",
                            {
                                "id": source_id,
                                "attempt": attempt,
                                "downloadedBytes": downloaded,
                                "expectedBytes": expected_bytes,
                                "bytesPerSecond": speed,
                                "etaSeconds": max(0, expected_bytes - downloaded) / speed if speed > 0 else None,
                            },
                        )
        except retryable_errors as exc:
            failures.append(f"attempt={attempt} network={exc}")
        except OSError as exc:
            failures.append(f"attempt={attempt} io={exc}")
        except BootstrapError as exc:
            failures.append(f"attempt={attempt} validation={exc}")

        actual_bytes = temporary.stat().st_size if temporary.exists() else 0
        if actual_bytes == expected_bytes:
            actual_sha256 = _sha256_file(temporary)
            if actual_sha256 == expected_sha256:
                os.replace(temporary, target)
                metadata_path.unlink(missing_ok=True)
                _emit(
                    progress,
                    stage,
                    progress_end,
                    f"{source_id} 下载与 SHA-256 校验完成",
                    {
                        "id": source_id,
                        "downloadedBytes": expected_bytes,
                        "expectedBytes": expected_bytes,
                        "force": True,
                    },
                )
                return
            failures.append(f"attempt={attempt} SHA-256 expected={expected_sha256} actual={actual_sha256}")
            temporary.unlink(missing_ok=True)
            metadata_path.unlink(missing_ok=True)
            prior_etag = ""
        elif actual_bytes > expected_bytes:
            failures.append(f"attempt={attempt} bytes expected={expected_bytes} actual={actual_bytes}")
            temporary.unlink(missing_ok=True)
            metadata_path.unlink(missing_ok=True)
            prior_etag = ""
        else:
            if len(failures) == failure_count_before_attempt:
                failures.append(f"attempt={attempt} incomplete expected={expected_bytes} actual={actual_bytes}")
        if attempt < COMPONENT_DOWNLOAD_ATTEMPTS:
            retry_delay = min(2 ** (attempt - 1), 4)
            _emit(
                progress,
                stage,
                progress_start + (progress_end - progress_start) * (actual_bytes / max(1, expected_bytes)),
                f"{source_id} 下载中断，保留安全 partial",
                {
                    "id": source_id,
                    "attempt": attempt,
                    "downloadedBytes": actual_bytes,
                    "expectedBytes": expected_bytes,
                    "retryInSeconds": retry_delay,
                    "force": True,
                },
            )
            time.sleep(retry_delay)

    actual_bytes = temporary.stat().st_size if temporary.exists() else 0
    detail = failures[-1] if failures else "unknown"
    if temporary.exists():
        raise BootstrapError(
            f"{source_id} 下载在 {COMPONENT_DOWNLOAD_ATTEMPTS} 次尝试后仍未完成: expected={expected_bytes} actual={actual_bytes}; "
            f"partial 已保留用于下次续传: {temporary}; last={detail}"
        )
    metadata_path.unlink(missing_ok=True)
    raise BootstrapError(
        f"{source_id} 下载在 {COMPONENT_DOWNLOAD_ATTEMPTS} 次尝试后失败且无可续传 partial: expected={expected_bytes}; last={detail}"
    )


def _verify_authenticode(path: Path, expected_publisher: str) -> None:
    if not expected_publisher or os.name != "nt":
        return
    command = (
        "$s=Get-AuthenticodeSignature -LiteralPath $env:ROTOWEAVE_SIGNATURE_PATH;"
        "if($s.Status -ne 'Valid' -or $s.SignerCertificate.Subject -notlike ('*'+$env:ROTOWEAVE_SIGNATURE_PUBLISHER+'*')){exit 2}"
    )
    environment = os.environ.copy()
    # A parent pwsh session can inject a PowerShell 7-only PSModulePath into
    # Windows PowerShell 5.1, preventing Microsoft.PowerShell.Security load.
    for key in list(environment):
        if key.casefold() == "psmodulepath":
            environment.pop(key, None)
    system_root = Path(environment.get("SystemRoot") or environment.get("SYSTEMROOT") or r"C:\Windows")
    program_files = Path(environment.get("ProgramFiles") or environment.get("PROGRAMFILES") or r"C:\Program Files")
    environment["PSModulePath"] = os.pathsep.join(
        [
            str(system_root / "System32" / "WindowsPowerShell" / "v1.0" / "Modules"),
            str(program_files / "WindowsPowerShell" / "Modules"),
        ]
    )
    environment["ROTOWEAVE_SIGNATURE_PATH"] = str(path.resolve(strict=True))
    environment["ROTOWEAVE_SIGNATURE_PUBLISHER"] = expected_publisher
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
        check=False,
        capture_output=True,
        text=False,
        timeout=30,
        env=environment,
    )
    if completed.returncode != 0:
        detail = (
            _decode_native_output(completed.stderr).strip()
            or _decode_native_output(completed.stdout).strip()
            or f"exit={completed.returncode}"
        )
        raise BootstrapError(f"Authenticode 发布者验证失败: {path.name}: {detail}")


def prepare_toolchain_cache(project_root: Path, role: str, digest: str, progress: ProgressCallback | None = None) -> Path:
    root = _deployment_cache_root(project_root, digest) / "toolchains"
    catalog = load_source_catalog(project_root)
    selected = [
        item for item in catalog["componentSources"]
        if isinstance(item, dict) and item.get("kind") == "toolchain" and role in set(item.get("roles") or [])
    ]
    if not selected:
        raise BootstrapError("受控源清单未配置当前角色的主机工具链。")
    root.mkdir(parents=True, exist_ok=True)
    for index, source in enumerate(selected):
        filename = str(source.get("filename") or "")
        if not filename or Path(filename).name != filename:
            raise BootstrapError(f"工具链来源 filename 无效: {source.get('id')}")
        target = root / filename
        expected_sha = str(source.get("sha256") or "").casefold()
        if not target.is_file() or target.stat().st_size != int(source.get("bytes", -1)) or _sha256_file(target) != expected_sha:
            item_start = 0.12 + index / max(1, len(selected)) * 0.18
            item_end = 0.12 + (index + 1) / max(1, len(selected)) * 0.18
            _emit(progress, "toolchains", item_start, f"正在下载并验证 {source.get('id')}")
            target.unlink(missing_ok=True)
            _download_verified_file(
                source,
                target,
                progress=progress,
                stage="toolchains",
                progress_start=item_start,
                progress_end=item_end,
            )
        _verify_authenticode(target, str(source.get("authenticodePublisher") or ""))
    marker = root / "TOOLCHAINS-MANIFEST.json"
    marker.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "compatibilityDigest": digest,
                "role": role,
                "items": [
                    {
                        "id": item["id"],
                        "filename": item["filename"],
                        "bytes": item["bytes"],
                        "sha256": item["sha256"],
                        "authenticodePublisher": item.get("authenticodePublisher"),
                    }
                    for item in selected
                ],
            },
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )
    return root


def _environment_cache_paths(project_root: Path, digest: str, environment: str) -> tuple[Path, Path]:
    root = _deployment_cache_root(project_root, digest) / environment
    return root, root / "CACHE-MANIFEST.json"


def _environment_cache_ready(project_root: Path, digest: str, environment: str) -> bool:
    root, marker = _environment_cache_paths(project_root, digest, environment)
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return (
        payload.get("schemaVersion") == 1
        and payload.get("compatibilityDigest") == digest
        and payload.get("environment") == environment
        and root.is_dir()
    )


def _environment_cache_payload_valid(project_root: Path, digest: str, environment: str) -> bool:
    root, marker = _environment_cache_paths(project_root, digest, environment)
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
        return payload.get("payload") == _tree_summary_without(root, {"CACHE-MANIFEST.json"})
    except (OSError, json.JSONDecodeError, BootstrapError):
        return False


def _run_checked(command: list[str], *, cwd: Path | None = None, timeout: float = 7200.0) -> None:
    completed = subprocess.run(command, cwd=cwd, check=False, text=True, timeout=timeout)
    if completed.returncode != 0:
        raise BootstrapError(f"命令失败 ({completed.returncode}): {' '.join(command[:4])}")


def _prepare_python_cache(project_root: Path, environment: str, root: Path) -> None:
    lock_relative, python_relative = ENVIRONMENT_INPUTS[environment]
    python = project_root / python_relative
    lock = project_root / lock_relative
    if not python.is_file():
        raise BootstrapError(f"无法生成 {environment} 离线缓存，当前环境不存在: {python}")
    wheelhouse = root / "wheelhouse"
    wheelhouse.mkdir(parents=True, exist_ok=True)
    _run_checked(
        [
            str(python),
            "-m",
            "pip",
            "download",
            "--disable-pip-version-check",
            "--dest",
            str(wheelhouse),
            "-r",
            str(lock),
        ]
    )
    _run_checked(
        [
            str(python),
            "-m",
            "pip",
            "download",
            "--disable-pip-version-check",
            "--dest",
            str(wheelhouse),
            "-r",
            str(project_root / "RotoWeaveContracts" / "build-requirements-lock.txt"),
        ]
    )


def _prepare_node_cache(project_root: Path, environment: str, root: Path) -> None:
    lock_relative, source_relative = ENVIRONMENT_INPUTS[environment]
    source = project_root / source_relative
    npm = shutil.which("npm.cmd") or shutil.which("npm")
    if not npm:
        raise BootstrapError("生成 npm 离线缓存需要 npm 22.13.0 兼容工具链。")
    work = root / ".npm-work"
    work.mkdir(parents=True, exist_ok=True)
    shutil.copy2(project_root / lock_relative, work / "package-lock.json")
    shutil.copy2(source / "package.json", work / "package.json")
    cache = root / "npm-cache"
    _run_checked(
        [npm, "ci", "--cache", str(cache), "--prefer-online", "--no-audit", "--no-fund"],
        cwd=work,
    )
    shutil.rmtree(work, ignore_errors=True)


def prepare_environment_cache(
    project_root: Path,
    role: str,
    digest: str,
    progress: ProgressCallback | None = None,
) -> dict[str, Path]:
    selected = ROLE_ENVIRONMENTS[role]
    result: dict[str, Path] = {}
    for index, environment in enumerate(selected):
        root, marker = _environment_cache_paths(project_root, digest, environment)
        if not _environment_cache_ready(project_root, digest, environment) or not _environment_cache_payload_valid(project_root, digest, environment):
            _emit(progress, "environment-cache", 0.30 + index / max(1, len(selected)) * 0.10, f"正在准备 {environment} 离线缓存", {"environment": environment})
            stage = root.parent / f".{root.name}.staging-{uuid.uuid4().hex}"
            if stage.exists():
                shutil.rmtree(stage)
            stage.mkdir(parents=True)
            try:
                if environment.endswith("-python"):
                    _prepare_python_cache(project_root, environment, stage)
                else:
                    _prepare_node_cache(project_root, environment, stage)
                summary = tree_summary(stage)
                (stage / "CACHE-MANIFEST.json").write_text(
                    json.dumps(
                        {
                            "schemaVersion": 1,
                            "compatibilityDigest": digest,
                            "environment": environment,
                            "payload": summary,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                        indent=2,
                    ) + "\n",
                    encoding="utf-8",
                )
                if root.exists():
                    shutil.rmtree(root)
                os.replace(stage, root)
            except Exception:
                shutil.rmtree(stage, ignore_errors=True)
                raise
        result[environment] = root
    return result


def _component_status(project_root: Path, component: str) -> CheckResult:
    if component == "client-basic":
        return check_basic(project_root, full_hash=False)
    if component == "server-runtimes":
        return check_server_runtimes(project_root)
    if component == "server-models":
        return check_server_models(project_root, full_hash=False)
    raise BootstrapError(f"未知组件: {component}")


def deployment_plan(project_root: Path, role: str) -> dict[str, Any]:
    digest = compatibility_digest(project_root, role)
    components: list[dict[str, Any]] = []
    estimated = 0
    for component in ROLE_COMPONENTS[role]:
        status = _component_status(project_root, component)
        target = project_root / COMPONENT_TARGETS[component]
        size = tree_size(target)[1] if status.status == "ready" else 0
        estimated += int(size)
        components.append(
            {
                "id": component,
                "status": status.status,
                "detail": status.detail,
                "bytes": size,
                "relativePath": COMPONENT_TARGETS[component],
            }
        )
    environments = [
        {
            "id": item,
            "ready": _environment_cache_ready(project_root, digest, item),
        }
        for item in ROLE_ENVIRONMENTS[role]
    ]
    cache_root = _deployment_cache_root(project_root, digest)
    if cache_root.is_dir():
        for cache_name in (*ROLE_ENVIRONMENTS[role], "toolchains"):
            cache_path = cache_root / cache_name
            if cache_path.is_dir():
                try:
                    estimated += int(tree_size(cache_path)[1])
                except BootstrapError:
                    pass
    source_status = "missing"
    try:
        catalog = load_source_catalog(project_root)
        matching_bundles = [
            item for item in catalog["bundleSources"]
            if isinstance(item, dict)
            and role in set(item.get("roles") or [item.get("role")])
            and item.get("platform") == PLATFORM_ID
            and item.get("productVersion") == _load_product_version(project_root)
            and item.get("compatibilityDigest") == digest
        ]
        source_status = "bundle-configured" if matching_bundles else "component-only"
    except BootstrapError:
        pass
    return {
        "schemaVersion": SCHEMA_VERSION,
        "role": role,
        "platform": PLATFORM_ID,
        "productVersion": _load_product_version(project_root),
        "compatibilityDigest": digest,
        "sourceRevision": _source_revision(project_root),
        "components": components,
        "environments": environments,
        "ready": all(item["status"] == "ready" for item in components),
        "estimatedBytes": estimated,
        "requiresNetworkForCache": any(not item["ready"] for item in environments),
        "diskFreeBytes": shutil.disk_usage(project_root).free,
        "sourceStatus": source_status,
    }


def _zip_compression(path: Path) -> int:
    if path.suffix.casefold() in {".pt", ".pth", ".safetensors", ".ckpt", ".onnx", ".whl", ".zip", ".gz", ".xz", ".7z", ".png", ".jpg", ".jpeg"}:
        return zipfile.ZIP_STORED
    return zipfile.ZIP_DEFLATED


def _zip_tree(archive: zipfile.ZipFile, source: Path, prefix: str) -> None:
    resolved = _assert_regular_tree(source)
    entries: list[tuple[str, Path]] = []
    for current, _, files in os.walk(resolved, followlinks=False):
        current_path = Path(current)
        for name in files:
            path = current_path / name
            relative = path.relative_to(resolved).as_posix()
            entries.append((f"{prefix.rstrip('/')}/{relative}", path))
    for archive_name, path in sorted(entries, key=lambda item: item[0].casefold()):
        archive.write(path, archive_name, compress_type=_zip_compression(path), compresslevel=6)


def _bundle_name(product_version: str, role: str, digest: str) -> str:
    return f"RotoWeave-{product_version}-{role.capitalize()}-{PLATFORM_ID}-{digest[:12]}.zip"


def _bundle_licenses(project_root: Path, role: str, include_environment: bool) -> list[dict[str, Any]]:
    licenses: list[dict[str, Any]] = []
    catalog = (
        load_source_catalog(project_root)
        if role in {"server", "all"} or include_environment
        else {"componentSources": []}
    )
    if role in {"server", "all"}:
        runtime_contract = load_server_runtime_source_contract(project_root)
        licenses.append(
            {
                "component": "server-runtime-python",
                "id": runtime_contract["python"]["id"],
                "licenseId": runtime_contract["python"]["licenseId"],
                "sourceUrl": runtime_contract["python"]["url"],
            }
        )
        for source in runtime_contract["sources"]:
            licenses.append(
                {
                    "component": "server-runtime-source",
                    "id": source.get("id"),
                    "revision": source.get("revision"),
                    "licenseId": source.get("licenseId"),
                    "sourceUrl": source.get("url"),
                }
            )
        licenses.append(
            {
                "component": "server-runtimes",
                "licenseId": "mixed-runtime-dependencies",
                "notice": "Runtime files remain subject to their embedded package and source notices.",
            }
        )
    if include_environment:
        for item in catalog["componentSources"]:
            if isinstance(item, dict) and item.get("kind") == "toolchain" and role in set(item.get("roles") or []):
                licenses.append(
                    {
                        "component": "host-toolchains",
                        "id": item.get("id"),
                        "licenseId": item.get("licenseId"),
                        "sourceUrl": item.get("url"),
                    }
                )
    return licenses


def _write_export_record(output: Path, sha256: str, manifest: dict[str, Any]) -> Path | None:
    local_app_data = os.getenv("LOCALAPPDATA")
    if not local_app_data:
        return None
    root = Path(local_app_data) / "RotoWeave" / "deployment-bundle-exports"
    root.mkdir(parents=True, exist_ok=True)
    target = root / f"{manifest['bundleId']}.json"
    temporary = target.with_suffix(f".partial-{uuid.uuid4().hex}")
    temporary.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "bundleId": manifest["bundleId"],
                "role": manifest["role"],
                "productVersion": manifest["productVersion"],
                "platform": manifest["platform"],
                "compatibilityDigest": manifest["compatibilityDigest"],
                "sourceRevision": manifest["sourceRevision"],
                "outputPath": str(output),
                "bytes": output.stat().st_size,
                "sha256": sha256,
                "completedAtUtc": datetime.now(timezone.utc).isoformat(),
            },
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, target)
    return target


def export_bundle(
    project_root: Path,
    role: str,
    output_directory: Path,
    *,
    include_environment: bool = True,
    progress: ProgressCallback | None = None,
) -> tuple[Path, str, dict[str, Any]]:
    if platform.system().casefold() != "windows" and os.name != "nt":
        raise BootstrapError("部署包只支持在 Windows x64 工程主机生成。")
    plan = deployment_plan(project_root, role)
    if not plan["ready"]:
        missing = [item["id"] for item in plan["components"] if item["status"] != "ready"]
        raise BootstrapError("当前机器缺少或损坏所需组件: " + ", ".join(missing))
    project_resolved = project_root.resolve(strict=True)
    output_directory = output_directory.resolve(strict=False)
    try:
        output_directory.relative_to(project_resolved)
    except ValueError:
        pass
    else:
        raise BootstrapError("部署 ZIP 输出目录必须位于源码工作副本之外。")
    output_directory.mkdir(parents=True, exist_ok=True)
    if output_directory.is_symlink() or _is_reparse(output_directory):
        raise BootstrapError("部署 ZIP 输出目录不能是链接或重解析点。")
    output = output_directory / _bundle_name(plan["productVersion"], role, plan["compatibilityDigest"])
    if output.exists():
        raise BootstrapError(f"导出目标已存在，拒绝覆盖: {output}")
    _emit(progress, "preflight", 0.03, "正在完整校验部署资产")
    component_sources: dict[str, Path] = {}
    for component in ROLE_COMPONENTS[role]:
        source = project_root / COMPONENT_TARGETS[component]
        if not _component_ready(project_root, component, full_hash=True):
            raise BootstrapError(f"{component} 未通过完整 SHA-256 校验。")
        component_sources[component] = source
    environment_roots: dict[str, Path] = {}
    toolchain_root: Path | None = None
    if include_environment:
        toolchain_root = prepare_toolchain_cache(project_root, role, plan["compatibilityDigest"], progress)
        environment_roots = prepare_environment_cache(project_root, role, plan["compatibilityDigest"], progress)
    sources: dict[str, tuple[Path, str, str]] = {}
    components: dict[str, Any] = {}
    for component in ROLE_COMPONENTS[role]:
        source = component_sources[component]
        prefix = f"payload/assets/{component}"
        summary = tree_summary(source)
        sources[component] = (source, prefix, "asset")
        components[component] = {
            "kind": "asset",
            "relativePath": COMPONENT_TARGETS[component],
            "archivePrefix": prefix,
            **summary,
        }
    for environment, source in environment_roots.items():
        prefix = f"payload/environment/{environment}"
        summary = tree_summary(source)
        sources[environment] = (source, prefix, "environment")
        components[environment] = {
            "kind": "environment",
            "relativePath": None,
            "archivePrefix": prefix,
            **summary,
        }
    if toolchain_root is not None:
        prefix = "payload/toolchains"
        summary = tree_summary(toolchain_root)
        sources["host-toolchains"] = (toolchain_root, prefix, "toolchain")
        components["host-toolchains"] = {
            "kind": "toolchain",
            "relativePath": None,
            "archivePrefix": prefix,
            **summary,
        }
    manifest = {
        "schemaVersion": SCHEMA_VERSION,
        "bundleId": f"bundle-{uuid.uuid4().hex}",
        "productVersion": plan["productVersion"],
        "role": role,
        "platform": PLATFORM_ID,
        "compatibilityDigest": plan["compatibilityDigest"],
        "sourceRevision": plan["sourceRevision"],
        "createdAtUtc": datetime.now(timezone.utc).isoformat(),
        "treeAlgorithm": TREE_ALGORITHM,
        "components": components,
        "toolchainVersions": [
            item.get("id") for item in load_source_catalog(project_root)["componentSources"]
            if isinstance(item, dict) and item.get("kind") == "toolchain" and role in set(item.get("roles") or [])
        ] if include_environment else [],
        "licenses": _bundle_licenses(project_root, role, include_environment),
    }
    expected_payload = sum(int(item["bytes"]) for item in components.values())
    free = shutil.disk_usage(output_directory).free
    required = int(expected_payload * 1.08) + 512 * 1024 * 1024
    if free < required:
        raise BootstrapError(f"输出磁盘空间不足: required={required} free={free}")
    partial = output.with_name(f".{output.stem}.partial-{uuid.uuid4().hex}.zip")
    try:
        _emit(progress, "compress", 0.42, "正在写入 ZIP64 部署包", {"output": str(output)})
        with zipfile.ZipFile(partial, "w", allowZip64=True) as archive:
            archive.writestr(MANIFEST_NAME, json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n", compress_type=zipfile.ZIP_DEFLATED)
            total = max(1, len(sources))
            for index, (component, (source, prefix, _kind)) in enumerate(sources.items()):
                _emit(progress, "compress", 0.42 + (index / total) * 0.42, f"正在写入 {component}", {"component": component})
                _zip_tree(archive, source, prefix)
        _emit(progress, "verify", 0.88, "正在复核整包和 Manifest")
        inspected = inspect_bundle(project_root, partial, expected_role=role, full_hash=True)
        digest = _sha256_file(partial)
        os.replace(partial, output)
        record_path: Path | None = None
        try:
            record_path = _write_export_record(output, digest, manifest)
        except OSError as exc:
            inspected["exportRecordWarning"] = str(exc)
        _emit(progress, "completed", 1.0, "部署 ZIP 已完成", {"outputPath": str(output), "sha256": digest, "bytes": output.stat().st_size, "recordPath": str(record_path) if record_path else None})
        return output, digest, inspected
    except Exception:
        partial.unlink(missing_ok=True)
        raise


def _safe_zip_name(name: str) -> str:
    if "\\" in name or not name or name.startswith("/") or re.match(r"^[A-Za-z]:", name):
        raise BootstrapError(f"ZIP 包含非法成员路径: {name}")
    parts = name.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise BootstrapError(f"ZIP 包含越界或空路径成员: {name}")
    return "/".join(parts)


def _validate_zip_members(archive: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    infos = archive.infolist()
    if len(infos) > MAX_ZIP_ENTRIES:
        raise BootstrapError("ZIP 成员数量超过安全上限。")
    seen: set[str] = set()
    for info in infos:
        normalized = _safe_zip_name(info.filename.rstrip("/") if info.is_dir() else info.filename)
        folded = normalized.casefold()
        if folded in seen:
            raise BootstrapError(f"ZIP 包含大小写冲突或重复成员: {info.filename}")
        seen.add(folded)
        mode = (info.external_attr >> 16) & 0xFFFF
        if stat.S_ISLNK(mode):
            raise BootstrapError(f"ZIP 包含符号链接: {info.filename}")
        if info.file_size > 10 * 1024 * 1024 and info.compress_size and info.file_size / info.compress_size > MAX_ZIP_RATIO:
            raise BootstrapError(f"ZIP 成员压缩比异常: {info.filename}")
    return infos


def _read_bundle_manifest(archive: zipfile.ZipFile) -> dict[str, Any]:
    names = set(archive.namelist())
    available = [name for name in (MANIFEST_NAME, LEGACY_MANIFEST_NAME) if name in names]
    if len(available) > 1:
        raise BootstrapError("部署包同时包含 RotoWeave 与 AIFrameTools 4.0 清单，拒绝猜测。")
    if RETIRED_MANIFEST_NAME in names:
        raise BootstrapError("检测到旧 schema 1 目录交接清单，请在源机器重新导出 schema 2 ZIP。")
    if not available:
        raise BootstrapError(f"部署包缺少 {MANIFEST_NAME}")
    source_manifest_name = available[0]
    raw = archive.read(source_manifest_name)
    if len(raw) > 4 * 1024 * 1024:
        raise BootstrapError("部署包 Manifest 超过安全上限。")
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise BootstrapError(f"部署包 Manifest 包含重复字段: {key}")
            result[key] = value
        return result

    manifest = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=reject_duplicates)
    if manifest.get("schemaVersion") != SCHEMA_VERSION:
        raise BootstrapError("部署包 schemaVersion 不受支持。")
    expected_fields = {
        "schemaVersion", "bundleId", "productVersion", "role", "platform",
        "compatibilityDigest", "sourceRevision", "createdAtUtc", "treeAlgorithm",
        "components", "toolchainVersions", "licenses",
    }
    if set(manifest) != expected_fields:
        raise BootstrapError("部署包 Manifest 顶层字段不符合 schema 2。")
    if not re.fullmatch(r"bundle-[0-9a-f]{32}", str(manifest.get("bundleId") or "")):
        raise BootstrapError("部署包 bundleId 无效。")
    if not re.fullmatch(r"[0-9a-f]{64}", str(manifest.get("compatibilityDigest") or "")):
        raise BootstrapError("部署包兼容摘要格式无效。")
    if manifest.get("treeAlgorithm") != TREE_ALGORITHM:
        raise BootstrapError("部署包树哈希算法不受支持。")
    try:
        datetime.fromisoformat(str(manifest.get("createdAtUtc"))).astimezone(timezone.utc)
    except (TypeError, ValueError):
        raise BootstrapError("部署包创建时间无效。") from None
    if not isinstance(manifest.get("licenses"), list) or not all(isinstance(item, dict) for item in manifest["licenses"]):
        raise BootstrapError("部署包许可证清单无效。")
    if not isinstance(manifest.get("toolchainVersions"), list) or not all(isinstance(item, str) for item in manifest["toolchainVersions"]):
        raise BootstrapError("部署包工具链版本清单无效。")
    if not isinstance(manifest.get("components"), dict):
        raise BootstrapError("部署包 Manifest 缺少 components。")
    manifest["_sourceManifestName"] = source_manifest_name
    return manifest


def inspect_bundle(
    project_root: Path,
    bundle: Path,
    *,
    expected_role: str | None = None,
    full_hash: bool = False,
) -> dict[str, Any]:
    if not bundle.is_file() or bundle.suffix.casefold() != ".zip":
        raise BootstrapError(f"部署包不是 ZIP 文件: {bundle}")
    with zipfile.ZipFile(bundle, "r") as archive:
        infos = _validate_zip_members(archive)
        manifest = _read_bundle_manifest(archive)
        source_manifest_name = str(manifest.pop("_sourceManifestName"))
        if manifest.get("platform") != PLATFORM_ID:
            raise BootstrapError(f"部署包平台不匹配: {manifest.get('platform')}")
        if manifest.get("productVersion") != _load_product_version(project_root):
            raise BootstrapError("部署包产品版本不匹配。")
        role = str(manifest.get("role") or "")
        if role not in ROLE_COMPONENTS or (expected_role and role != expected_role):
            raise BootstrapError(f"部署包角色不匹配: {role} != {expected_role}")
        if manifest.get("compatibilityDigest") != compatibility_digest(project_root, role):
            raise BootstrapError("部署包与当前源码工作副本依赖/Recipe 不兼容。")
        by_name = {item.filename: item for item in infos if not item.is_dir()}
        required_assets = set(ROLE_COMPONENTS[role])
        allowed_components = required_assets | set(ROLE_ENVIRONMENTS[role]) | {"host-toolchains"}
        component_names = list(manifest["components"])
        if any(not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name) for name in component_names):
            raise BootstrapError("部署包 Manifest 包含非法组件标识。")
        if len({name.casefold() for name in component_names}) != len(component_names):
            raise BootstrapError("部署包 Manifest 包含大小写冲突组件。")
        unexpected_components = set(component_names) - allowed_components
        if unexpected_components:
            raise BootstrapError(f"部署包 Manifest 包含当前角色未声明组件: {sorted(unexpected_components)[0]}")
        declared_assets = {
            key for key, value in manifest["components"].items()
            if isinstance(value, dict) and value.get("kind") == "asset"
        }
        if not required_assets.issubset(declared_assets):
            raise BootstrapError("部署包缺少当前角色要求的资产组件。")
        claimed_names = {source_manifest_name}
        prefixes: set[str] = set()
        declared_total = 0
        for component, entry in manifest["components"].items():
            if not isinstance(entry, dict) or entry.get("algorithm") != TREE_ALGORITHM:
                raise BootstrapError(f"组件清单无效: {component}")
            raw_prefix = str(entry.get("archivePrefix") or "").rstrip("/")
            prefix = _safe_zip_name(raw_prefix) + "/"
            kind = entry.get("kind")
            expected_prefix = (
                f"payload/assets/{component}" if kind == "asset"
                else f"payload/environment/{component}" if kind == "environment"
                else "payload/toolchains" if kind == "toolchain"
                else None
            )
            if expected_prefix is None or raw_prefix != expected_prefix:
                raise BootstrapError(f"组件清单路径或类型不符合部署协议: {component}")
            if kind == "asset" and entry.get("relativePath") != COMPONENT_TARGETS.get(component):
                raise BootstrapError(f"资产目标目录不符合部署协议: {component}")
            if kind != "asset" and entry.get("relativePath") is not None:
                raise BootstrapError(f"环境/工具链组件不得声明工程目标目录: {component}")
            if prefix.casefold() in prefixes:
                raise BootstrapError(f"组件清单包含重复路径前缀: {component}")
            prefixes.add(prefix.casefold())
            matching = [item for name, item in by_name.items() if name.startswith(prefix)]
            actual_bytes = sum(item.file_size for item in matching)
            if len(matching) != int(entry.get("fileCount", -1)) or actual_bytes != int(entry.get("bytes", -1)):
                raise BootstrapError(f"ZIP 组件成员数量或字节数不匹配: {component}")
            declared_total += actual_bytes
            claimed_names.update(item.filename for item in matching)
            if full_hash:
                tree_digest = hashlib.sha256()
                for info in sorted(matching, key=lambda item: item.filename):
                    relative = info.filename[len(prefix):]
                    file_digest = hashlib.sha256()
                    with archive.open(info, "r") as stream:
                        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
                            file_digest.update(chunk)
                    tree_digest.update(f"{relative}\0{info.file_size}\0{file_digest.hexdigest()}\n".encode("utf-8"))
                if tree_digest.hexdigest() != entry.get("sha256"):
                    raise BootstrapError(f"ZIP 组件树 SHA-256 不匹配: {component}")
        undeclared = sorted(name for name in by_name if name not in claimed_names)
        if undeclared:
            raise BootstrapError(f"ZIP 存在 Manifest 未声明成员: {undeclared[0]}")
        return manifest


def _extract_component(archive: zipfile.ZipFile, entry: dict[str, Any], target: Path) -> None:
    prefix = str(entry["archivePrefix"]).rstrip("/") + "/"
    target.mkdir(parents=True)
    for info in archive.infolist():
        if info.is_dir() or not info.filename.startswith(prefix):
            continue
        relative = info.filename[len(prefix):]
        output = _safe_target(target, relative)
        output.parent.mkdir(parents=True, exist_ok=True)
        with archive.open(info, "r") as source, output.open("xb") as destination:
            shutil.copyfileobj(source, destination, length=8 * 1024 * 1024)


def import_bundle(
    project_root: Path,
    role: str,
    bundle: Path,
    *,
    expected_sha256: str | None = None,
    replace_invalid: bool = False,
) -> dict[str, Any]:
    if expected_sha256:
        actual = _sha256_file(bundle)
        if actual != expected_sha256.casefold():
            raise BootstrapError(f"部署 ZIP SHA-256 不匹配: expected={expected_sha256.casefold()} actual={actual}")
    manifest = inspect_bundle(project_root, bundle, expected_role=role, full_hash=True)
    required_free = int(sum(int(item.get("bytes", 0)) for item in manifest["components"].values()) * 1.10) + 512 * 1024 * 1024
    available_free = shutil.disk_usage(project_root).free
    if available_free < required_free:
        raise BootstrapError(f"部署磁盘空间不足: required={required_free} free={available_free}")
    selected = list(ROLE_COMPONENTS[role])
    stage_root = project_root / "Temp" / "Bootstrap" / f"bundle-{uuid.uuid4().hex}"
    backup_root = project_root / "Temp" / "BootstrapBackups" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    payload_root = project_root / "Temp" / "BootstrapPayloads" / str(manifest["bundleId"])
    installed: list[str] = []
    promoted: list[tuple[str, Path, Path | None]] = []
    try:
        with zipfile.ZipFile(bundle, "r") as archive:
            _validate_zip_members(archive)
            missing = [component for component in selected if not _component_ready(project_root, component, full_hash=False)]
            for component in missing:
                entry = manifest["components"].get(component)
                if not isinstance(entry, dict) or entry.get("kind") != "asset":
                    raise BootstrapError(f"部署包不包含所需资产组件: {component}")
                staged = stage_root / "assets" / component
                _extract_component(archive, entry, staged)
                summary = tree_summary(staged)
                for key in ("algorithm", "fileCount", "bytes", "sha256"):
                    if summary[key] != entry.get(key):
                        raise BootstrapError(f"{component} 解压校验失败: {key}")

            environment_entries = {
                key: value for key, value in manifest["components"].items()
                if isinstance(value, dict) and value.get("kind") == "environment"
            }
            toolchain_entries = {
                key: value for key, value in manifest["components"].items()
                if isinstance(value, dict) and value.get("kind") == "toolchain"
            }
            environment_stage = stage_root / "environment"
            for component, entry in environment_entries.items():
                target = environment_stage / component
                _extract_component(archive, entry, target)
                summary = tree_summary(target)
                for key in ("algorithm", "fileCount", "bytes", "sha256"):
                    if summary[key] != entry.get(key):
                        raise BootstrapError(f"{component} 环境缓存解压校验失败: {key}")
            for component, entry in toolchain_entries.items():
                target = environment_stage / "toolchains"
                _extract_component(archive, entry, target)
                summary = tree_summary(target)
                for key in ("algorithm", "fileCount", "bytes", "sha256"):
                    if summary[key] != entry.get(key):
                        raise BootstrapError(f"{component} 工具链解压校验失败: {key}")

            # Refuse every invalid existing target before moving any staged asset.
            for component in missing:
                target = _safe_target(project_root, COMPONENT_TARGETS[component])
                if target.exists() and not replace_invalid:
                    raise BootstrapError(f"目标已存在但未通过校验: {target}。确认修复时使用 -Repair。")

            for component in missing:
                staged = stage_root / "assets" / component
                target = _safe_target(project_root, COMPONENT_TARGETS[component])
                backup: Path | None = None
                if target.exists():
                    backup = backup_root / component
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(target), str(backup))
                target.parent.mkdir(parents=True, exist_ok=True)
                try:
                    shutil.move(str(staged), str(target))
                except Exception:
                    if backup and backup.exists() and not target.exists():
                        shutil.move(str(backup), str(target))
                    raise
                promoted.append((component, target, backup))
                if not _component_ready(project_root, component, full_hash=False):
                    raise BootstrapError(f"{component} 晋升后未通过产品校验。")
                installed.append(component)
            if environment_entries or toolchain_entries:
                payload_root.parent.mkdir(parents=True, exist_ok=True)
                if payload_root.exists():
                    shutil.rmtree(payload_root)
                os.replace(environment_stage, payload_root)
        return {
            "bundleId": manifest["bundleId"],
            "role": role,
            "installed": installed,
            "payloadRoot": str(payload_root) if payload_root.exists() else None,
            "compatibilityDigest": manifest["compatibilityDigest"],
        }
    except Exception:
        for _component, target, backup in reversed(promoted):
            if target.exists():
                if target.is_dir():
                    shutil.rmtree(target)
                else:
                    target.unlink()
            if backup and backup.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(backup), str(target))
        raise
    finally:
        shutil.rmtree(stage_root, ignore_errors=True)


def _run_basic_checked(command: list[str], label: str) -> None:
    # Keep stdout reserved for the bootstrap command's machine-readable JSON.
    # pip and the exporter are still streamed to the console through stderr.
    completed = subprocess.run(command, check=False, stdout=sys.stderr, stderr=sys.stderr)
    if completed.returncode != 0:
        raise BootstrapError(f"{label}失败 (exit={completed.returncode})")


def _basic_export_cache_root() -> Path:
    local_app_data = os.getenv("LOCALAPPDATA")
    if not local_app_data:
        raise BootstrapError("LOCALAPPDATA 不可用，无法建立 Basic 隔离导出缓存。")
    return Path(local_app_data) / "RotoWeave" / "bootstrap" / "basic-export"


def _prepare_basic_export_environment(project_root: Path, cache_root: Path) -> tuple[Path, str]:
    contract = json.loads(
        (project_root / "RotoWeaveContracts" / "basic-assets.json").read_text(encoding="utf-8")
    )
    required_python = str(contract["exportEnvironment"]["python"])
    actual_python = platform.python_version()
    if actual_python != required_python:
        raise BootstrapError(
            f"Basic 导出要求 Python {required_python}，当前为 {actual_python}。"
        )
    requirements = project_root / "RotoWeaveClient" / "requirements-basic-export-lock.txt"
    requirements_sha = _sha256_file(requirements)
    environment_root = cache_root / f"py312-{requirements_sha[:16]}"
    environment_python = environment_root / "venv" / "Scripts" / "python.exe"
    marker = environment_root / "ENVIRONMENT.json"
    expected_marker = {
        "schemaVersion": 1,
        "python": required_python,
        "requirementsSha256": requirements_sha,
    }
    if environment_python.is_file() and marker.is_file():
        try:
            if json.loads(marker.read_text(encoding="utf-8")) == expected_marker:
                return environment_python, requirements_sha
        except (OSError, json.JSONDecodeError):
            pass
    if environment_root.exists():
        shutil.rmtree(environment_root)
    environment_root.parent.mkdir(parents=True, exist_ok=True)
    stage = environment_root.parent / f".{environment_root.name}.staging-{uuid.uuid4().hex}"
    try:
        _run_basic_checked([sys.executable, "-m", "venv", str(stage / "venv")], "创建 Basic 隔离导出环境")
        stage_python = stage / "venv" / "Scripts" / "python.exe"
        _run_basic_checked(
            [
                str(stage_python),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--only-binary=:all:",
                "--no-deps",
                "--requirement",
                str(requirements),
            ],
            "安装 Basic 固定导出依赖",
        )
        _run_basic_checked([str(stage_python), "-m", "pip", "check"], "检查 Basic 导出依赖")
        marker_stage = stage / "ENVIRONMENT.json"
        marker_stage.write_text(
            json.dumps(expected_marker, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(stage, environment_root)
        return environment_python, requirements_sha
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise


def build_basic_from_source(
    project_root: Path,
    *,
    replace_invalid: bool = False,
    cache_root: Path | None = None,
) -> dict[str, Any]:
    target = project_root / COMPONENT_TARGETS["client-basic"]
    current = check_basic(project_root, full_hash=True)
    if current.status == "ready":
        return {"built": False, "reused": True, "target": str(target), "detail": current.detail}
    if target.exists() and not replace_invalid:
        raise BootstrapError(f"现有 Basic 目录未通过校验: {target}。确认修复时使用 -Repair。")

    export_cache = (cache_root or _basic_export_cache_root()).resolve(strict=False)
    environment_python, requirements_sha = _prepare_basic_export_environment(project_root, export_cache)
    contract_path = project_root / "RotoWeaveContracts" / "basic-assets.json"
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    source_cache = export_cache / "sources" / str(contract["revision"])
    exporter = project_root / "RotoWeaveClient" / "scripts" / "export-birefnet-onnx.py"
    license_path = (
        export_cache
        / "licenses"
        / str(contract["licenseSha256"])
        / str(contract["licenseFile"])
    )
    if not license_path.is_file() or _sha256_file(license_path) != str(contract["licenseSha256"]):
        license_path.unlink(missing_ok=True)
        _download_verified_file(
            {
                "id": "birefnet-license",
                "url": contract["licenseUrl"],
                "bytes": contract["licenseBytes"],
                "sha256": contract["licenseSha256"],
            },
            license_path,
            stage="basic-license",
        )
    stage_root = project_root / "Temp" / "Bootstrap" / f"basic-build-{uuid.uuid4().hex}"
    staged = stage_root / "client-basic"
    output = staged / str(contract["onnxFile"])
    backup: Path | None = None
    promoted = False
    try:
        staged.mkdir(parents=True)
        _run_basic_checked(
            [
                str(environment_python),
                str(exporter),
                "--output",
                str(output),
                "--cache-dir",
                str(source_cache),
                "--contract",
                str(contract_path),
                "--license",
                str(license_path),
                "--requirements",
                str(project_root / "RotoWeaveClient" / "requirements-basic-export-lock.txt"),
            ],
            "Basic ONNX 动态生成和数值自检",
        )
        staged_check = _check_basic_root(project_root, staged, full_hash=True)
        if staged_check.status != "ready":
            raise BootstrapError(f"Basic staging 未通过最终校验: {staged_check.detail}")
        if target.exists():
            backup = (
                project_root
                / "Temp"
                / "BootstrapBackups"
                / f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
                / "client-basic"
            )
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(target), str(backup))
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(staged), str(target))
        promoted = True
        final_check = check_basic(project_root, full_hash=True)
        if final_check.status != "ready":
            raise BootstrapError(f"Basic 晋升后校验失败: {final_check.detail}")
        manifest = json.loads(
            (target / "birefnet-lite-matting.manifest.json").read_text(encoding="utf-8")
        )
        return {
            "built": True,
            "reused": False,
            "target": str(target),
            "onnxSha256": manifest["onnxSha256"],
            "selfTestSha256": manifest["selfTest"]["sha256"],
            "requirementsSha256": requirements_sha,
            "backup": str(backup) if backup else None,
        }
    except Exception:
        if promoted and target.exists():
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
        if backup and backup.exists() and not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(backup), str(target))
        raise
    finally:
        shutil.rmtree(stage_root, ignore_errors=True)


def load_server_runtime_source_contract(project_root: Path) -> dict[str, Any]:
    path = project_root / "RotoWeaveContracts" / "server-runtime-sources.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BootstrapError(f"Server 固定运行时来源契约不可用: {path}: {exc}") from exc
    if payload.get("schemaVersion") != 1 or payload.get("platform") != PLATFORM_ID:
        raise BootstrapError("Server 固定运行时来源契约 schema 或平台不受支持。")
    python_source = payload.get("python")
    standard_library_sources = payload.get("standardLibrarySources")
    sources = payload.get("sources")
    profiles = payload.get("profiles")
    bootstrap_packages = payload.get("bootstrapPackages")
    runtime_wheels = payload.get("runtimeWheels")
    if (
        not isinstance(python_source, dict)
        or not isinstance(standard_library_sources, list)
        or not standard_library_sources
        or not isinstance(sources, list)
        or not isinstance(profiles, dict)
    ):
        raise BootstrapError(
            "Server 固定运行时来源契约缺少 python/standardLibrarySources/sources/profiles。"
        )
    if not isinstance(bootstrap_packages, list) or not bootstrap_packages or not all(isinstance(item, str) and "==" in item for item in bootstrap_packages):
        raise BootstrapError("Server 固定运行时 pip bootstrap 包必须精确锁定。")
    if not isinstance(runtime_wheels, list) or not runtime_wheels:
        raise BootstrapError("Server 固定运行时缺少大型 wheel 来源。")
    if payload.get("projectSearchPaths") != ["..\\..\\..", "..\\..\\..\\..\\RotoWeaveContracts"]:
        raise BootstrapError("Server 固定运行时项目搜索路径契约无效。")
    wheel_ids: set[str] = set()
    for wheel in runtime_wheels:
        if not isinstance(wheel, dict):
            raise BootstrapError("Server 固定运行时 wheel 来源必须是对象。")
        wheel_id = str(wheel.get("id") or "")
        filename = str(wheel.get("filename") or "")
        if not wheel_id or wheel_id in wheel_ids:
            raise BootstrapError(f"Server 固定运行时 wheel id 缺失或重复: {wheel_id or '<empty>'}")
        wheel_ids.add(wheel_id)
        if (
            urllib.parse.urlparse(str(wheel.get("url") or "")).scheme != "https"
            or Path(filename).name != filename
            or not filename.casefold().endswith(".whl")
            or int(wheel.get("bytes", -1)) <= 0
            or not re.fullmatch(r"[0-9a-f]{64}", str(wheel.get("sha256") or ""))
            or int(wheel.get("segments", 1)) < 1
            or int(wheel.get("segments", 1)) > 32
        ):
            raise BootstrapError(f"Server 固定运行时 wheel 来源无效: {wheel_id}")
    if (
        int(python_source.get("bytes", -1)) <= 0
        or not re.fullmatch(r"[0-9a-f]{64}", str(python_source.get("sha256") or ""))
        or urllib.parse.urlparse(str(python_source.get("url") or "")).scheme != "https"
        or Path(str(python_source.get("filename") or "")).name != str(python_source.get("filename") or "")
    ):
        raise BootstrapError("Server embedded Python 来源缺少精确 HTTPS/bytes/SHA-256。")
    standard_library_ids: set[str] = set()
    standard_library_targets: set[str] = set()
    for source in standard_library_sources:
        if not isinstance(source, dict):
            raise BootstrapError("Server 标准库源码来源必须是对象。")
        source_id = str(source.get("id") or "")
        filename = str(source.get("filename") or "")
        target_path = str(source.get("targetPath") or "")
        revision = str(source.get("revision") or "")
        normalized_target = target_path.replace("\\", "/")
        target_parts = normalized_target.split("/")
        if not source_id or source_id in standard_library_ids:
            raise BootstrapError(
                f"Server 标准库源码 id 缺失或重复: {source_id or '<empty>'}"
            )
        standard_library_ids.add(source_id)
        if normalized_target.casefold() in standard_library_targets:
            raise BootstrapError(f"Server 标准库源码目标重复: {target_path}")
        standard_library_targets.add(normalized_target.casefold())
        if (
            not re.fullmatch(r"[0-9a-f]{40}", revision)
            or urllib.parse.urlparse(str(source.get("url") or "")).scheme != "https"
            or Path(filename).name != filename
            or int(source.get("bytes", -1)) <= 0
            or not re.fullmatch(r"[0-9a-f]{64}", str(source.get("sha256") or ""))
            or len(target_parts) < 2
            or target_parts[0].casefold() != "lib"
            or any(part in {"", ".", ".."} for part in target_parts)
            or not normalized_target.casefold().endswith(".py")
        ):
            raise BootstrapError(f"Server 标准库源码来源无效: {source_id}")
    source_ids: set[str] = set()
    for source in sources:
        if not isinstance(source, dict):
            raise BootstrapError("Server 固定源码来源必须是对象。")
        source_id = str(source.get("id") or "")
        revision = str(source.get("revision") or "")
        include_path = str(source.get("includePath") or "")
        target_path = str(source.get("targetPath") or "")
        tree = source.get("tree")
        if not source_id or source_id in source_ids:
            raise BootstrapError(f"Server 固定源码 id 缺失或重复: {source_id or '<empty>'}")
        source_ids.add(source_id)
        if not re.fullmatch(r"[0-9a-f]{40}", revision):
            raise BootstrapError(f"Server 固定源码 revision 无效: {source_id}")
        if urllib.parse.urlparse(str(source.get("url") or "")).scheme != "https":
            raise BootstrapError(f"Server 固定源码只允许 HTTPS: {source_id}")
        if Path(str(source.get("filename") or "")).name != str(source.get("filename") or ""):
            raise BootstrapError(f"Server 固定源码 filename 无效: {source_id}")
        if int(source.get("maxBytes", 0)) <= 0 or int(source.get("maxBytes", 0)) > 1024 * 1024 * 1024:
            raise BootstrapError(f"Server 固定源码 maxBytes 无效: {source_id}")
        for label, relative in (("includePath", include_path), ("targetPath", target_path)):
            parts = relative.replace("\\", "/").split("/")
            if not relative or any(part in {"", ".", ".."} for part in parts):
                raise BootstrapError(f"Server 固定源码 {label} 无效: {source_id}")
        if (
            not isinstance(tree, dict)
            or tree.get("algorithm") != TREE_ALGORITHM
            or int(tree.get("fileCount", 0)) <= 0
            or int(tree.get("bytes", 0)) <= 0
            or not re.fullmatch(r"[0-9a-f]{64}", str(tree.get("sha256") or ""))
        ):
            raise BootstrapError(f"Server 固定源码树摘要无效: {source_id}")
    for profile in ("high", "ultra"):
        profile_payload = profiles.get(profile)
        if not isinstance(profile_payload, dict):
            raise BootstrapError(f"Server 固定运行时缺少 Profile 来源: {profile}")
        requirements = str(profile_payload.get("requirements") or "")
        requirements_path = project_root / requirements
        if not requirements or not requirements_path.is_file():
            raise BootstrapError(f"Server 固定运行时依赖锁缺失: {profile}")
        selected = profile_payload.get("sources")
        if not isinstance(selected, list) or not selected or any(str(item) not in source_ids for item in selected):
            raise BootstrapError(f"Server 固定运行时源码选择无效: {profile}")
    if profiles["ultra"].get("baseProfile") != "high":
        raise BootstrapError("Ultra 必须显式以 High 为只读公共依赖基座。")
    return payload


def _server_runtime_cache_root() -> Path:
    local_app_data = os.getenv("LOCALAPPDATA")
    if not local_app_data:
        raise BootstrapError("LOCALAPPDATA 不可用，无法建立 Server 固定运行时下载缓存。")
    return Path(local_app_data) / "RotoWeave" / "bootstrap" / "server-runtimes"


def _download_bounded_https_file(
    source: dict[str, Any],
    target: Path,
    *,
    progress: ProgressCallback | None = None,
    stage: str = "server-runtime-source-download",
    progress_start: float = 0.0,
    progress_end: float = 1.0,
) -> None:
    source_id = str(source["id"])
    url = str(source["url"])
    maximum = int(source["maxBytes"])
    if urllib.parse.urlparse(url).scheme != "https":
        raise BootstrapError(f"Server 固定源码只允许 HTTPS: {source_id}")
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(target.suffix + ".partial")
    failures: list[str] = []
    for attempt in range(1, COMPONENT_DOWNLOAD_ATTEMPTS + 1):
        partial.unlink(missing_ok=True)
        _emit(
            progress,
            stage,
            progress_start,
            f"正在下载固定源码 {source_id}（第 {attempt}/{COMPONENT_DOWNLOAD_ATTEMPTS} 次）",
            {"id": source_id, "downloadedBytes": 0, "attempt": attempt, "force": True},
        )
        request = urllib.request.Request(
            url,
            headers={"User-Agent": "RotoWeave-Setup/4.0", "Accept-Encoding": "identity"},
        )
        started = time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                final_url = str(response.geturl())
                if urllib.parse.urlparse(final_url).scheme != "https":
                    raise BootstrapError(f"{source_id} 重定向到了非 HTTPS 来源。")
                content_encoding = str(response.headers.get("Content-Encoding") or "identity").casefold()
                if content_encoding not in {"", "identity"}:
                    raise BootstrapError(f"{source_id} 返回不受支持的 Content-Encoding={content_encoding}")
                declared = response.headers.get("Content-Length")
                if declared is not None and int(declared) > maximum:
                    raise BootstrapError(f"{source_id} 远端长度超过安全上限: {declared}>{maximum}")
                expected_bytes = int(declared) if declared is not None else None
                downloaded = 0
                with partial.open("wb") as stream:
                    while True:
                        chunk = response.read(DOWNLOAD_CHUNK_BYTES)
                        if not chunk:
                            break
                        downloaded += len(chunk)
                        if downloaded > maximum:
                            raise BootstrapError(f"{source_id} 下载超过安全上限: {maximum}")
                        stream.write(chunk)
                        elapsed = max(0.001, time.monotonic() - started)
                        speed = downloaded / elapsed
                        denominator = expected_bytes or maximum
                        _emit(
                            progress,
                            stage,
                            progress_start + (progress_end - progress_start) * (downloaded / max(1, denominator)),
                            f"正在下载固定源码 {source_id}",
                            {
                                "id": source_id,
                                "attempt": attempt,
                                "downloadedBytes": downloaded,
                                "expectedBytes": expected_bytes,
                                "bytesPerSecond": speed,
                                "etaSeconds": (
                                    max(0, expected_bytes - downloaded) / speed
                                    if expected_bytes is not None and speed > 0
                                    else None
                                ),
                            },
                        )
            if not zipfile.is_zipfile(partial):
                raise BootstrapError(f"{source_id} 下载结果不是有效 ZIP。")
            os.replace(partial, target)
            _emit(
                progress,
                stage,
                progress_end,
                f"固定源码下载完成 {source_id}",
                {
                    "id": source_id,
                    "downloadedBytes": target.stat().st_size,
                    "expectedBytes": target.stat().st_size,
                    "force": True,
                },
            )
            return
        except (urllib.error.URLError, http.client.HTTPException, socket.timeout, TimeoutError, OSError, BootstrapError) as exc:
            failures.append(f"attempt={attempt} {exc}")
            downloaded = partial.stat().st_size if partial.exists() else 0
            partial.unlink(missing_ok=True)
        if attempt < COMPONENT_DOWNLOAD_ATTEMPTS:
            retry_delay = min(2 ** (attempt - 1), 4)
            _emit(
                progress,
                stage,
                progress_start,
                f"{source_id} 下载中断，将重新下载",
                {
                    "id": source_id,
                    "attempt": attempt,
                    "downloadedBytes": downloaded,
                    "retryInSeconds": retry_delay,
                    "force": True,
                },
            )
            time.sleep(retry_delay)
    raise BootstrapError(f"{source_id} 下载失败: {failures[-1] if failures else 'unknown'}")


def _download_segmented_verified_file(
    source: dict[str, Any],
    target: Path,
    *,
    progress: ProgressCallback | None = None,
    stage: str = "server-runtime-wheel-download",
    progress_start: float = 0.0,
    progress_end: float = 1.0,
) -> None:
    source_id = str(source["id"])
    url = str(source["url"])
    expected_bytes = int(source["bytes"])
    expected_sha256 = str(source["sha256"]).casefold()
    segments = min(int(source.get("segments", 8)), max(1, expected_bytes // SEGMENT_TARGET_BYTES))
    segments = max(1, segments)
    expected_etag = str(source.get("etag") or "")
    if urllib.parse.urlparse(url).scheme != "https":
        raise BootstrapError(f"Server 固定 wheel 只允许 HTTPS: {source_id}")
    if target.is_file() and target.stat().st_size == expected_bytes:
        actual_sha256 = _sha256_file_with_progress(
            target,
            progress=progress,
            stage=f"{stage}-cache-check",
            progress_start=progress_start,
            progress_end=progress_end,
            source_id=source_id,
        )
        if actual_sha256 == expected_sha256:
            _emit(
                progress,
                stage,
                progress_end,
                f"使用已验证下载缓存 {source_id}",
                {"id": source_id, "downloadedBytes": expected_bytes, "expectedBytes": expected_bytes, "force": True},
            )
            return
    target.unlink(missing_ok=True)
    parts_root = target.with_name(target.name + ".parts")
    metadata_path = parts_root / "metadata.json"
    identity = {
        "schemaVersion": 1,
        "id": source_id,
        "url": url,
        "expectedBytes": expected_bytes,
        "expectedSha256": expected_sha256,
        "segments": segments,
        "etag": expected_etag,
    }
    if parts_root.exists():
        try:
            if json.loads(metadata_path.read_text(encoding="utf-8")) != identity:
                raise ValueError("segmented identity mismatch")
        except (OSError, ValueError, json.JSONDecodeError):
            shutil.rmtree(parts_root)
    parts_root.mkdir(parents=True, exist_ok=True)
    _write_partial_metadata(metadata_path, identity)
    segment_size = (expected_bytes + segments - 1) // segments
    segment_lengths = [min(expected_bytes, (index + 1) * segment_size) - index * segment_size for index in range(segments)]
    segment_downloaded = {
        index: min(
            segment_lengths[index],
            (parts_root / f"part-{index:03d}.bin").stat().st_size
            if (parts_root / f"part-{index:03d}.bin").is_file()
            else 0,
        )
        for index in range(segments)
    }
    initial_downloaded = sum(segment_downloaded.values())
    download_started = time.monotonic()
    progress_lock = threading.Lock()

    def report_segment(
        index: int,
        downloaded: int,
        message: str,
        *,
        force: bool = False,
        retry_in_seconds: float | None = None,
    ) -> None:
        with progress_lock:
            segment_downloaded[index] = downloaded
            aggregate = sum(segment_downloaded.values())
            completed = sum(
                1 for item_index, item_downloaded in segment_downloaded.items()
                if item_downloaded >= segment_lengths[item_index]
            )
            elapsed = max(0.001, time.monotonic() - download_started)
            speed = max(0, aggregate - initial_downloaded) / elapsed
            _emit(
                progress,
                stage,
                progress_start + (progress_end - progress_start) * (aggregate / max(1, expected_bytes)),
                message,
                {
                    "id": source_id,
                    "segment": index + 1,
                    "downloadedBytes": aggregate,
                    "expectedBytes": expected_bytes,
                    "bytesPerSecond": speed,
                    "etaSeconds": max(0, expected_bytes - aggregate) / speed if speed > 0 else None,
                    "completedSegments": completed,
                    "totalSegments": segments,
                    "retryInSeconds": retry_in_seconds,
                    "force": force,
                },
            )

    _emit(
        progress,
        stage,
        progress_start + (progress_end - progress_start) * (initial_downloaded / max(1, expected_bytes)),
        f"正在分段下载 {source_id}",
        {
            "id": source_id,
            "downloadedBytes": initial_downloaded,
            "expectedBytes": expected_bytes,
            "completedSegments": sum(
                1 for index, downloaded in segment_downloaded.items() if downloaded >= segment_lengths[index]
            ),
            "totalSegments": segments,
            "force": True,
        },
    )

    def download_segment(index: int) -> None:
        start = index * segment_size
        end = min(expected_bytes, start + segment_size) - 1
        expected_length = end - start + 1
        part = parts_root / f"part-{index:03d}.bin"
        if part.is_file() and part.stat().st_size == expected_length:
            return
        if part.exists() and part.stat().st_size > expected_length:
            part.unlink()
        failures: list[str] = []
        for attempt in range(1, COMPONENT_DOWNLOAD_ATTEMPTS + 1):
            offset = part.stat().st_size if part.exists() else 0
            request_start = start + offset
            if request_start > end:
                return
            request = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "RotoWeave-Setup/4.0",
                    "Accept-Encoding": "identity",
                    "Range": f"bytes={request_start}-{end}",
                    **({"If-Range": expected_etag} if expected_etag else {}),
                },
            )
            try:
                with urllib.request.urlopen(request, timeout=60) as response:
                    final_url = str(response.geturl())
                    status = int(getattr(response, "status", 200))
                    content_range = str(response.headers.get("Content-Range") or "")
                    match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", content_range)
                    if urllib.parse.urlparse(final_url).scheme != "https" or status != 206:
                        raise BootstrapError(f"{source_id} 分段 {index} 没有返回 HTTPS 206。")
                    if not match or (int(match.group(1)), int(match.group(2)), int(match.group(3))) != (request_start, end, expected_bytes):
                        raise BootstrapError(f"{source_id} 分段 {index} Content-Range 无效: {content_range}")
                    response_etag = str(response.headers.get("ETag") or "")
                    if expected_etag and response_etag != expected_etag:
                        raise BootstrapError(f"{source_id} 分段 {index} ETag 不匹配。")
                    declared = response.headers.get("Content-Length")
                    if declared is not None and int(declared) != end - request_start + 1:
                        raise BootstrapError(f"{source_id} 分段 {index} 长度不匹配。")
                    downloaded = offset
                    with part.open("ab" if offset else "wb") as stream:
                        while True:
                            chunk = response.read(DOWNLOAD_CHUNK_BYTES)
                            if not chunk:
                                break
                            downloaded += len(chunk)
                            if downloaded > expected_length:
                                raise BootstrapError(f"{source_id} 分段 {index} 超出预期长度。")
                            stream.write(chunk)
                            report_segment(index, downloaded, f"正在分段下载 {source_id}")
                if part.stat().st_size == expected_length:
                    report_segment(index, expected_length, f"正在分段下载 {source_id}")
                    return
                failures.append(f"attempt={attempt} incomplete={part.stat().st_size}/{expected_length}")
            except (urllib.error.URLError, http.client.HTTPException, socket.timeout, TimeoutError, OSError, BootstrapError) as exc:
                failures.append(f"attempt={attempt} {exc}")
            if attempt < COMPONENT_DOWNLOAD_ATTEMPTS:
                retry_delay = min(2 ** (attempt - 1), 4)
                report_segment(
                    index,
                    part.stat().st_size if part.exists() else 0,
                    f"{source_id} 分段 {index + 1}/{segments} 下载中断",
                    force=True,
                    retry_in_seconds=retry_delay,
                )
                time.sleep(retry_delay)
        raise BootstrapError(f"{source_id} 分段 {index} 下载失败: {failures[-1] if failures else 'unknown'}")

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=segments) as executor:
            futures = [executor.submit(download_segment, index) for index in range(segments)]
            for future in concurrent.futures.as_completed(futures):
                future.result()
        partial = target.with_suffix(target.suffix + ".partial")
        _emit(
            progress,
            stage,
            progress_end,
            f"正在合并并校验 {source_id}",
            {
                "id": source_id,
                "downloadedBytes": expected_bytes,
                "expectedBytes": expected_bytes,
                "completedSegments": segments,
                "totalSegments": segments,
                "force": True,
            },
        )
        digest = hashlib.sha256()
        written = 0
        with partial.open("wb") as output:
            for index in range(segments):
                part = parts_root / f"part-{index:03d}.bin"
                with part.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(DOWNLOAD_CHUNK_BYTES), b""):
                        output.write(chunk)
                        digest.update(chunk)
                        written += len(chunk)
        if written != expected_bytes or digest.hexdigest() != expected_sha256:
            partial.unlink(missing_ok=True)
            shutil.rmtree(parts_root, ignore_errors=True)
            raise BootstrapError(
                f"{source_id} 分段合并校验失败: bytes={written}/{expected_bytes} sha256={digest.hexdigest()}"
            )
        os.replace(partial, target)
        shutil.rmtree(parts_root)
        _emit(
            progress,
            stage,
            progress_end,
            f"分段下载与 SHA-256 校验完成 {source_id}",
            {
                "id": source_id,
                "downloadedBytes": expected_bytes,
                "expectedBytes": expected_bytes,
                "completedSegments": segments,
                "totalSegments": segments,
                "force": True,
            },
        )
    except Exception:
        target.with_suffix(target.suffix + ".partial").unlink(missing_ok=True)
        raise


def _copy_runtime_source_projection(archive_path: Path, source: dict[str, Any], destination: Path) -> dict[str, Any]:
    include_path = str(source["includePath"]).replace("\\", "/").strip("/")
    target_root = destination / Path(str(source["targetPath"]).replace("/", os.sep))
    if target_root.exists():
        raise BootstrapError(f"固定源码 staging 目标已存在: {target_root}")
    target_root.mkdir(parents=True)
    copied = 0
    try:
        with zipfile.ZipFile(archive_path, "r") as archive:
            infos = _validate_zip_members(archive)
            seen: set[str] = set()
            for info in infos:
                if info.is_dir():
                    continue
                normalized = _safe_zip_name(info.filename)
                parts = normalized.split("/")
                if len(parts) < 3:
                    continue
                repository_relative = "/".join(parts[1:])
                if not repository_relative.startswith(include_path + "/"):
                    continue
                relative = repository_relative[len(include_path) + 1 :]
                folded = relative.casefold()
                if folded in seen:
                    raise BootstrapError(f"固定源码投影包含大小写冲突: {source['id']}:{relative}")
                seen.add(folded)
                output = target_root / Path(relative.replace("/", os.sep))
                output.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info, "r") as reader, output.open("xb") as writer:
                    shutil.copyfileobj(reader, writer, length=DOWNLOAD_CHUNK_BYTES)
                copied += 1
        if copied == 0:
            raise BootstrapError(f"固定源码归档缺少裁剪路径: {source['id']}:{include_path}")
        actual = tree_summary(target_root)
        if actual != source["tree"]:
            raise BootstrapError(
                f"固定源码树摘要不匹配: {source['id']} expected={source['tree']} actual={actual}"
            )
        return actual
    except Exception:
        shutil.rmtree(target_root, ignore_errors=True)
        raise


def _extract_embedded_python(
    archive_path: Path,
    runtime_root: Path,
    *,
    inherit_high: bool,
    project_search_paths: list[str],
    standard_library_sources: list[tuple[dict[str, Any], Path]],
) -> None:
    if runtime_root.exists():
        raise BootstrapError(f"embedded Python staging 目标已存在: {runtime_root}")
    runtime_root.mkdir(parents=True)
    with zipfile.ZipFile(archive_path, "r") as archive:
        infos = _validate_zip_members(archive)
        for info in infos:
            if info.is_dir():
                continue
            normalized = _safe_zip_name(info.filename)
            if "/" in normalized:
                raise BootstrapError(f"embedded Python ZIP 包含意外子目录: {normalized}")
            output = runtime_root / normalized
            with archive.open(info, "r") as reader, output.open("xb") as writer:
                shutil.copyfileobj(reader, writer, length=DOWNLOAD_CHUNK_BYTES)
    for source, cached_path in standard_library_sources:
        relative = Path(str(source["targetPath"]).replace("/", os.sep))
        output = runtime_root / relative
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(cached_path, output)
    site_packages = runtime_root / "Lib" / "site-packages"
    site_packages.mkdir(parents=True)
    lines = ["Lib", "python310.zip", ".", "Lib\\site-packages"]
    if inherit_high:
        lines.append("..\\..\\high\\runtime\\Lib\\site-packages")
    lines.extend(project_search_paths)
    lines.append("import site")
    (runtime_root / "python310._pth").write_text("\n".join(lines) + "\n", encoding="ascii")


def _run_runtime_checked(
    command: list[str],
    label: str,
    *,
    environment: dict[str, str] | None = None,
    progress: ProgressCallback | None = None,
    stage: str = "server-runtime-command",
    progress_value: float = 0.0,
) -> None:
    env = os.environ.copy()
    env.update(environment or {})
    env["PYTHONNOUSERSITE"] = "1"
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    env["SETUPTOOLS_USE_DISTUTILS"] = "stdlib"
    _emit(progress, stage, progress_value, f"开始{label}", {"force": True})
    started = time.monotonic()
    process = subprocess.Popen(command, stdout=sys.stderr, stderr=sys.stderr, env=env)
    while True:
        try:
            return_code = process.wait(timeout=RUNTIME_HEARTBEAT_SECONDS)
            break
        except subprocess.TimeoutExpired:
            _emit(
                progress,
                stage,
                progress_value,
                f"{label}仍在执行，请勿关闭窗口",
                {"elapsedSeconds": time.monotonic() - started, "force": True},
            )
    if return_code != 0:
        raise BootstrapError(f"{label}失败 (exit={return_code})")
    _emit(
        progress,
        stage,
        progress_value,
        f"{label}完成",
        {"elapsedSeconds": time.monotonic() - started, "force": True},
    )


def _bootstrap_embedded_pip(
    contract: dict[str, Any],
    runtime_root: Path,
    *,
    progress: ProgressCallback | None = None,
    progress_value: float = 0.0,
) -> None:
    site_packages = runtime_root / "Lib" / "site-packages"
    command = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--only-binary=:all:",
        "--no-deps",
        "--target",
        str(site_packages),
        *[str(item) for item in contract["bootstrapPackages"]],
    ]
    _run_runtime_checked(
        command,
        "安装 embedded Python pip 工具",
        progress=progress,
        stage="server-runtime-bootstrap-pip",
        progress_value=progress_value,
    )


def _install_runtime_requirements(
    python: Path,
    requirements: Path,
    profile: str,
    *,
    progress: ProgressCallback | None = None,
    progress_value: float = 0.0,
) -> None:
    install_arguments = [
        str(python),
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--prefer-binary",
        "--no-build-isolation",
        "--no-deps",
    ]
    if profile == "ultra":
        install_arguments.append("--ignore-installed")
    install_arguments.extend(["--requirement", str(requirements)])
    _run_runtime_checked(
        install_arguments,
        f"安装 {profile} 固定依赖",
        progress=progress,
        stage=f"server-runtime-{profile}-requirements",
        progress_value=progress_value,
    )
    _run_runtime_checked(
        [str(python), "-m", "pip", "check"],
        f"检查 {profile} 固定依赖",
        progress=progress,
        stage=f"server-runtime-{profile}-pip-check",
        progress_value=progress_value + 0.01,
    )


def _probe_server_runtime(runtime_root: Path, profile: str) -> dict[str, Any]:
    python = runtime_root / profile / "runtime" / "python.exe"
    high_site = (runtime_root / "high" / "runtime" / "Lib" / "site-packages").resolve(strict=False)
    is_overlay = profile == "ultra"
    script = (
        "import enum,inspect,json,platform;from pathlib import Path;"
        "import torch,numpy,cv2;"
        "enum_source=inspect.getsource(enum.Enum._generate_next_value_);"
        f"overlay={is_overlay!r};h=Path({str(high_site)!r});t=Path(torch.__file__).resolve();n=Path(numpy.__file__).resolve();"
        "print(json.dumps({'python':platform.python_version(),'torch':torch.__version__,"
        "'cuda':str(torch.version.cuda),'numpy':numpy.__version__,'opencv':cv2.__version__,"
        "'inheritsHigh':overlay and (h==t or h in t.parents),"
        "'numpyFromHigh':overlay and (h==n or h in n.parents),"
        "'stdlibSourceAvailable':'def _generate_next_value_' in enum_source},sort_keys=True))"
    )
    env = os.environ.copy()
    env["PYTHONNOUSERSITE"] = "1"
    env.pop("PYTHONPATH", None)
    completed = subprocess.run(
        [str(python), "-c", script],
        check=False,
        capture_output=True,
        text=False,
        env=env,
        timeout=60,
    )
    if completed.returncode != 0:
        detail = _decode_native_output(completed.stderr).strip() or _decode_native_output(completed.stdout).strip()
        raise BootstrapError(f"{profile} 固定运行时导入探针失败: {detail or completed.returncode}")
    try:
        return json.loads(_decode_native_output(completed.stdout).strip())
    except json.JSONDecodeError as exc:
        raise BootstrapError(f"{profile} 固定运行时探针输出无效。") from exc


def _validate_staged_server_runtimes(project_root: Path, staged: Path, contract: dict[str, Any]) -> dict[str, Any]:
    _, runtime_recipe = _load_contracts(project_root)
    probes: dict[str, Any] = {}
    for profile in ("high", "ultra"):
        expected = runtime_recipe(profile)
        probe = _probe_server_runtime(staged, profile)
        if probe["python"] != expected["pythonVersion"] or probe["torch"] != expected["torch"] or probe["cuda"] != expected["cuda"]:
            raise BootstrapError(f"{profile} 固定运行时版本探针与 Recipe 不一致: {probe}")
        if profile == "high" and (probe.get("inheritsHigh") or probe.get("numpyFromHigh")):
            raise BootstrapError("High 固定运行时意外继承外部 Profile。")
        if profile == "ultra" and (not probe.get("inheritsHigh") or probe.get("numpyFromHigh")):
            raise BootstrapError("Ultra overlay 必须从 High 加载 torch，并从自身 overlay 加载 numpy。")
        if probe.get("stdlibSourceAvailable") is not True:
            raise BootstrapError(f"{profile} 固定运行时无法向 TorchScript 提供标准库源码。")
        probes[profile] = probe
    forbidden = [
        path for path in staged.rglob("*")
        if path.is_file() and _is_forbidden_runtime_weight(path, staged)
    ]
    if forbidden:
        raise BootstrapError(f"生成的固定运行时混入模型权重: {forbidden[0]}")
    return probes


def build_server_runtimes_from_source(
    project_root: Path,
    *,
    replace_invalid: bool = False,
    cache_root: Path | None = None,
    progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    target = project_root / COMPONENT_TARGETS["server-runtimes"]
    _emit(progress, "server-runtime-preflight", 0.0, "正在检查 Server 固定运行时与下载缓存", {"force": True})
    current = check_server_runtimes(project_root)
    if current.status == "ready":
        _emit(progress, "server-runtime-ready", 1.0, "Server 固定运行时已就绪，无需重建", {"force": True})
        return {"built": False, "reused": True, "target": str(target), "detail": current.detail}
    if target.exists() and not replace_invalid:
        raise BootstrapError(f"现有 Server 固定运行时未通过校验: {target}。确认修复时使用 -Repair。")

    contract_path = project_root / "RotoWeaveContracts" / "server-runtime-sources.json"
    contract = load_server_runtime_source_contract(project_root)
    contract_sha = _sha256_file(contract_path)
    cache = (cache_root or _server_runtime_cache_root()).resolve(strict=False)
    downloads = cache / "downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    python_source = contract["python"]
    python_archive = downloads / str(python_source["filename"])
    python_ready = False
    if python_archive.is_file() and python_archive.stat().st_size == int(python_source["bytes"]):
        python_ready = _sha256_file_with_progress(
            python_archive,
            progress=progress,
            stage="server-runtime-python-cache-check",
            progress_start=0.01,
            progress_end=0.02,
            source_id=str(python_source["id"]),
        ) == str(python_source["sha256"])
    if not python_ready:
        python_archive.unlink(missing_ok=True)
        _download_verified_file(
            python_source,
            python_archive,
            progress=progress,
            stage="server-runtime-python-download",
            progress_start=0.01,
            progress_end=0.04,
        )
    else:
        _emit(
            progress,
            "server-runtime-python-cache",
            0.04,
            f"使用已验证下载缓存 {python_source['id']}",
            {
                "id": str(python_source["id"]),
                "downloadedBytes": int(python_source["bytes"]),
                "expectedBytes": int(python_source["bytes"]),
                "force": True,
            },
        )

    standard_library_files: list[tuple[dict[str, Any], Path]] = []
    standard_library_count = max(1, len(contract["standardLibrarySources"]))
    for source_index, source in enumerate(contract["standardLibrarySources"]):
        range_start = 0.04 + 0.01 * (source_index / standard_library_count)
        range_end = 0.04 + 0.01 * ((source_index + 1) / standard_library_count)
        cached_path = downloads / str(source["filename"])
        ready = (
            cached_path.is_file()
            and cached_path.stat().st_size == int(source["bytes"])
            and _sha256_file(cached_path) == str(source["sha256"])
        )
        if not ready:
            cached_path.unlink(missing_ok=True)
            _download_verified_file(
                source,
                cached_path,
                progress=progress,
                stage="server-runtime-stdlib-source-download",
                progress_start=range_start,
                progress_end=range_end,
            )
        else:
            _emit(
                progress,
                "server-runtime-stdlib-source-cache",
                range_end,
                f"使用已验证标准库源码缓存 {source['id']}",
                {
                    "id": str(source["id"]),
                    "downloadedBytes": int(source["bytes"]),
                    "expectedBytes": int(source["bytes"]),
                    "force": True,
                },
            )
        standard_library_files.append((source, cached_path))

    source_archives: dict[str, Path] = {}
    source_count = max(1, len(contract["sources"]))
    for source_index, source in enumerate(contract["sources"]):
        range_start = 0.05 + 0.07 * (source_index / source_count)
        range_end = 0.05 + 0.07 * ((source_index + 1) / source_count)
        archive = downloads / str(source["filename"])
        if not archive.is_file():
            _download_bounded_https_file(
                source,
                archive,
                progress=progress,
                progress_start=range_start,
                progress_end=range_end,
            )
        else:
            _emit(
                progress,
                "server-runtime-source-cache",
                range_end,
                f"复用固定源码缓存 {source['id']}（投影阶段将校验内容）",
                {
                    "id": str(source["id"]),
                    "downloadedBytes": archive.stat().st_size,
                    "expectedBytes": archive.stat().st_size,
                    "force": True,
                },
            )
        source_archives[str(source["id"])] = archive

    runtime_wheels: list[Path] = []
    total_wheel_bytes = max(1, sum(int(item["bytes"]) for item in contract["runtimeWheels"]))
    wheel_bytes_before = 0
    for source in contract["runtimeWheels"]:
        range_start = 0.12 + 0.36 * (wheel_bytes_before / total_wheel_bytes)
        wheel_bytes_before += int(source["bytes"])
        range_end = 0.12 + 0.36 * (wheel_bytes_before / total_wheel_bytes)
        wheel = downloads / str(source["filename"])
        _download_segmented_verified_file(
            source,
            wheel,
            progress=progress,
            progress_start=range_start,
            progress_end=range_end,
        )
        runtime_wheels.append(wheel)

    _emit(progress, "server-runtime-downloads-ready", 0.48, "全部受控下载已完成并通过校验", {"force": True})

    stage_root = project_root / "Temp" / "Bootstrap" / f"server-runtime-build-{uuid.uuid4().hex}"
    staged = stage_root / "server-runtimes"
    backup: Path | None = None
    promoted = False
    try:
        staged.mkdir(parents=True)
        for profile in ("high", "ultra"):
            profile_start = 0.50 if profile == "high" else 0.70
            runtime = staged / profile / "runtime"
            _emit(
                progress,
                f"server-runtime-{profile}-extract",
                profile_start,
                f"正在生成 {profile.title()} embedded Python 运行时",
                {"force": True},
            )
            _extract_embedded_python(
                python_archive,
                runtime,
                inherit_high=profile == "ultra",
                project_search_paths=[str(item) for item in contract["projectSearchPaths"]],
                standard_library_sources=standard_library_files,
            )
            _verify_authenticode(runtime / "python.exe", str(python_source.get("authenticodePublisher") or ""))
            _verify_authenticode(runtime / "python310.dll", str(python_source.get("authenticodePublisher") or ""))
            _bootstrap_embedded_pip(
                contract,
                runtime,
                progress=progress,
                progress_value=profile_start + 0.02,
            )
            if profile == "high":
                _run_runtime_checked(
                    [str(runtime / "python.exe"), "-m", "pip", "install", "--no-deps", *[str(item) for item in runtime_wheels]],
                    "安装 High 固定 CUDA wheels",
                    progress=progress,
                    stage="server-runtime-high-cuda-wheels",
                    progress_value=0.56,
                )
            requirements = project_root / str(contract["profiles"][profile]["requirements"])
            _install_runtime_requirements(
                runtime / "python.exe",
                requirements,
                profile,
                progress=progress,
                progress_value=0.63 if profile == "high" else 0.78,
            )
            _emit(
                progress,
                f"server-runtime-{profile}-ready",
                0.69 if profile == "high" else 0.82,
                f"{profile.title()} Python 与固定依赖安装完成",
                {"force": True},
            )

        source_by_id = {str(item["id"]): item for item in contract["sources"]}
        source_trees: dict[str, dict[str, dict[str, Any]]] = {"high": {}, "ultra": {}}
        projection_total = sum(len(contract["profiles"][profile]["sources"]) for profile in ("high", "ultra"))
        projection_index = 0
        for profile in ("high", "ultra"):
            destination = staged / profile / "sources"
            destination.mkdir(parents=True)
            for source_id in contract["profiles"][profile]["sources"]:
                projection_value = 0.83 + 0.07 * (projection_index / max(1, projection_total))
                _emit(
                    progress,
                    "server-runtime-source-projection",
                    projection_value,
                    f"正在生成 {profile.title()} 固定源码投影：{source_id}",
                    {"id": str(source_id), "force": True},
                )
                source = source_by_id[str(source_id)]
                archive = source_archives[str(source_id)]
                try:
                    summary = _copy_runtime_source_projection(archive, source, destination)
                except (BootstrapError, OSError, zipfile.BadZipFile):
                    archive.unlink(missing_ok=True)
                    _download_bounded_https_file(
                        source,
                        archive,
                        progress=progress,
                        progress_start=projection_value,
                        progress_end=projection_value,
                    )
                    summary = _copy_runtime_source_projection(archive, source, destination)
                source_trees[profile][str(source_id)] = summary
                projection_index += 1

        _emit(progress, "server-runtime-manifests", 0.91, "正在生成运行时清单与构建回执", {"force": True})
        _, runtime_recipe = _load_contracts(project_root)
        for profile in ("high", "ultra"):
            recipe = runtime_recipe(profile)
            profile_root = staged / profile
            (profile_root / "runtime-manifest.json").write_text(
                json.dumps(recipe, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                encoding="utf-8",
            )
            receipt = {
                "schemaVersion": 1,
                "profile": profile,
                "runtimeDigest": recipe["digest"],
                "pythonArchiveSha256": python_source["sha256"],
                "requirementsSha256": recipe["requirementsSha256"],
                "runtimeSourceContractSha256": contract_sha,
                "standardLibrarySources": {
                    str(source["id"]): {
                        "targetPath": str(source["targetPath"]),
                        "bytes": int(source["bytes"]),
                        "sha256": str(source["sha256"]),
                    }
                    for source in contract["standardLibrarySources"]
                },
                "sourceTrees": source_trees[profile],
            }
            (profile_root / "runtime-build.json").write_text(
                json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                encoding="utf-8",
            )
        _emit(progress, "server-runtime-validation", 0.93, "正在执行 High/Ultra 导入、版本与模型权重隔离检查", {"force": True})
        probes = _validate_staged_server_runtimes(project_root, staged, contract)
        _emit(progress, "server-runtime-validation", 0.97, "High/Ultra staging 校验通过", {"force": True})

        _emit(progress, "server-runtime-promotion", 0.98, "正在原子晋升新运行时（现有运行时会先备份）", {"force": True})
        if target.exists():
            backup = (
                project_root
                / "Temp"
                / "BootstrapBackups"
                / f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
                / "server-runtimes"
            )
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(target), str(backup))
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(staged), str(target))
        promoted = True
        final = check_server_runtimes(project_root)
        if final.status != "ready":
            raise BootstrapError(f"Server 固定运行时晋升后校验失败: {final.detail}")
        _emit(progress, "server-runtime-complete", 1.0, "Server High/Ultra 固定运行时已就绪", {"force": True})
        return {
            "built": True,
            "reused": False,
            "target": str(target),
            "runtimeSourceContractSha256": contract_sha,
            "profiles": probes,
            "backup": str(backup) if backup else None,
        }
    except Exception:
        if promoted and target.exists():
            shutil.rmtree(target, ignore_errors=True)
        if backup and backup.exists() and not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(backup), str(target))
        raise
    finally:
        shutil.rmtree(stage_root, ignore_errors=True)


def select_bundle(project_root: Path, role: str, directory: Path) -> Path:
    if not directory.is_dir():
        raise BootstrapError(f"部署包目录不存在: {directory}")
    compatible: list[Path] = []
    failures: list[str] = []
    for candidate in sorted(directory.glob("*.zip"), key=lambda item: item.name.casefold()):
        try:
            inspect_bundle(project_root, candidate, expected_role=role, full_hash=False)
            compatible.append(candidate)
        except (BootstrapError, OSError, zipfile.BadZipFile, json.JSONDecodeError) as exc:
            failures.append(f"{candidate.name}: {exc}")
    if not compatible:
        detail = "; ".join(failures[:3])
        raise BootstrapError("目录中没有兼容部署 ZIP。" + (f" {detail}" if detail else ""))
    if len(compatible) > 1:
        raise BootstrapError("目录中存在多个兼容部署 ZIP；非交互模式请使用 -BundlePath 精确指定。")
    return compatible[0]


def download_bundle(source: str, output: Path, expected_sha256: str) -> Path:
    if not re.fullmatch(r"[0-9a-fA-F]{64}", expected_sha256 or ""):
        raise BootstrapError("受控下载必须提供 64 位预期 ZIP SHA-256。")
    expected_sha256 = expected_sha256.casefold()
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        raise BootstrapError(f"下载目标已存在，拒绝覆盖: {output}")
    partial = output.with_suffix(output.suffix + ".partial")
    partial_metadata = output.with_suffix(output.suffix + ".partial.json")
    identity = {
        "schemaVersion": 1,
        "source": source,
        "expectedSha256": expected_sha256,
    }
    metadata: dict[str, Any] = {}
    if partial.exists():
        try:
            metadata = json.loads(partial_metadata.read_text(encoding="utf-8"))
            if any(metadata.get(key) != value for key, value in identity.items()):
                raise ValueError("partial identity mismatch")
        except (OSError, ValueError, json.JSONDecodeError):
            partial.unlink(missing_ok=True)
            partial_metadata.unlink(missing_ok=True)
            metadata = {}
    elif partial_metadata.exists():
        partial_metadata.unlink(missing_ok=True)
    parsed = urllib.parse.urlparse(source)
    windows_path = bool(re.match(r"^[A-Za-z]:[\\/]", source)) or source.startswith("\\\\")
    if windows_path or parsed.scheme in {"", "file"}:
        source_path = Path(urllib.request.url2pathname(parsed.path) if parsed.scheme == "file" else source).resolve(strict=True)
        if not source_path.is_file():
            raise BootstrapError(f"本地/UNC 来源不是文件: {source_path}")
        source_bytes = source_path.stat().st_size
        offset = partial.stat().st_size if partial.exists() else 0
        if offset > source_bytes:
            partial.unlink(missing_ok=True)
            partial_metadata.unlink(missing_ok=True)
            offset = 0
        _write_partial_metadata(partial_metadata, {**identity, "expectedBytes": source_bytes, "etag": ""})
        with source_path.open("rb") as reader, partial.open("ab" if offset else "wb") as writer:
            reader.seek(offset)
            shutil.copyfileobj(reader, writer, length=8 * 1024 * 1024)
    elif parsed.scheme == "https":
        offset = partial.stat().st_size if partial.exists() else 0
        prior_etag = str(metadata.get("etag") or "")
        prior_expected_bytes = int(metadata.get("expectedBytes", -1))
        request = urllib.request.Request(source, headers={"User-Agent": "RotoWeave-Setup/4.0"})
        if offset:
            request.add_header("Range", f"bytes={offset}-")
            if prior_etag:
                request.add_header("If-Range", prior_etag)
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                status = int(getattr(response, "status", 200))
                response_etag = str(response.headers.get("ETag") or "")
                content_encoding = str(response.headers.get("Content-Encoding") or "identity").casefold()
                if content_encoding not in {"", "identity"}:
                    raise BootstrapError(f"部署包来源返回不受支持的 Content-Encoding={content_encoding}")
                append = offset > 0 and status == 206
                expected_bytes = -1
                if status == 206:
                    content_range = str(response.headers.get("Content-Range") or "")
                    match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", content_range)
                    if not match or int(match.group(1)) != offset:
                        raise BootstrapError(
                            f"部署包断点响应无效: expected-start={offset} actual={content_range or '<missing>'}"
                        )
                    range_end = int(match.group(2))
                    expected_bytes = int(match.group(3))
                    if range_end < offset or range_end >= expected_bytes:
                        raise BootstrapError(f"部署包断点响应范围无效: {content_range}")
                    content_length = response.headers.get("Content-Length")
                    if content_length is not None and int(content_length) != range_end - offset + 1:
                        raise BootstrapError(
                            f"部署包断点响应长度无效: range={content_range} content-length={content_length}"
                        )
                    if prior_expected_bytes >= 0 and expected_bytes != prior_expected_bytes:
                        raise BootstrapError(
                            f"部署包来源总长度已变化: expected={prior_expected_bytes} actual={expected_bytes}"
                        )
                elif status == 200:
                    append = False
                    offset = 0
                    content_length = response.headers.get("Content-Length")
                    expected_bytes = int(content_length) if content_length is not None else -1
                else:
                    raise BootstrapError(f"部署包来源返回不受支持的 HTTP status={status}")
                if append and prior_etag and response_etag and response_etag != prior_etag:
                    partial.unlink(missing_ok=True)
                    partial_metadata.unlink(missing_ok=True)
                    raise BootstrapError("部署包来源 ETag 已变化；拒绝把新旧字节拼接到同一 partial。")
                _write_partial_metadata(
                    partial_metadata,
                    {**identity, "etag": response_etag or prior_etag, "expectedBytes": expected_bytes},
                )
                with partial.open("ab" if append else "wb") as writer:
                    shutil.copyfileobj(response, writer, length=8 * 1024 * 1024)
                actual_bytes = partial.stat().st_size
                if expected_bytes >= 0 and actual_bytes != expected_bytes:
                    raise BootstrapError(
                        f"部署包下载未完成，可保留 partial 后重试: expected={expected_bytes} actual={actual_bytes}"
                    )
        except (urllib.error.URLError, http.client.HTTPException, socket.timeout, TimeoutError, OSError) as exc:
            raise BootstrapError(f"部署包下载失败，可保留 partial 后重试: {exc}") from exc
    else:
        raise BootstrapError("部署包来源只允许 HTTPS、UNC 或本地文件。")
    actual = _sha256_file(partial)
    if actual != expected_sha256:
        partial.unlink(missing_ok=True)
        partial_metadata.unlink(missing_ok=True)
        raise BootstrapError(f"下载完成但 ZIP SHA-256 不匹配: expected={expected_sha256} actual={actual}；错误 partial 已删除")
    os.replace(partial, output)
    partial_metadata.unlink(missing_ok=True)
    return output


def _print_checks(results: list[CheckResult], as_json: bool) -> None:
    if as_json:
        print(json.dumps({"ready": all(item.status == "ready" for item in results), "checks": [asdict(item) for item in results]}, ensure_ascii=False, sort_keys=True))
        return
    labels = {"ready": "OK", "missing": "MISSING", "invalid": "INVALID"}
    for result in results:
        print(f"[{labels[result.status]:7}] {result.key}: {result.detail}")


def main() -> int:
    parser = argparse.ArgumentParser(description="RotoWeave clean-checkout bootstrap helper")
    parser.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parent.parent)
    subparsers = parser.add_subparsers(dest="command", required=True)

    check_parser = subparsers.add_parser("check")
    check_parser.add_argument("--role", choices=ROLE_COMPONENTS, default="client")
    check_parser.add_argument("--full-hash", action="store_true")
    check_parser.add_argument("--skip-environments", action="store_true")
    check_parser.add_argument("--strict-profiles", action="store_true")
    check_parser.add_argument("--json", action="store_true")

    plan_parser = subparsers.add_parser("plan")
    plan_parser.add_argument("--role", choices=ROLE_COMPONENTS, default="client")
    plan_parser.add_argument("--json", action="store_true")

    basic_build_parser = subparsers.add_parser("build-basic")
    basic_build_parser.add_argument("--replace-invalid", action="store_true")
    basic_build_parser.add_argument("--cache-root", type=Path)
    basic_build_parser.add_argument("--json", action="store_true")

    server_runtime_build_parser = subparsers.add_parser("build-server-runtimes")
    server_runtime_build_parser.add_argument("--replace-invalid", action="store_true")
    server_runtime_build_parser.add_argument("--cache-root", type=Path)
    server_runtime_build_parser.add_argument("--progress", action="store_true")
    server_runtime_build_parser.add_argument("--json", action="store_true")

    bundle_import_parser = subparsers.add_parser("import-bundle")
    bundle_import_parser.add_argument("--role", choices=ROLE_COMPONENTS, default="client")
    bundle_import_parser.add_argument("--bundle", type=Path, required=True)
    bundle_import_parser.add_argument("--expected-sha256")
    bundle_import_parser.add_argument("--replace-invalid", action="store_true")
    bundle_import_parser.add_argument("--json", action="store_true")

    bundle_select_parser = subparsers.add_parser("select-bundle")
    bundle_select_parser.add_argument("--role", choices=ROLE_COMPONENTS, default="client")
    bundle_select_parser.add_argument("--directory", type=Path, required=True)
    bundle_select_parser.add_argument("--json", action="store_true")

    bundle_inspect_parser = subparsers.add_parser("inspect-bundle")
    bundle_inspect_parser.add_argument("--role", choices=ROLE_COMPONENTS)
    bundle_inspect_parser.add_argument("--bundle", type=Path, required=True)
    bundle_inspect_parser.add_argument("--full-hash", action="store_true")
    bundle_inspect_parser.add_argument("--json", action="store_true")

    bundle_export_parser = subparsers.add_parser("export-bundle")
    bundle_export_parser.add_argument("--role", choices=ROLE_COMPONENTS, default="client")
    bundle_export_parser.add_argument("--output-directory", type=Path, required=True)
    bundle_export_parser.add_argument("--without-environment", action="store_true")
    bundle_export_parser.add_argument("--json-progress", action="store_true")

    bundle_download_parser = subparsers.add_parser("download-bundle")
    bundle_download_parser.add_argument("--source", required=True)
    bundle_download_parser.add_argument("--output", type=Path, required=True)
    bundle_download_parser.add_argument("--expected-sha256", required=True)
    bundle_download_parser.add_argument("--json", action="store_true")

    args = parser.parse_args()
    project_root = args.project_root.resolve(strict=True)
    try:
        if args.command == "check":
            results = collect_checks(project_root, args.role, full_hash=args.full_hash, skip_environments=args.skip_environments, strict_profiles=args.strict_profiles)
            _print_checks(results, args.json)
            return 0 if all(item.status == "ready" for item in results) else 2
        if args.command == "plan":
            result = deployment_plan(project_root, args.role)
            print(json.dumps(result, ensure_ascii=False, sort_keys=True) if args.json else json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.command == "build-basic":
            result = build_basic_from_source(
                project_root,
                replace_invalid=args.replace_invalid,
                cache_root=args.cache_root,
            )
            print(json.dumps(result, ensure_ascii=False, sort_keys=True) if args.json else json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.command == "build-server-runtimes":
            runtime_progress: ProgressCallback | None = _ConsoleProgressPrinter() if args.progress else None
            result = build_server_runtimes_from_source(
                project_root,
                replace_invalid=args.replace_invalid,
                cache_root=args.cache_root,
                progress=runtime_progress,
            )
            print(json.dumps(result, ensure_ascii=False, sort_keys=True) if args.json else json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.command == "inspect-bundle":
            result = inspect_bundle(project_root, args.bundle.resolve(strict=True), expected_role=args.role, full_hash=args.full_hash)
            print(json.dumps(result, ensure_ascii=False, sort_keys=True) if args.json else json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.command == "select-bundle":
            selected = select_bundle(project_root, args.role, args.directory.resolve(strict=True))
            result = {"bundlePath": str(selected)}
            print(json.dumps(result, ensure_ascii=False, sort_keys=True) if args.json else str(selected))
            return 0
        if args.command == "import-bundle":
            result = import_bundle(
                project_root,
                args.role,
                args.bundle.resolve(strict=True),
                expected_sha256=args.expected_sha256,
                replace_invalid=args.replace_invalid,
            )
            print(json.dumps(result, ensure_ascii=False, sort_keys=True) if args.json else json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.command == "download-bundle":
            output = download_bundle(args.source, args.output.resolve(strict=False), args.expected_sha256)
            result = {"bundlePath": str(output), "sha256": _sha256_file(output), "bytes": output.stat().st_size}
            print(json.dumps(result, ensure_ascii=False, sort_keys=True) if args.json else json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.command == "export-bundle":
            progress: ProgressCallback | None = None
            if args.json_progress:
                def write_progress(stage: str, value: float, message: str, detail: dict[str, Any] | None) -> None:
                    print(json.dumps({"type": "progress", "stage": stage, "progress": value, "message": message, "detail": detail or {}}, ensure_ascii=False, sort_keys=True), flush=True)
                progress = write_progress
            output, digest, manifest = export_bundle(
                project_root,
                args.role,
                args.output_directory,
                include_environment=not args.without_environment,
                progress=progress,
            )
            result = {
                "type": "result",
                "outputPath": str(output),
                "sha256": digest,
                "bytes": output.stat().st_size,
                "bundleId": manifest["bundleId"],
            }
            print(json.dumps(result, ensure_ascii=False, sort_keys=True), flush=True)
            return 0
    except (BootstrapError, FileNotFoundError, json.JSONDecodeError, OSError, zipfile.BadZipFile, subprocess.SubprocessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
