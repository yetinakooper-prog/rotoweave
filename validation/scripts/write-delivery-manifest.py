from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path


WORKSPACE = Path(__file__).resolve().parent.parent.parent
CLIENT_RELEASE = WORKSPACE / "RotoWeaveClient" / "release"
SERVER_RELEASE = WORKSPACE / "RotoWeaveServer" / "release"
OUTPUT_ROOT = WORKSPACE / "validation" / "artifacts"
CLIENT_MANIFEST = CLIENT_RELEASE / "launchers" / "CLIENT-MANIFEST.json"
SERVER_MANIFEST = SERVER_RELEASE / "server-only" / "SERVER-MANIFEST.json"
UNITY_PACKAGE = CLIENT_RELEASE / "RotoWeave-UnityImporter.unitypackage"
OUTPUT = OUTPUT_ROOT / "DELIVERY-MANIFEST.json"


def read_json(path: Path) -> dict[str, object]:
    if not path.is_file():
        raise SystemExit(f"Missing delivery input: {path}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise SystemExit(f"Invalid JSON object: {path}")
    return value


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    client = read_json(CLIENT_MANIFEST).get("client")
    server = read_json(SERVER_MANIFEST).get("server")
    if not isinstance(client, dict) or not isinstance(server, dict):
        raise SystemExit("Client or server manifest does not contain its release entry.")
    if not UNITY_PACKAGE.is_file():
        raise SystemExit(f"Missing Unity package: {UNITY_PACKAGE}")
    payload = {
        "schemaVersion": 1,
        "productVersion": "4.0.0",
        "createdAtUtc": datetime.now(timezone.utc).isoformat(),
        "client": {
            "manifest": CLIENT_MANIFEST.relative_to(CLIENT_RELEASE).as_posix(),
            "manifestSha256": sha256_file(CLIENT_MANIFEST),
            "artifactSha256": client.get("sha256"),
        },
        "server": {
            "manifest": SERVER_MANIFEST.relative_to(SERVER_RELEASE).as_posix(),
            "manifestSha256": sha256_file(SERVER_MANIFEST),
            "artifactSha256": server.get("sha256"),
        },
        "unity": {
            "path": UNITY_PACKAGE.relative_to(CLIENT_RELEASE).as_posix(),
            "sha256": sha256_file(UNITY_PACKAGE),
        },
    }
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"Delivery manifest: {OUTPUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
