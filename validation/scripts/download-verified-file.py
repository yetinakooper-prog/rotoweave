from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path


WORKSPACE = Path(__file__).resolve().parent.parent.parent


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(4 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def ensure_workspace_path(path: Path) -> Path:
    resolved = path.resolve()
    try:
        resolved.relative_to(WORKSPACE)
    except ValueError as exc:
        raise RuntimeError(f"Output must stay inside the workspace: {resolved}") from exc
    return resolved


def download_part(
    url: str,
    path: Path,
    start: int,
    end: int,
    retries: int,
) -> None:
    expected = end - start + 1
    for attempt in range(1, retries + 1):
        existing = path.stat().st_size if path.is_file() else 0
        if existing == expected:
            return
        if existing > expected:
            raise RuntimeError(f"Oversized partial file: {path}")
        request_start = start + existing
        request = urllib.request.Request(
            url,
            headers={
                "Range": f"bytes={request_start}-{end}",
                "User-Agent": "RotoWeave-Verified-Downloader/1",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                if response.status != 206:
                    raise RuntimeError(
                        f"Server ignored Range for {path.name}: HTTP {response.status}"
                    )
                content_range = str(response.headers.get("Content-Range") or "")
                if not content_range.startswith(f"bytes {request_start}-{end}/"):
                    raise RuntimeError(
                        f"Unexpected Content-Range for {path.name}: {content_range}"
                    )
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("ab") as handle:
                    while chunk := response.read(1024 * 1024):
                        handle.write(chunk)
        except (OSError, urllib.error.URLError, RuntimeError) as exc:
            if attempt >= retries:
                raise RuntimeError(
                    f"Part {path.name} failed after {attempt} attempts: {exc}"
                ) from exc
            time.sleep(min(2**attempt, 10))
            continue
        if path.stat().st_size == expected:
            return
    raise RuntimeError(f"Part did not reach expected length: {path}")


def download_verified(
    url: str,
    output: Path,
    expected_size: int,
    expected_sha256: str,
    chunks: int,
    retries: int,
) -> dict[str, object]:
    output = ensure_workspace_path(output)
    expected_sha256 = expected_sha256.lower()
    if output.is_file():
        actual = sha256_file(output)
        if output.stat().st_size == expected_size and actual == expected_sha256:
            return {
                "output": str(output),
                "size": expected_size,
                "sha256": actual,
                "reused": True,
            }
        raise RuntimeError(f"Refusing to overwrite mismatched output: {output}")

    chunks = max(1, min(chunks, expected_size))
    chunk_size = (expected_size + chunks - 1) // chunks
    parts_root = ensure_workspace_path(
        output.parent / f".{output.name}.parts"
    )
    parts_root.mkdir(parents=True, exist_ok=True)
    ranges: list[tuple[Path, int, int]] = []
    for index in range(chunks):
        start = index * chunk_size
        end = min(expected_size - 1, (index + 1) * chunk_size - 1)
        if start > end:
            break
        ranges.append((parts_root / f"part-{index:03d}.bin", start, end))

    stop = threading.Event()

    def report() -> None:
        while not stop.wait(10):
            transferred = sum(
                path.stat().st_size if path.is_file() else 0
                for path, _, _ in ranges
            )
            print(
                json.dumps(
                    {
                        "output": output.name,
                        "transferred": transferred,
                        "total": expected_size,
                    },
                    sort_keys=True,
                ),
                flush=True,
            )

    reporter = threading.Thread(target=report, name="download-progress", daemon=True)
    reporter.start()
    try:
        with ThreadPoolExecutor(max_workers=len(ranges)) as executor:
            futures = [
                executor.submit(download_part, url, path, start, end, retries)
                for path, start, end in ranges
            ]
            for future in as_completed(futures):
                future.result()
    finally:
        stop.set()
        reporter.join(timeout=1)

    temporary = ensure_workspace_path(output.with_name(output.name + ".assembling"))
    if temporary.exists():
        temporary.unlink()
    with temporary.open("wb") as target:
        for path, start, end in ranges:
            expected = end - start + 1
            if not path.is_file() or path.stat().st_size != expected:
                raise RuntimeError(f"Incomplete part before assembly: {path}")
            with path.open("rb") as source:
                shutil.copyfileobj(source, target, length=4 * 1024 * 1024)
    actual_size = temporary.stat().st_size
    actual_hash = sha256_file(temporary)
    if actual_size != expected_size or actual_hash != expected_sha256:
        raise RuntimeError(
            f"Downloaded file verification failed: size={actual_size}, sha256={actual_hash}"
        )
    os.replace(temporary, output)
    return {
        "output": str(output),
        "size": actual_size,
        "sha256": actual_hash,
        "reused": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Resume ranged downloads and accept only an exact size/SHA-256."
    )
    parser.add_argument("--url", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--size", type=int, required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--chunks", type=int, default=8)
    parser.add_argument("--retries", type=int, default=8)
    args = parser.parse_args()
    result = download_verified(
        args.url,
        args.output,
        args.size,
        args.sha256,
        args.chunks,
        args.retries,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True), flush=True)


if __name__ == "__main__":
    main()

