from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import shutil
import stat
import sys
import types
import urllib.error
import zipfile
from pathlib import Path

import pytest


WORKSPACE = Path(__file__).resolve().parents[2]
SCRIPT = WORKSPACE / "scripts" / "rotoweave_bootstrap.py"
SPEC = importlib.util.spec_from_file_location("rotoweave_bootstrap_v4", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
bootstrap = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = bootstrap
SPEC.loader.exec_module(bootstrap)


class _ScriptedResponse:
    def __init__(self, status: int, headers: dict[str, str], chunks: list[bytes | Exception]) -> None:
        self.status = status
        self.headers = headers
        self._chunks = list(chunks)

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def geturl(self) -> str:
        return "https://downloads.example.invalid/test-component.bin"

    def read(self, _size: int) -> bytes:
        if not self._chunks:
            return b""
        item = self._chunks.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _download_source(content: bytes) -> dict[str, object]:
    return {
        "id": "test-component",
        "url": "https://downloads.example.invalid/test-component.bin",
        "bytes": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
    }


def _project(root: Path, *, with_basic: bool = True) -> Path:
    (root / "RotoWeaveContracts").mkdir(parents=True)
    (root / "RotoWeaveContracts" / "product.json").write_text('{"version":"4.0.0"}', encoding="utf-8")
    (root / "RotoWeaveContracts" / "deployment-protocol.json").write_text(
        '{"schemaVersion":2,"platform":"windows-x64"}', encoding="utf-8"
    )
    (root / "RotoWeaveContracts" / "deployment-sources.json").write_text(
        '{"schemaVersion":1,"platform":"windows-x64","bundleSources":[],"componentSources":[],"guidedHostSources":[]}',
        encoding="utf-8",
    )
    contract = json.loads(
        (WORKSPACE / "RotoWeaveContracts" / "basic-assets.json").read_text(encoding="utf-8")
    )
    test_license = b"test-only-license"
    contract["licenseBytes"] = len(test_license)
    contract["licenseSha256"] = hashlib.sha256(test_license).hexdigest()
    contract["licenseUrl"] = "https://downloads.example.invalid/LICENSE"
    contract_bytes = json.dumps(contract, sort_keys=True).encode("utf-8")
    contract_path = root / "RotoWeaveContracts" / "basic-assets.json"
    contract_path.write_bytes(contract_bytes)
    client_license = root / "RotoWeaveClient" / "licenses" / "LICENSE-BiRefNet.txt"
    client_license.parent.mkdir(parents=True)
    client_license.write_bytes(test_license)
    requirements = root / "RotoWeaveClient" / "requirements-basic-export-lock.txt"
    requirements.write_bytes(
        (WORKSPACE / "RotoWeaveClient" / "requirements-basic-export-lock.txt").read_bytes()
    )
    if with_basic:
        basic = root / "RotoWeaveModels" / "application" / "basic"
        basic.mkdir(parents=True)
        onnx = basic / contract["onnxFile"]
        self_test = basic / contract["selfTest"]["file"]
        onnx.write_bytes(b"verified-basic-onnx")
        self_test.write_bytes(b"verified-basic-self-test")
        (basic / contract["licenseFile"]).write_bytes(client_license.read_bytes())
        manifest = dict(contract)
        manifest.pop("validation")
        manifest["contractSha256"] = hashlib.sha256(contract_bytes).hexdigest()
        manifest["onnxSha256"] = hashlib.sha256(onnx.read_bytes()).hexdigest()
        manifest["pytorchOnnxCpuMaxAbs"] = 0.0
        manifest["requirementsSha256"] = hashlib.sha256(requirements.read_bytes()).hexdigest()
        manifest["selfTest"] = {
            **contract["selfTest"],
            "sha256": hashlib.sha256(self_test.read_bytes()).hexdigest(),
        }
        (basic / "birefnet-lite-matting.manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
    return root


def _export_client_bundle(project: Path, output: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, str]:
    monkeypatch.setattr(bootstrap.platform, "system", lambda: "Windows")
    cache = project / "Temp" / "test-bundle-cache"
    environment = cache / "client-python"
    toolchains = cache / "toolchains"
    environment.mkdir(parents=True)
    toolchains.mkdir(parents=True)
    (environment / "asset.bin").write_bytes(b"verified-environment")
    (toolchains / "tool.bin").write_bytes(b"verified-toolchain")
    monkeypatch.setattr(
        bootstrap,
        "prepare_environment_cache",
        lambda *_args, **_kwargs: {"client-python": environment},
    )
    monkeypatch.setattr(
        bootstrap,
        "prepare_toolchain_cache",
        lambda *_args, **_kwargs: toolchains,
    )
    bundle, digest, _ = bootstrap.export_bundle(project, "client", output, include_environment=True)
    return bundle, digest


def _rewrite_zip(source: Path, target: Path, transform) -> None:
    with zipfile.ZipFile(source, "r") as reader, zipfile.ZipFile(target, "w", allowZip64=True) as writer:
        for info in reader.infolist():
            data = reader.read(info.filename)
            new_info, new_data = transform(info, data)
            writer.writestr(new_info, new_data, compress_type=info.compress_type)


def _schema2_manifest() -> dict[str, object]:
    return {
        "schemaVersion": 2,
        "bundleId": "bundle-" + "a" * 32,
        "productVersion": "4.0.0",
        "role": "client",
        "platform": "windows-x64",
        "compatibilityDigest": "0" * 64,
        "sourceRevision": "185",
        "createdAtUtc": "2026-08-26T00:00:00Z",
        "treeAlgorithm": "sha256-tree-v1",
        "components": {},
        "toolchainVersions": [],
        "licenses": [],
    }


def test_bundle_reader_accepts_exact_legacy_manifest_and_rejects_dual_identity() -> None:
    payload = json.dumps(_schema2_manifest()).encode("utf-8")
    legacy_buffer = io.BytesIO()
    with zipfile.ZipFile(legacy_buffer, "w") as writer:
        writer.writestr(bootstrap.LEGACY_MANIFEST_NAME, payload)
    legacy_buffer.seek(0)
    with zipfile.ZipFile(legacy_buffer, "r") as archive:
        assert bootstrap._read_bundle_manifest(archive)["bundleId"] == "bundle-" + "a" * 32

    dual_buffer = io.BytesIO()
    with zipfile.ZipFile(dual_buffer, "w") as writer:
        writer.writestr(bootstrap.MANIFEST_NAME, payload)
        writer.writestr(bootstrap.LEGACY_MANIFEST_NAME, payload)
    dual_buffer.seek(0)
    with zipfile.ZipFile(dual_buffer, "r") as archive:
        with pytest.raises(bootstrap.BootstrapError, match="同时包含"):
            bootstrap._read_bundle_manifest(archive)


def test_root_bootstrap_routes_roles_to_independent_components() -> None:
    assert bootstrap.ROLE_COMPONENTS == {
        "client": (),
        "server": ("server-runtimes",),
        "all": ("server-runtimes",),
    }
    assert bootstrap.ROLE_ENVIRONMENTS["all"] == (
        "client-python", "client-node", "server-python", "server-node"
    )


def test_default_server_check_does_not_gate_on_profile_models(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bootstrap, "check_host", lambda _root: [])
    monkeypatch.setattr(
        bootstrap,
        "check_server_runtimes",
        lambda _root: bootstrap.CheckResult("server-runtimes", "ready", "ready"),
    )
    called = []
    monkeypatch.setattr(
        bootstrap,
        "check_server_models",
        lambda *_args, **_kwargs: called.append(True)
        or bootstrap.CheckResult("server-models", "missing", "profile unavailable"),
    )
    default = bootstrap.collect_checks(
        tmp_path, "server", full_hash=False, skip_environments=True, strict_profiles=False
    )
    assert [item.key for item in default] == ["server-runtimes"]
    assert called == []
    strict = bootstrap.collect_checks(
        tmp_path, "server", full_hash=False, skip_environments=True, strict_profiles=True
    )
    assert [item.key for item in strict] == ["server-runtimes", "server-models"]
    assert called == [True]


def test_native_command_output_uses_byte_capture_and_windows_encoding_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append(command)
        assert kwargs["capture_output"] is True
        assert kwargs["text"] is False
        if command[0] == "tool.exe":
            return types.SimpleNamespace(returncode=0, stdout="版本 1.2.3".encode("cp936"), stderr=b"")
        return types.SimpleNamespace(returncode=1, stdout=b"", stderr="不是工作副本".encode("cp936"))

    monkeypatch.setattr(bootstrap.shutil, "which", lambda _command: "tool.exe")
    monkeypatch.setattr(bootstrap.subprocess, "run", fake_run)

    assert bootstrap._command_version("tool", ("--version",)) == "版本 1.2.3"
    assert bootstrap._source_revision(tmp_path) == "unknown"
    assert len(calls) == 3


def test_setup_has_no_blocking_profile_escape_hatch_or_runonce() -> None:
    setup = (WORKSPACE / "scripts" / "Setup-RotoWeave.ps1").read_text(encoding="utf-8")
    assert "AllowBlockedProfiles" not in setup
    assert "CurrentVersion\\RunOnce" not in setup
    assert "profileCandidate" in setup
    assert "build-basic" in setup
    assert "build-server-runtimes" in setup
    assert '"build-server-runtimes", "--progress", "--json"' in setup
    assert "Basic 模型未就绪" in setup
    assert "Setup 不扫描、下载" in setup


def test_component_download_resumes_interrupted_transfer_with_range_and_etag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content = b"abcdefghij"
    requests = []
    responses = [
        _ScriptedResponse(200, {"Content-Length": "10", "ETag": '"v1"'}, [b"abcd", urllib.error.URLError("cut")]),
        _ScriptedResponse(206, {"Content-Length": "6", "Content-Range": "bytes 4-9/10", "ETag": '"v1"'}, [b"efghij"]),
    ]

    def fake_urlopen(request, timeout):
        requests.append(request)
        assert timeout == 60
        return responses.pop(0)

    monkeypatch.setattr(bootstrap.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(bootstrap.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(bootstrap, "COMPONENT_DOWNLOAD_ATTEMPTS", 2)
    target = tmp_path / "component.bin"
    bootstrap._download_verified_file(_download_source(content), target)

    assert target.read_bytes() == content
    assert requests[0].get_header("Range") is None
    assert requests[1].get_header("Range") == "bytes=4-"
    assert requests[1].get_header("If-range") == '"v1"'
    assert not target.with_suffix(".bin.partial").exists()
    assert not target.with_suffix(".bin.partial.json").exists()


def test_component_download_discards_partial_when_etag_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content = b"abcdefghij"
    source = _download_source(content)
    target = tmp_path / "component.bin"
    partial = target.with_suffix(".bin.partial")
    metadata = target.with_suffix(".bin.partial.json")
    partial.write_bytes(b"abcd")
    metadata.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "id": source["id"],
                "url": source["url"],
                "expectedBytes": source["bytes"],
                "expectedSha256": source["sha256"],
                "etag": '"v1"',
            }
        ),
        encoding="utf-8",
    )
    requests = []
    responses = [
        _ScriptedResponse(206, {"Content-Length": "6", "Content-Range": "bytes 4-9/10", "ETag": '"v2"'}, []),
        _ScriptedResponse(200, {"Content-Length": "10", "ETag": '"v2"'}, [content]),
    ]

    def fake_urlopen(request, timeout):
        requests.append(request)
        return responses.pop(0)

    monkeypatch.setattr(bootstrap.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(bootstrap.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(bootstrap, "COMPONENT_DOWNLOAD_ATTEMPTS", 2)
    bootstrap._download_verified_file(source, target)

    assert target.read_bytes() == content
    assert requests[0].get_header("Range") == "bytes=4-"
    assert requests[1].get_header("Range") is None


def test_component_download_reports_remote_length_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content = b"abcdefghij"
    monkeypatch.setattr(
        bootstrap.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: _ScriptedResponse(200, {"Content-Length": "9"}, []),
    )
    monkeypatch.setattr(bootstrap, "COMPONENT_DOWNLOAD_ATTEMPTS", 1)

    with pytest.raises(bootstrap.BootstrapError, match=r"expected=10 remote=9"):
        bootstrap._download_verified_file(_download_source(content), tmp_path / "component.bin")


def test_component_download_keeps_safe_partial_after_retry_exhaustion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content = b"abcdefghij"
    responses = [
        _ScriptedResponse(200, {"Content-Length": "10", "ETag": '"v1"'}, [b"ab", urllib.error.URLError("cut-1")]),
        _ScriptedResponse(206, {"Content-Length": "8", "Content-Range": "bytes 2-9/10", "ETag": '"v1"'}, [b"c", urllib.error.URLError("cut-2")]),
    ]
    monkeypatch.setattr(bootstrap.urllib.request, "urlopen", lambda *_args, **_kwargs: responses.pop(0))
    monkeypatch.setattr(bootstrap.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(bootstrap, "COMPONENT_DOWNLOAD_ATTEMPTS", 2)
    target = tmp_path / "component.bin"

    with pytest.raises(bootstrap.BootstrapError, match=r"expected=10 actual=3.*partial 已保留"):
        bootstrap._download_verified_file(_download_source(content), target)

    assert target.with_suffix(".bin.partial").read_bytes() == b"abc"
    assert target.with_suffix(".bin.partial.json").is_file()


def test_segmented_download_uses_exact_ranges_etag_and_final_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content = b"abcdefghij"
    requests = []
    progress_events = []

    def fake_urlopen(request, timeout):
        requests.append(request)
        assert timeout == 60
        requested_range = request.get_header("Range")
        assert requested_range in {"bytes=0-4", "bytes=5-9"}
        start, end = (int(value) for value in requested_range.removeprefix("bytes=").split("-"))
        return _ScriptedResponse(
            206,
            {
                "Content-Length": str(end - start + 1),
                "Content-Range": f"bytes {start}-{end}/{len(content)}",
                "ETag": '"fixed"',
            },
            [content[start : end + 1]],
        )

    source = {**_download_source(content), "segments": 2, "etag": '"fixed"'}
    target = tmp_path / "large-wheel.whl"
    monkeypatch.setattr(bootstrap, "SEGMENT_TARGET_BYTES", 4)
    monkeypatch.setattr(bootstrap.urllib.request, "urlopen", fake_urlopen)
    bootstrap._download_segmented_verified_file(
        source,
        target,
        progress=lambda stage, value, message, detail: progress_events.append((stage, value, message, detail)),
    )

    assert target.read_bytes() == content
    assert {request.get_header("Range") for request in requests} == {"bytes=0-4", "bytes=5-9"}
    assert all(request.get_header("If-range") == '"fixed"' for request in requests)
    assert not target.with_name(target.name + ".parts").exists()
    assert any(
        detail["downloadedBytes"] == len(content)
        and detail["expectedBytes"] == len(content)
        and detail["completedSegments"] == 2
        for _, _, _, detail in progress_events
        if detail
    )


def test_console_progress_formats_bytes_speed_eta_segments_and_elapsed() -> None:
    stream = io.StringIO()
    progress = bootstrap._ConsoleProgressPrinter(stream=stream, minimum_interval=0)

    progress(
        "server-runtime-wheel-download",
        0.25,
        "正在分段下载 torch",
        {
            "id": "torch",
            "downloadedBytes": 512 * 1024 * 1024,
            "expectedBytes": 1024 * 1024 * 1024,
            "bytesPerSecond": 16 * 1024 * 1024,
            "etaSeconds": 32,
            "completedSegments": 8,
            "totalSegments": 16,
            "elapsedSeconds": 30,
            "force": True,
        },
    )

    output = stream.getvalue()
    assert "Server Runtime  25.0%" in output
    assert "512.00 MiB / 1.00 GiB (50.0%)" in output
    assert "16.00 MiB/s" in output
    assert "预计剩余 00:32" in output
    assert "分段 8/16" in output
    assert "已运行 00:30" in output


def test_console_progress_uses_stderr_without_polluting_result_json(capsys: pytest.CaptureFixture[str]) -> None:
    progress = bootstrap._ConsoleProgressPrinter(minimum_interval=0)
    progress("server-runtime-preflight", 0.0, "正在检查", {"force": True})
    print('{"built":true}')

    captured = capsys.readouterr()
    assert captured.out == '{"built":true}\n'
    assert "[Server Runtime   0.0%] 正在检查" in captured.err


def test_runtime_command_emits_heartbeat_until_process_completes(monkeypatch: pytest.MonkeyPatch) -> None:
    events = []

    class FakeProcess:
        waits = 0

        def wait(self, timeout):
            assert timeout == bootstrap.RUNTIME_HEARTBEAT_SECONDS
            self.waits += 1
            if self.waits == 1:
                raise bootstrap.subprocess.TimeoutExpired(["fake"], timeout)
            return 0

    monkeypatch.setattr(bootstrap.subprocess, "Popen", lambda *_args, **_kwargs: FakeProcess())
    bootstrap._run_runtime_checked(
        ["fake"],
        "安装测试依赖",
        progress=lambda stage, value, message, detail: events.append((stage, value, message, detail)),
        progress_value=0.5,
    )

    assert [message for _, _, message, _ in events] == [
        "开始安装测试依赖",
        "安装测试依赖仍在执行，请勿关闭窗口",
        "安装测试依赖完成",
    ]


def test_segmented_download_rejects_changed_etag_without_promoting_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content = b"abcdefghij"

    def fake_urlopen(request, timeout):
        assert timeout == 60
        requested_range = request.get_header("Range")
        start, end = (int(value) for value in requested_range.removeprefix("bytes=").split("-"))
        return _ScriptedResponse(
            206,
            {
                "Content-Length": str(end - start + 1),
                "Content-Range": f"bytes {start}-{end}/{len(content)}",
                "ETag": '"changed"',
            },
            [content[start : end + 1]],
        )

    source = {**_download_source(content), "segments": 2, "etag": '"fixed"'}
    target = tmp_path / "large-wheel.whl"
    monkeypatch.setattr(bootstrap, "SEGMENT_TARGET_BYTES", 4)
    monkeypatch.setattr(bootstrap, "COMPONENT_DOWNLOAD_ATTEMPTS", 1)
    monkeypatch.setattr(bootstrap.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(bootstrap.BootstrapError, match="ETag 不匹配"):
        bootstrap._download_segmented_verified_file(source, target)
    assert not target.exists()


def test_compatibility_digest_ignores_document_only_changes(tmp_path: Path) -> None:
    project = _project(tmp_path / "project")
    before = bootstrap.compatibility_digest(project)
    (project / "Docs").mkdir()
    (project / "Docs" / "note.md").write_text("documentation change", encoding="utf-8")
    assert bootstrap.compatibility_digest(project) == before
    (project / "RotoWeaveClient").mkdir(exist_ok=True)
    (project / "RotoWeaveClient" / "package-lock.json").write_text("lock", encoding="utf-8")
    assert bootstrap.compatibility_digest(project) != before


def test_compatibility_digest_changes_with_deployment_protocol(tmp_path: Path) -> None:
    project = _project(tmp_path / "project")
    before = bootstrap.compatibility_digest(project)
    (project / "RotoWeaveContracts" / "deployment-protocol.json").write_text(
        '{"schemaVersion":2,"platform":"windows-x64","safetyPolicyVersion":2}',
        encoding="utf-8",
    )
    assert bootstrap.compatibility_digest(project) != before


def test_zip64_export_is_atomic_manifested_and_never_overwrites(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = _project(tmp_path / "project")
    output = tmp_path / "handoff"
    bundle, digest = _export_client_bundle(project, output, monkeypatch)
    assert bundle.is_file()
    assert hashlib.sha256(bundle.read_bytes()).hexdigest() == digest
    assert not list(output.glob(".*.partial-*"))
    with zipfile.ZipFile(bundle) as archive:
        assert archive._allowZip64 is True
        manifest = json.loads(archive.read(bootstrap.MANIFEST_NAME))
        assert manifest["schemaVersion"] == 2
        assert manifest["role"] == "client"
        assert manifest["platform"] == "windows-x64"
        assert "client-basic" not in manifest["components"]
        assert manifest["components"]["client-python"]["fileCount"] == 1
        assert ".venv" not in "\n".join(archive.namelist())
        assert "node_modules" not in "\n".join(archive.namelist())
    with pytest.raises(bootstrap.BootstrapError, match="拒绝覆盖"):
        bootstrap.export_bundle(project, "client", output, include_environment=False)


def test_bundle_import_verifies_sha_and_promotes_atomically(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source_project = _project(tmp_path / "source")
    bundle, digest = _export_client_bundle(source_project, tmp_path / "handoff", monkeypatch)
    target_project = _project(tmp_path / "target", with_basic=False)
    result = bootstrap.import_bundle(target_project, "client", bundle, expected_sha256=digest)
    assert result["installed"] == []
    assert not (target_project / "RotoWeaveModels" / "application" / "basic").exists()
    assert not list((target_project / "Temp" / "Bootstrap").glob("bundle-*"))


def test_bundle_hash_mismatch_does_not_create_target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source_project = _project(tmp_path / "source")
    bundle, _ = _export_client_bundle(source_project, tmp_path / "handoff", monkeypatch)
    target_project = _project(tmp_path / "target", with_basic=False)
    with pytest.raises(bootstrap.BootstrapError, match="SHA-256"):
        bootstrap.import_bundle(target_project, "client", bundle, expected_sha256="0" * 64)
    assert not (target_project / "RotoWeaveModels" / "application" / "basic").exists()


def test_import_disk_preflight_stops_before_extraction(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source_project = _project(tmp_path / "source")
    bundle, _ = _export_client_bundle(source_project, tmp_path / "handoff", monkeypatch)
    target_project = _project(tmp_path / "target", with_basic=False)
    monkeypatch.setattr(bootstrap.shutil, "disk_usage", lambda _path: types.SimpleNamespace(free=0))
    with pytest.raises(bootstrap.BootstrapError, match="磁盘空间不足"):
        bootstrap.import_bundle(target_project, "client", bundle)
    assert not (target_project / "RotoWeaveModels" / "application" / "basic").exists()


@pytest.mark.parametrize("member", ["../escape.bin", "/absolute.bin", "C:/drive.bin"])
def test_zip_path_attacks_are_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, member: str) -> None:
    project = _project(tmp_path / "project")
    bundle, _ = _export_client_bundle(project, tmp_path / "handoff", monkeypatch)
    attacked = tmp_path / "attacked.zip"
    shutil.copy2(bundle, attacked)
    with zipfile.ZipFile(attacked, "a") as archive:
        archive.writestr(member, b"attack")
    with pytest.raises(bootstrap.BootstrapError, match="非法|越界"):
        bootstrap.inspect_bundle(project, attacked, expected_role="client", full_hash=True)


def test_backslash_zip_member_is_rejected_before_extraction() -> None:
    with pytest.raises(bootstrap.BootstrapError, match="非法"):
        bootstrap._safe_zip_name("payload\\escape.bin")


def test_case_collision_symlink_and_compression_bomb_are_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = _project(tmp_path / "project")
    bundle, _ = _export_client_bundle(project, tmp_path / "handoff", monkeypatch)

    collision = tmp_path / "collision.zip"
    shutil.copy2(bundle, collision)
    with zipfile.ZipFile(collision, "a") as archive:
        original = next(name for name in archive.namelist() if name.endswith("asset.bin"))
        archive.writestr(original.upper(), b"collision")
    with pytest.raises(bootstrap.BootstrapError, match="大小写冲突"):
        bootstrap.inspect_bundle(project, collision)

    linked = tmp_path / "linked.zip"
    shutil.copy2(bundle, linked)
    with zipfile.ZipFile(linked, "a") as archive:
        info = zipfile.ZipInfo("payload/link")
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(info, "target")
    with pytest.raises(bootstrap.BootstrapError, match="符号链接"):
        bootstrap.inspect_bundle(project, linked)

    bomb = tmp_path / "bomb.zip"
    shutil.copy2(bundle, bomb)
    with zipfile.ZipFile(bomb, "a", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("payload/bomb.bin", b"0" * (11 * 1024 * 1024))
    with pytest.raises(bootstrap.BootstrapError, match="压缩比异常"):
        bootstrap.inspect_bundle(project, bomb)


def test_tampered_tree_wrong_role_and_undeclared_member_are_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = _project(tmp_path / "project")
    bundle, _ = _export_client_bundle(project, tmp_path / "handoff", monkeypatch)
    with pytest.raises(bootstrap.BootstrapError, match="角色不匹配"):
        bootstrap.inspect_bundle(project, bundle, expected_role="server")

    tampered = tmp_path / "tampered.zip"
    _rewrite_zip(
        bundle,
        tampered,
        lambda info, data: (info, b"changed" if info.filename.endswith("asset.bin") else data),
    )
    with pytest.raises(bootstrap.BootstrapError, match="字节数|树 SHA-256"):
        bootstrap.inspect_bundle(project, tampered, expected_role="client", full_hash=True)

    undeclared = tmp_path / "undeclared.zip"
    shutil.copy2(bundle, undeclared)
    with zipfile.ZipFile(undeclared, "a") as archive:
        archive.writestr("extra.txt", b"x")
    with pytest.raises(bootstrap.BootstrapError, match="未声明成员"):
        bootstrap.inspect_bundle(project, undeclared)


def test_directory_selection_requires_one_compatible_bundle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = _project(tmp_path / "project")
    bundle, _ = _export_client_bundle(project, tmp_path / "handoff", monkeypatch)
    directory = tmp_path / "bundles"
    directory.mkdir()
    shutil.copy2(bundle, directory / "one.zip")
    assert bootstrap.select_bundle(project, "client", directory).name == "one.zip"
    shutil.copy2(bundle, directory / "two.zip")
    with pytest.raises(bootstrap.BootstrapError, match="多个兼容"):
        bootstrap.select_bundle(project, "client", directory)


def test_local_download_resumes_partial_and_verifies_sha(tmp_path: Path) -> None:
    source = tmp_path / "source.zip"
    source.write_bytes(b"0123456789" * 1000)
    expected = hashlib.sha256(source.read_bytes()).hexdigest()
    output = tmp_path / "output.zip"
    output.with_suffix(".zip.partial").write_bytes(source.read_bytes()[:321])
    output.with_suffix(".zip.partial.json").write_text(
        json.dumps({"schemaVersion": 1, "source": str(source), "expectedSha256": expected}),
        encoding="utf-8",
    )
    assert bootstrap.download_bundle(str(source), output, expected) == output
    assert output.read_bytes() == source.read_bytes()
    assert not output.with_suffix(".zip.partial").exists()


def test_download_bundle_discards_partial_when_expected_sha_changes(tmp_path: Path) -> None:
    source = tmp_path / "source.zip"
    source.write_bytes(b"new controlled bundle")
    expected = hashlib.sha256(source.read_bytes()).hexdigest()
    output = tmp_path / "output.zip"
    partial = output.with_suffix(".zip.partial")
    metadata = output.with_suffix(".zip.partial.json")
    partial.write_bytes(b"old bytes")
    metadata.write_text(
        json.dumps({"schemaVersion": 1, "source": str(source), "expectedSha256": "0" * 64}),
        encoding="utf-8",
    )

    assert bootstrap.download_bundle(str(source), output, expected) == output
    assert output.read_bytes() == source.read_bytes()


def test_https_resume_rejects_wrong_content_range_without_appending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "output.zip"
    partial = output.with_suffix(".zip.partial")
    metadata = output.with_suffix(".zip.partial.json")
    partial.write_bytes(b"safe")
    metadata.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "source": "https://example/bundle.zip",
                "expectedSha256": "0" * 64,
                "expectedBytes": 10,
                "etag": '"v1"',
            }
        ),
        encoding="utf-8",
    )
    response = _ScriptedResponse(
        206,
        {"Content-Length": "6", "Content-Range": "bytes 3-8/10", "ETag": '"v1"'},
        [b"attack"],
    )
    monkeypatch.setattr(bootstrap.urllib.request, "urlopen", lambda *_args, **_kwargs: response)

    with pytest.raises(bootstrap.BootstrapError, match="expected-start=4"):
        bootstrap.download_bundle("https://example/bundle.zip", output, "0" * 64)
    assert partial.read_bytes() == b"safe"


def test_https_resume_rejects_changed_etag_and_keeps_network_partial(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    output = tmp_path / "output.zip"
    partial = output.with_suffix(".zip.partial")
    metadata = output.with_suffix(".zip.partial.json")
    partial.write_bytes(b"old")
    metadata.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "source": "https://example/bundle.zip",
                "expectedSha256": "0" * 64,
                "expectedBytes": 7,
                "etag": '"old"',
            }
        ),
        encoding="utf-8",
    )

    class Response(io.BytesIO):
        status = 206
        headers = {"Content-Length": "3", "Content-Range": "bytes 3-5/7", "ETag": '"new"'}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()

    monkeypatch.setattr(bootstrap.urllib.request, "urlopen", lambda *_args, **_kwargs: Response(b"new"))
    with pytest.raises(bootstrap.BootstrapError, match="ETag"):
        bootstrap.download_bundle("https://example/bundle.zip", output, "0" * 64)
    assert not partial.exists() and not metadata.exists()

    partial.write_bytes(b"resume")
    metadata.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "source": "https://example/bundle.zip",
                "expectedSha256": "0" * 64,
                "expectedBytes": 7,
                "etag": '"stable"',
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        bootstrap.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(urllib.error.URLError("offline")),
    )
    with pytest.raises(bootstrap.BootstrapError, match="partial"):
        bootstrap.download_bundle("https://example/bundle.zip", output, "0" * 64)
    assert partial.read_bytes() == b"resume"


def test_server_setup_sources_exclude_models_and_match_runtime_recipe() -> None:
    catalog = bootstrap.load_source_catalog(WORKSPACE)
    assert not [item for item in catalog["componentSources"] if item.get("kind") == "model"]

    contract_path = WORKSPACE / "RotoWeaveContracts" / "server-runtime-sources.json"
    contract = bootstrap.load_server_runtime_source_contract(WORKSPACE)
    contract_sha = hashlib.sha256(contract_path.read_bytes()).hexdigest()
    assert contract["projectSearchPaths"] == ["..\\..\\..", "..\\..\\..\\..\\RotoWeaveContracts"]
    _, runtime_recipe = bootstrap._load_contracts(WORKSPACE)
    revisions = {item["id"]: item["revision"] for item in contract["sources"]}
    for profile in ("high", "ultra"):
        recipe = runtime_recipe(profile)
        requirements = WORKSPACE / contract["profiles"][profile]["requirements"]
        assert recipe["runtimeSourceContractSha256"] == contract_sha
        assert recipe["requirementsSha256"] == hashlib.sha256(requirements.read_bytes()).hexdigest()
        assert all(revisions[source_id] in recipe["sourceRevisions"].values() for source_id in contract["profiles"][profile]["sources"])


def test_dynamic_server_runtime_build_refuses_unapproved_invalid_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "RotoWeaveServer" / "server-runtimes"
    target.mkdir(parents=True)
    sentinel = target / "preserve.bin"
    sentinel.write_bytes(b"preserve")
    monkeypatch.setattr(
        bootstrap,
        "check_server_runtimes",
        lambda _root: bootstrap.CheckResult("server-runtimes", "invalid", "contract changed"),
    )

    with pytest.raises(bootstrap.BootstrapError, match="-Repair"):
        bootstrap.build_server_runtimes_from_source(tmp_path, cache_root=tmp_path / "cache")

    assert sentinel.read_bytes() == b"preserve"


def test_ultra_overlay_install_never_uninstalls_high_packages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(
        bootstrap,
        "_run_runtime_checked",
        lambda command, *_args, **_kwargs: calls.append(command),
    )

    bootstrap._install_runtime_requirements(tmp_path / "python.exe", tmp_path / "lock.txt", "high")
    bootstrap._install_runtime_requirements(tmp_path / "python.exe", tmp_path / "lock.txt", "ultra")

    assert "--ignore-installed" not in calls[0]
    assert "--ignore-installed" in calls[2]
    assert "--no-deps" in calls[2]


def test_runtime_weight_gate_allows_only_small_text_site_path_files(tmp_path: Path) -> None:
    site = tmp_path / "high" / "runtime" / "Lib" / "site-packages"
    site.mkdir(parents=True)
    path_configuration = site / "distutils-precedence.pth"
    path_configuration.write_text("import _distutils_hack\n", encoding="utf-8")
    assert bootstrap._is_forbidden_runtime_weight(path_configuration, tmp_path) is False

    binary_weight = site / "weights.pth"
    binary_weight.write_bytes(b"\0" * 128)
    assert bootstrap._is_forbidden_runtime_weight(binary_weight, tmp_path) is True
    source_weight = tmp_path / "high" / "sources" / "weights.pth"
    source_weight.parent.mkdir(parents=True)
    source_weight.write_text("not a path configuration", encoding="utf-8")
    assert bootstrap._is_forbidden_runtime_weight(source_weight, tmp_path) is True


def test_fresh_client_checkout_reports_missing_external_assets_and_environment(tmp_path: Path) -> None:
    _project(tmp_path, with_basic=False)
    assert bootstrap.check_basic(tmp_path).status == "missing"
    assert bootstrap.check_environments(tmp_path, "client")[0].status == "missing"


def test_dynamic_basic_manifest_uses_contract_and_local_artifact_hashes(tmp_path: Path) -> None:
    project = _project(tmp_path)
    assert bootstrap.check_basic(project, full_hash=True).status == "ready"
    manifest_path = (
        project
        / "RotoWeaveModels"
        / "application"
        / "basic"
        / "birefnet-lite-matting.manifest.json"
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["onnxSha256"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    result = bootstrap.check_basic(project, full_hash=True)
    assert result.status == "invalid"
    assert "SHA-256" in result.detail


def test_dynamic_basic_rejects_non_finite_error_and_tampered_license(tmp_path: Path) -> None:
    project = _project(tmp_path)
    basic = project / "RotoWeaveModels" / "application" / "basic"
    manifest_path = basic / "birefnet-lite-matting.manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    requirements = project / "RotoWeaveClient" / "requirements-basic-export-lock.txt"
    requirements_bytes = requirements.read_bytes()
    requirements.write_bytes(requirements_bytes + b"# changed\n")
    assert bootstrap.check_basic(project, full_hash=True).status == "invalid"
    requirements.write_bytes(requirements_bytes)

    manifest["pytorchOnnxCpuMaxAbs"] = float("nan")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    assert bootstrap.check_basic(project, full_hash=True).status == "invalid"

    manifest["pytorchOnnxCpuMaxAbs"] = 0.0
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    (basic / manifest["licenseFile"]).write_text("tampered", encoding="utf-8")
    result = bootstrap.check_basic(project, full_hash=True)
    assert result.status == "invalid"
    assert "SHA-256" in result.detail


def test_dynamic_basic_build_promotes_only_after_full_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path / "project", with_basic=False)
    fake_python = tmp_path / "export-python.exe"
    fake_python.write_bytes(b"python")
    monkeypatch.setattr(
        bootstrap,
        "_prepare_basic_export_environment",
        lambda *_args, **_kwargs: (fake_python, "a" * 64),
    )

    def fake_run(command: list[str], _label: str) -> None:
        output = Path(command[command.index("--output") + 1])
        output.parent.mkdir(parents=True, exist_ok=True)
        contract_path = Path(command[command.index("--contract") + 1])
        contract_bytes = contract_path.read_bytes()
        contract = json.loads(contract_bytes)
        output.write_bytes(b"generated-onnx")
        self_test = output.with_name(contract["selfTest"]["file"])
        self_test.write_bytes(b"generated-self-test")
        license_path = Path(command[command.index("--license") + 1])
        output.with_name(contract["licenseFile"]).write_bytes(license_path.read_bytes())
        manifest = dict(contract)
        manifest.pop("validation")
        manifest["contractSha256"] = hashlib.sha256(contract_bytes).hexdigest()
        manifest["onnxSha256"] = hashlib.sha256(output.read_bytes()).hexdigest()
        manifest["pytorchOnnxCpuMaxAbs"] = 0.0
        requirements_path = Path(command[command.index("--requirements") + 1])
        manifest["requirementsSha256"] = hashlib.sha256(requirements_path.read_bytes()).hexdigest()
        manifest["selfTest"] = {
            **contract["selfTest"],
            "sha256": hashlib.sha256(self_test.read_bytes()).hexdigest(),
        }
        output.with_suffix(".manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    monkeypatch.setattr(bootstrap, "_run_basic_checked", fake_run)
    def fake_download(_source, target: Path, **_kwargs) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"test-only-license")

    monkeypatch.setattr(bootstrap, "_download_verified_file", fake_download)
    result = bootstrap.build_basic_from_source(project, cache_root=tmp_path / "cache")
    assert result["built"] is True
    assert bootstrap.check_basic(project, full_hash=True).status == "ready"
    assert not list((project / "Temp" / "Bootstrap").glob("basic-build-*"))


def test_dynamic_basic_runner_keeps_machine_stdout_clean(capfd: pytest.CaptureFixture[str]) -> None:
    bootstrap._run_basic_checked(
        [
            sys.executable,
            "-c",
            "import sys; print('Collecting.'); print('export warning', file=sys.stderr)",
        ],
        "Basic output routing probe",
    )

    captured = capfd.readouterr()
    assert captured.out == ""
    assert "Collecting." in captured.err
    assert "export warning" in captured.err


def test_dynamic_basic_export_download_is_retryable_and_uses_its_own_cache() -> None:
    exporter = (
        WORKSPACE / "RotoWeaveClient" / "scripts" / "export-birefnet-onnx.py"
    ).read_text(encoding="utf-8")

    assert exporter.index('ensure_minimum_timeout("HF_HUB_DOWNLOAD_TIMEOUT", 120)') < exporter.index(
        "import numpy as np"
    )
    assert "SOURCE_DOWNLOAD_ATTEMPTS = 6" in exporter
    assert "cache_dir=str(hub_cache_dir)" in exporter
    assert "resuming in {delay}s" in exporter
    assert 'root = files["config.json"].parent' in exporter
    assert '"local_files_only": True' in exporter
    assert "from huggingface_hub import hf_hub_download\nfrom torchvision" not in exporter


def test_dynamic_basic_build_refuses_to_overwrite_invalid_existing_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path / "project", with_basic=False)
    target = project / "RotoWeaveModels" / "application" / "basic"
    target.mkdir(parents=True)
    sentinel = target / "user-content.bin"
    sentinel.write_bytes(b"preserve-me")

    monkeypatch.setattr(
        bootstrap,
        "_prepare_basic_export_environment",
        lambda *_args, **_kwargs: pytest.fail("export environment must not be prepared"),
    )
    with pytest.raises(bootstrap.BootstrapError, match="-Repair"):
        bootstrap.build_basic_from_source(project, cache_root=tmp_path / "cache")

    assert sentinel.read_bytes() == b"preserve-me"


def test_root_commands_and_new_machine_document_form_one_public_flow() -> None:
    expected = {
        "Setup-RotoWeave.cmd": "scripts\\Setup-RotoWeave.ps1",
        "Check-RotoWeave.cmd": "scripts\\Check-RotoWeave.ps1",
        "Start-RotoWeave.cmd": "scripts\\Start-RotoWeave.ps1",
        "Stop-RotoWeave.cmd": "scripts\\Stop-RotoWeave.ps1",
        "Export-RotoWeaveArtifacts.cmd": "scripts\\Export-RotoWeaveArtifacts.ps1",
    }
    for filename, route in expected.items():
        text = (WORKSPACE / filename).read_text(encoding="utf-8")
        assert route in text and "ExecutionPolicy Bypass" in text
    setup = (WORKSPACE / "scripts" / "Setup-RotoWeave.ps1").read_text(encoding="utf-8")
    guide = (WORKSPACE / "README.md").read_text(encoding="utf-8")
    assert "build-basic" in setup and "build-server-runtimes" in setup
    assert "BundleDirectory" in setup and "AcceptDownload" in setup
    assert setup.count("--full-hash") >= 4
    assert "RotoWeaveClient\\Setup.ps1" in setup and "RotoWeaveServer\\Setup.ps1" in setup
    assert "Setup-RotoWeave.cmd Client" in guide and "GitHub Release 均不包含任何模型权重" in guide
