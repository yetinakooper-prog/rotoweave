from __future__ import annotations

import json
import re
from pathlib import Path


WORKSPACE = Path(__file__).resolve().parent.parent.parent


def _require(pattern: str, text: str, label: str) -> str:
    match = re.search(pattern, text)
    if not match:
        raise SystemExit(f"Missing product contract value: {label}")
    return match.group(1)


def validate() -> None:
    contracts_root = WORKSPACE / "RotoWeaveContracts"
    client_root = WORKSPACE / "RotoWeaveClient"
    server_root = WORKSPACE / "RotoWeaveServer"
    product = json.loads((contracts_root / "product.json").read_text(encoding="utf-8"))
    protocols = json.loads(
        (contracts_root / "contracts" / "protocols.json").read_text(encoding="utf-8")
    )
    json.loads(
        (contracts_root / "contracts" / "matting-v1.schema.json").read_text(
            encoding="utf-8"
        )
    )
    version = str(product["version"])
    runtime = product["runtime"]
    contracts = product["contracts"]

    expected_runtime = {
        "applicationDataDirectory": "RotoWeave-4.0",
        "developmentDataDirectory": "RotoWeave-4.0-Dev",
        "singleInstanceMutex": r"Local\RotoWeave.SingleInstance.v4",
        "sessionCookieName": "rotoweave_v4_session",
        "apiPort": 8766,
        "webDevelopmentPort": 3000,
    }
    if runtime != expected_runtime:
        raise SystemExit(
            f"Product runtime identity drift: expected {expected_runtime}, found {runtime}"
        )

    expected_contracts = {
        "httpApi": 4,
        "remoteMattingApi": 1,
        "workspaceFormat": 3,
        "runtimeDatabaseSchema": 4,
        "canonicalPixelsPerUnit": 100.0,
        "coordinateContract": "frame-transform-unity-curves-v3",
        "characterPackageFormat": 3,
        "characterPackageShape": "deduplicated-atlas-v3",
        "unityScriptedImporter": 8,
    }
    if contracts != expected_contracts:
        raise SystemExit(
            f"Public contract drift: expected {expected_contracts}, found {contracts}"
        )

    expected_protocols = {
        "schemaVersion": 1,
        "productVersion": version,
        "localApi": {"version": 4, "prefix": "/api/v4"},
    }
    for key, expected in expected_protocols.items():
        if protocols.get(key) != expected:
            raise SystemExit(
                f"Protocol manifest drift at {key}: expected {expected}, "
                f"found {protocols.get(key)}"
            )
    remote = protocols.get("remoteMattingApi")
    if not isinstance(remote, dict) or {
        "version": remote.get("version"),
        "prefix": remote.get("prefix"),
        "transport": remote.get("transport"),
        "authentication": remote.get("authentication"),
        "eventType": remote.get("eventType"),
        "resultType": remote.get("resultType"),
    } != {
        "version": 1,
        "prefix": "/api/matting/v1",
        "transport": "http",
        "authentication": {"scheme": "none"},
        "eventType": "text/event-stream",
        "resultType": "application/zip",
    }:
        raise SystemExit(f"Remote matting protocol drift: {remote}")

    package = json.loads((client_root / "package.json").read_text(encoding="utf-8"))
    package_lock = json.loads(
        (client_root / "package-lock.json").read_text(encoding="utf-8")
    )
    contract_package = json.loads((contracts_root / "package.json").read_text(encoding="utf-8"))
    admin_package = json.loads((server_root / "server-admin/package.json").read_text(encoding="utf-8"))
    versions = {
        "package.json": str(package.get("version") or ""),
        "package-lock.json": str(package_lock.get("version") or ""),
        "package-lock root": str(
            package_lock.get("packages", {}).get("", {}).get("version") or ""
        ),
        "RotoWeaveContracts/package.json": str(contract_package.get("version") or ""),
        "RotoWeaveServer/server-admin/package.json": str(admin_package.get("version") or ""),
    }
    drift = {name: value for name, value in versions.items() if value != version}
    if drift:
        raise SystemExit(f"Product version drift: expected {version}, found {drift}")
    client_start = (client_root / "Start.ps1").read_text(encoding="utf-8")
    server_start = (server_root / "Start.ps1").read_text(encoding="utf-8")
    vite = (client_root / "vite.config.ts").read_text(encoding="utf-8")
    launcher = (client_root / "backend/launcher.py").read_text(encoding="utf-8")
    runtime_consumers = {
        "RotoWeaveClient/Start.ps1": (
            client_start,
            (
                "ROTOWEAVE_MODELS_ROOT",
                "RotoWeaveContracts",
                "npm.cmd run build",
                "backend.client_launcher",
            ),
        ),
        "RotoWeaveServer/Start.ps1": (
            server_start,
            (
                "ROTOWEAVE_MODELS_ROOT",
                "RotoWeaveContracts",
                "npm.cmd run build",
                "server.launcher",
            ),
        ),
        "RotoWeaveClient/vite.config.ts": (
            vite,
            (
                "productContract.runtime.apiPort",
                "productContract.runtime.webDevelopmentPort",
            ),
        ),
        "backend/launcher.py": (
            launcher,
            ("RUNTIME_SINGLE_INSTANCE_MUTEX",),
        ),
        "RotoWeaveClient/backend/app/main.py": (
            (client_root / "backend/app/main.py").read_text(encoding="utf-8"),
            ("create_v4_router",),
        ),
        "RotoWeaveContracts/contracts/remote_protocol.py": (
            (contracts_root / "contracts/remote_protocol.py").read_text(encoding="utf-8"),
            (
                "REMOTE_MATTING_API_PREFIX",
                "REMOTE_MATTING_API_VERSION",
                "require_contract_version",
            ),
        ),
        "RotoWeaveClient/app/lib/protocol-contract.ts": (
            (client_root / "app/lib/protocol-contract.ts").read_text(encoding="utf-8"),
            (
                "IncompatibleProtocolError",
                "LOCAL_API_PREFIX",
                "REMOTE_MATTING_API_PREFIX",
            ),
        ),
        "RotoWeaveClient/backend/app/domain_character_exporter.py": (
            (client_root / "backend/app/domain_character_exporter.py").read_text(
                encoding="utf-8"
            ),
            ("CHARACTER_PACKAGE_FORMAT", "CHARACTER_PACKAGE_SHAPE"),
        ),
    }
    for name, (text, needles) in runtime_consumers.items():
        missing = [needle for needle in needles if needle not in text]
        if missing:
            raise SystemExit(f"{name} does not consume runtime identity: {missing}")

    retired_product_files = (
        client_root / "backend/app/character_exporter.py",
        client_root / "backend/app/model_pack.py",
        contracts_root / "contracts/model_pack.py",
        contracts_root / "tools/model-pack-tool.py",
        server_root / "server/api_admin_v1.py",
    )
    unexpected = [str(path.relative_to(WORKSPACE)) for path in retired_product_files if path.exists()]
    if unexpected:
        raise SystemExit(f"Retired compatibility files still exist: {unexpected}")

    vite_watch_ignores = (
        "**/artifacts/**",
        "**/tmp/**",
        "**/.svn/**",
        "**/.git/**",
    )
    missing_vite_ignores = [value for value in vite_watch_ignores if value not in vite]
    if missing_vite_ignores:
        raise SystemExit(
            f"vite.config.ts misses large workspace ignores: {missing_vite_ignores}"
        )

    source = (
        WORKSPACE
        / "RotoWeaveClient/unity/RotoWeave-UnityImporter/Assets/RotoWeave/Editor/RotoWeaveProductContract.cs"
    ).read_text(encoding="utf-8")
    unity_values = {
        "version": _require(
            r'ProductVersion\s*=\s*"([^"]+)"', source, "Unity product version"
        ),
        "canonicalPixelsPerUnit": float(
            _require(
                r"CanonicalPixelsPerUnit\s*=\s*([0-9.]+)f",
                source,
                "canonical pixels per unit",
            )
        ),
        "coordinateContract": _require(
            r'CoordinateContract\s*=\s*"([^"]+)"',
            source,
            "coordinate contract",
        ),
        "characterPackageFormat": int(
            _require(r"CharacterPackageFormat\s*=\s*(\d+)", source, "package format")
        ),
        "characterPackageShape": _require(
            r'CharacterPackageShape\s*=\s*"([^"]+)"', source, "package shape"
        ),
        "unityScriptedImporter": int(
            _require(
                r"ScriptedImporterVersion\s*=\s*(\d+)",
                source,
                "ScriptedImporter version",
            )
        ),
    }
    expected_unity = {
        "version": version,
        **{
            key: contracts[key]
            for key in (
                "canonicalPixelsPerUnit",
                "coordinateContract",
                "characterPackageFormat",
                "characterPackageShape",
                "unityScriptedImporter",
            )
        },
    }
    if unity_values != expected_unity:
        raise SystemExit(
            f"Unity product contract drift: expected {expected_unity}, "
            f"found {unity_values}"
        )

    print(
        "Product contract OK: "
        f"{product['product']} {version}, port {runtime['apiPort']}, data {runtime['applicationDataDirectory']}, "
        f"Workspace {contracts['workspaceFormat']}, Runtime DB {contracts['runtimeDatabaseSchema']}, "
        f"package {contracts['characterPackageFormat']}/"
        f"{contracts['characterPackageShape']}, importer "
        f"{contracts['unityScriptedImporter']}, independent-models-only"
    )


if __name__ == "__main__":
    validate()
