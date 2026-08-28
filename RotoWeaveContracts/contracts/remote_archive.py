from __future__ import annotations

import hashlib
from typing import Callable

from .integrity import canonical_sha256, canonical_transport_json_bytes
from .remote_protocol import RemoteResultManifest


IDEMPOTENCY_HEADER = "Idempotency-Key"
PROTOCOL_HEADER = "X-RotoWeave-Protocol-Version"
LEGACY_PROTOCOL_HEADER = "X-AIFrame-Protocol-Version"
ARCHIVE_SHA256_HEADER = "X-Archive-SHA256"
RESULT_MANIFEST_PATH = "result.json"


def result_payload_sha256(
    manifest: RemoteResultManifest,
    read_member: Callable[[str], bytes],
) -> str:
    payload = manifest.model_dump(mode="json")
    payload["archiveSha256"] = ""
    digest = hashlib.sha256(canonical_transport_json_bytes(payload))
    for frame in sorted(manifest.frames, key=lambda item: item.ordinal):
        for member in (frame.rgbaPath, frame.emissionPath):
            if member:
                digest.update(member.encode("utf-8"))
                digest.update(b"\0")
                digest.update(read_member(member))
    return digest.hexdigest()


__all__ = [
    "ARCHIVE_SHA256_HEADER",
    "IDEMPOTENCY_HEADER",
    "LEGACY_PROTOCOL_HEADER",
    "PROTOCOL_HEADER",
    "RESULT_MANIFEST_PATH",
    "canonical_sha256",
    "result_payload_sha256",
]
