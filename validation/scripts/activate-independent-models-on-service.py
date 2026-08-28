from __future__ import annotations

import argparse
import asyncio
import json
import time

import httpx


async def _request(client: httpx.AsyncClient, method: str, path: str, token: str, body: dict | None = None) -> dict:
    response = await client.request(method, path, headers={"X-RotoWeave-Admin-CSRF": token}, json=body)
    if response.status_code >= 400:
        raise RuntimeError(f"{method} {path} failed: {response.status_code} {response.text}")
    return response.json()


async def _wait(client: httpx.AsyncClient, operation_id: str, token: str, timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        operation = await _request(client, "GET", f"/api/admin/v2/model-operations/{operation_id}", token)
        if operation["state"] not in {"queued", "running"}:
            if operation["state"] != "passed":
                raise RuntimeError(f"{operation['kind']} failed: {operation.get('error')}")
            return operation
        await asyncio.sleep(0.25)
    raise TimeoutError(f"Model operation timed out: {operation_id}")


async def run(base_url: str, timeout: float) -> dict:
    async with httpx.AsyncClient(base_url=base_url, timeout=120.0) as client:
        deadline = time.monotonic() + timeout
        while True:
            try:
                session = await client.get("/api/admin/v2/session")
                if session.status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            if time.monotonic() >= deadline:
                raise TimeoutError("Admin service did not start.")
            await asyncio.sleep(0.25)
        token = session.json()["csrfToken"]
        snapshot = await _request(client, "GET", "/api/admin/v2/model-center", token)
        for operation in snapshot.get("operations") or []:
            if operation.get("state") in {"queued", "running"}:
                await _wait(client, str(operation["id"]), token, timeout)
        selection_result = await _request(
            client,
            "POST",
            "/api/admin/v2/model-selections/default",
            token,
            {},
        )
        selection = await _wait(
            client,
            str(selection_result["id"]),
            token,
            timeout,
        )
        verify = await _request(client, "POST", "/api/admin/v2/model-configurations/draft/verify", token, {})
        verify = await _wait(client, verify["id"], token, timeout)
        self_test = await _request(client, "POST", "/api/admin/v2/model-configurations/draft/self-test", token, {})
        self_test = await _wait(client, self_test["id"], token, timeout)
        activate = await _request(client, "POST", "/api/admin/v2/model-configurations/draft/activate", token, {})
        activate = await _wait(client, activate["id"], token, timeout)
        snapshot = await _request(client, "GET", "/api/admin/v2/model-center", token)
        active = snapshot.get("activeConfiguration") or {}
        return {
            "schemaVersion": 1,
            "admin": base_url,
            "selection": selection,
            "verify": verify,
            "selfTest": self_test,
            "activate": activate,
            "activeConfigurationDigest": active.get("configurationDigest"),
            "profiles": snapshot.get("profiles"),
            "slots": snapshot.get("slots"),
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--admin", default="http://127.0.0.1:8444")
    parser.add_argument("--timeout", type=float, default=7200.0)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(run(args.admin, args.timeout)), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
