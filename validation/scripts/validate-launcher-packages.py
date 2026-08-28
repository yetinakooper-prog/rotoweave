from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
from typing import Any

WORKSPACE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(WORKSPACE / "RotoWeaveContracts"))

from contracts.integrity import canonical_sha256


FORBIDDEN_SERVER_NAMES = {
    "model-pack.json",
    "model-pack.sig.json",
    "model-pack-public-key.hex",
    "self-test-receipt.json",
    "server-key.pem",
    "lan-ca-key.pem",
    "bearer-token.txt",
}
CLIENT_REQUIRED_FILES = {
    "contracts/matting-v1.schema.json",
    "contracts/protocols.json",
    "frontend/index.html",
}
FORBIDDEN_SERVER_WEIGHT_SUFFIXES = {".pt", ".pth", ".safetensors", ".ckpt"}


def runtime_root(root: Path) -> Path:
    internal = root / "_internal"
    return internal if internal.is_dir() else root


def product_version(root: Path) -> str:
    payload = json.loads((runtime_root(root) / "product.json").read_text(encoding="utf-8"))
    return str(payload.get("version") or "")


def require_files(root: Path, relative_paths: set[str], *, label: str) -> None:
    missing = sorted(path for path in relative_paths if not (root / path).is_file())
    if missing:
        raise SystemExit(f"{label}缺少运行文件：{', '.join(missing)}")


def validate_client_frontend(internal: Path) -> None:
    index = internal / "frontend" / "index.html"
    html = index.read_text(encoding="utf-8")
    references = {
        match.lstrip("/")
        for match in re.findall(r'''(?:src|href)=["']([^"']+)["']''', html)
        if not match.startswith(("http://", "https://", "data:"))
    }
    missing = sorted(reference for reference in references if not (internal / "frontend" / reference).is_file())
    if missing:
        raise SystemExit("客户端 React 生产前端引用缺失：" + ", ".join(missing))


def validate_client(root: Path) -> dict[str, Any]:
    executable = root / "RotoWeave-Client.exe"
    internal = runtime_root(root)
    if not executable.is_file():
        raise SystemExit(f"客户端启动器不存在：{executable}")
    if product_version(root) != "4.0.0":
        raise SystemExit("客户端启动器未携带 4.0.0 产品契约。")
    require_files(internal, CLIENT_REQUIRED_FILES, label="客户端启动器")
    protocols = json.loads((internal / "contracts" / "protocols.json").read_text(encoding="utf-8"))
    if protocols.get("schemaVersion") != 1 or protocols.get("productVersion") != "4.0.0":
        raise SystemExit("客户端公共协议清单版本无效。")
    validate_client_frontend(internal)
    models = internal / "models"
    actual = {path.relative_to(models).as_posix() for path in models.rglob("*") if path.is_file()}
    if actual:
        raise SystemExit(f"客户端发行包不得内置动态生成的 Basic 模型：{sorted(actual)}")
    return {"executable": str(executable), "bytes": executable.stat().st_size}


def validate_server(root: Path) -> dict[str, Any]:
    executable = root / "RotoWeave-Server.exe"
    internal = runtime_root(root)
    if not executable.is_file():
        raise SystemExit(f"服务端启动器不存在：{executable}")
    if product_version(root) != "4.0.0":
        raise SystemExit("服务端启动器未携带 4.0.0 产品契约。")
    if (internal / "frontend").exists() or (internal / "models").exists():
        raise SystemExit("服务端启动器不得捆绑客户端前端或模型目录。")
    if not (internal / "server-admin" / "index.html").is_file():
        raise SystemExit("服务端启动器缺少独立 localhost 管理后台。")
    forbidden = [
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and path.name.casefold() in FORBIDDEN_SERVER_NAMES
    ]
    if forbidden:
        raise SystemExit("服务端启动器包含秘密或模型包元数据：" + ", ".join(forbidden))
    runtime_ids = []
    for profile in ("high", "ultra"):
        profile_root = internal / "server-runtimes" / profile
        manifest_path = profile_root / "runtime-manifest.json"
        if not manifest_path.is_file():
            raise SystemExit(f"服务端启动器缺少 {profile} 固定运行时清单。")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("profile") != profile or not re.fullmatch(r"[0-9a-f]{64}", str(manifest.get("digest") or "")):
            raise SystemExit(f"服务端 {profile} 固定运行时身份无效。")
        identity = dict(manifest)
        observed_digest = str(identity.pop("digest"))
        if canonical_sha256(identity) != observed_digest:
            raise SystemExit(f"服务端 {profile} 固定运行时清单摘要无效。")
        if not (profile_root / str(manifest.get("pythonRelativePath") or "")).is_file():
            raise SystemExit(f"服务端 {profile} 固定运行时 Python 缺失。")
        runtime_ids.append(str(manifest["id"]))
    weights = sorted(
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and path.suffix.casefold() in FORBIDDEN_SERVER_WEIGHT_SUFFIXES
    )
    if weights:
        raise SystemExit("服务端启动器不得包含模型权重：" + ", ".join(weights))
    return {"executable": str(executable), "bytes": executable.stat().st_size, "fixedRuntimes": runtime_ids}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--client", type=Path)
    parser.add_argument("--server", type=Path)
    args = parser.parse_args()
    if args.client is None and args.server is None:
        parser.error("at least one of --client or --server is required")
    result: dict[str, Any] = {
        "schemaVersion": 1,
        "productVersion": "4.0.0",
    }
    if args.client is not None:
        result["client"] = validate_client(args.client.resolve())
    if args.server is not None:
        result["server"] = validate_server(args.server.resolve())
        result["serverModelWeightsIncluded"] = False
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
