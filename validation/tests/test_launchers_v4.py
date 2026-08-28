from __future__ import annotations

import base64
import json
import importlib.util
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
import zipfile

import pytest

from backend import client_launcher
from server import launcher as server_launcher
from server.config import RemoteServerSettings


WORKSPACE = Path(__file__).resolve().parents[2]


def _powershell_hosts() -> list[str]:
    return [path for name in ("powershell.exe", "pwsh.exe") if (path := shutil.which(name))]


def _server_host_check(
    tmp_path: Path,
    powershell: str,
    *,
    gpu_name: str = "NVIDIA GeForce RTX 4090",
    query_exit: int = 0,
    cuda_version: str = "13.1",
    smi_exit: int = 0,
) -> dict[str, object]:
    fake_smi = tmp_path / "fake-nvidia-smi.cmd"
    fake_smi.write_text(
        "@echo off\r\n"
        'if not "%~1"=="" (\r\n'
        "  echo 0,GPU-test,%FAKE_GPU_NAME%,999.0,8.9,24564,1024\r\n"
        "  exit /b %FAKE_GPU_QUERY_EXIT%\r\n"
        ")\r\n"
        "echo NVIDIA-SMI TEST Driver Version: 999.0 CUDA Version: %FAKE_CUDA_VERSION%\r\n"
        "exit /b %FAKE_SMI_EXIT%\r\n",
        encoding="ascii",
    )
    command = r"""
$tokens = $null
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($env:ROTOWEAVE_SETUP_SCRIPT, [ref]$tokens, [ref]$parseErrors)
if ($parseErrors.Count -ne 0) { throw ($parseErrors | Out-String) }
$functionAst = $ast.Find({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Get-RotoWeaveServerHostStatus' }, $true)
if (-not $functionAst) { throw 'Get-RotoWeaveServerHostStatus was not found.' }
Invoke-Expression $functionAst.Extent.Text
Get-RotoWeaveServerHostStatus -NvidiaSmiPath $env:ROTOWEAVE_FAKE_NVIDIA_SMI | ConvertTo-Json -Compress
"""
    environment = os.environ.copy()
    environment.update(
        {
            "ROTOWEAVE_SETUP_SCRIPT": str(WORKSPACE / "scripts" / "Setup-RotoWeave.ps1"),
            "ROTOWEAVE_FAKE_NVIDIA_SMI": str(fake_smi),
            "FAKE_GPU_NAME": gpu_name,
            "FAKE_GPU_QUERY_EXIT": str(query_exit),
            "FAKE_CUDA_VERSION": cuda_version,
            "FAKE_SMI_EXIT": str(smi_exit),
        }
    )
    completed = subprocess.run(
        [
            powershell,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-EncodedCommand",
            base64.b64encode(command.encode("utf-16-le")).decode("ascii"),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=environment,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    json_lines = [line for line in completed.stdout.splitlines() if line.strip().startswith("{")]
    assert json_lines, completed.stdout
    return json.loads(json_lines[-1])


def _run_setup_function_probe(
    powershell: str,
    function_names: tuple[str, ...],
    invocation: str,
    environment: dict[str, str],
    script_path: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    names = ",".join(f"'{name}'" for name in function_names)
    command = f"""
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$tokens = $null
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($env:ROTOWEAVE_PROBE_SCRIPT, [ref]$tokens, [ref]$parseErrors)
if ($parseErrors.Count -ne 0) {{ throw ($parseErrors | Out-String) }}
foreach ($name in @({names})) {{
    $functionAst = $ast.Find({{ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $name }}, $true)
    if (-not $functionAst) {{ throw "Setup function was not found: $name" }}
    Invoke-Expression $functionAst.Extent.Text
}}
{invocation}
"""
    probe_environment = os.environ.copy()
    probe_environment.update(environment)
    probe_environment["ROTOWEAVE_PROBE_SCRIPT"] = str(
        script_path or WORKSPACE / "scripts" / "Setup-RotoWeave.ps1"
    )
    return subprocess.run(
        [
            powershell,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-EncodedCommand",
            base64.b64encode(command.encode("utf-16-le")).decode("ascii"),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=probe_environment,
        timeout=30,
        check=False,
    )


@pytest.mark.parametrize("powershell", _powershell_hosts())
def test_setup_resolves_absolute_bundle_directory_without_recombining_it(
    tmp_path: Path, powershell: str
) -> None:
    completed = _run_setup_function_probe(
        powershell,
        ("Resolve-RotoWeaveBundleFromUserInput",),
        "Resolve-RotoWeaveBundleFromUserInput $env:ROTOWEAVE_BUNDLE_INPUT",
        {"ROTOWEAVE_BUNDLE_INPUT": str(tmp_path)},
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    assert Path(completed.stdout.strip()).resolve() == tmp_path.resolve()


@pytest.mark.parametrize("powershell", _powershell_hosts())
def test_setup_can_preselect_role_bundle_before_python_is_available(
    tmp_path: Path, powershell: str
) -> None:
    for role in ("client", "server"):
        manifest = {
            "schemaVersion": 2,
            "productVersion": "4.0.0",
            "platform": "windows-x64",
            "role": role,
        }
        with zipfile.ZipFile(tmp_path / f"{role}.zip", "w") as archive:
            archive.writestr("RotoWeave-DEPLOYMENT.json", json.dumps(manifest))

    completed = _run_setup_function_probe(
        powershell,
        ("Read-RotoWeaveBootstrapBundleManifest", "Find-RotoWeaveHostBootstrapBundle"),
        "Find-RotoWeaveHostBootstrapBundle -ResolvedInput $env:ROTOWEAVE_BUNDLE_INPUT -ExpectedRole client -ProjectRoot $env:ROTOWEAVE_PROJECT_ROOT",
        {
            "ROTOWEAVE_BUNDLE_INPUT": str(tmp_path),
            "ROTOWEAVE_PROJECT_ROOT": str(WORKSPACE),
        },
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    assert Path(completed.stdout.strip()).resolve() == (tmp_path / "client.zip").resolve()


@pytest.mark.parametrize("powershell", _powershell_hosts())
def test_python_probe_suppresses_missing_runtime_launcher_noise(
    tmp_path: Path, powershell: str
) -> None:
    fake_launcher = tmp_path / "fake-py.cmd"
    fake_launcher.write_text(
        "@echo off\r\n"
        "echo No suitable Python runtime found 1>&2\r\n"
        "exit /b 103\r\n",
        encoding="ascii",
    )
    completed = _run_setup_function_probe(
        powershell,
        ("Test-RotoWeavePython312Available",),
        "[PSCustomObject]@{available=(Test-RotoWeavePython312Available $env:ROTOWEAVE_FAKE_PY)} | ConvertTo-Json -Compress",
        {"ROTOWEAVE_FAKE_PY": str(fake_launcher)},
        WORKSPACE / "scripts" / "Bootstrap-Common.ps1",
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    assert json.loads(completed.stdout.strip()) == {"available": False}
    assert "No suitable Python runtime found" not in completed.stdout + completed.stderr


def test_root_bootstrap_invocations_use_resolved_python_executable() -> None:
    common = (WORKSPACE / "scripts" / "Bootstrap-Common.ps1").read_text(encoding="utf-8")
    assert "Find-RotoWeavePython312Executable" in common
    assert 'Programs\\Python\\Python312\\python.exe' in common
    for relative in (
        "scripts/Setup-RotoWeave.ps1",
        "scripts/Check-RotoWeave.ps1",
        "scripts/Start-RotoWeave.ps1",
        "scripts/Export-RotoWeaveArtifacts.ps1",
    ):
        assert '"-3.12"' not in (WORKSPACE / relative).read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def isolate_launcher_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "ROTOWEAVE_REMOTE_BEARER_TOKEN",
        "ROTOWEAVE_REMOTE_HOST",
        "ROTOWEAVE_REMOTE_PORT",
        "ROTOWEAVE_REMOTE_ADMIN_PORT",
        "ROTOWEAVE_REMOTE_TTL_HOURS",
        "ROTOWEAVE_REMOTE_TLS_CERT",
        "ROTOWEAVE_REMOTE_TLS_KEY",
        "ROTOWEAVE_REMOTE_MATTING_URL",
        "ROTOWEAVE_REMOTE_MATTING_TOKEN",
        "ROTOWEAVE_REMOTE_MATTING_CA",
        "ROTOWEAVE_CLIENT_LAUNCHER_CONFIG",
    ):
        monkeypatch.delenv(name, raising=False)


def test_client_launcher_creates_offline_v4_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "client.json"
    monkeypatch.setenv("ROTOWEAVE_REMOTE_MATTING_TOKEN", "must-be-cleared")

    assert client_launcher.apply_config(target) == target.resolve()
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["productVersion"] == "4.0.0"
    assert payload["schemaVersion"] == 2
    assert payload["remoteMatting"]["enabled"] is False
    assert "ROTOWEAVE_REMOTE_MATTING_TOKEN" not in __import__("os").environ


@pytest.mark.parametrize("powershell", _powershell_hosts())
def test_server_setup_accepts_compatible_driver_in_both_powershell_hosts(tmp_path: Path, powershell: str) -> None:
    result = _server_host_check(tmp_path, powershell)
    assert result["installable"] is True
    assert result["ready"] is True
    assert result["detail"] == "NVIDIA GeForce RTX 4090 / CUDA Version: 13.1"


@pytest.mark.parametrize("powershell", _powershell_hosts())
@pytest.mark.parametrize(
    "gpu_name",
    [
        "NVIDIA GeForce RTX 3060",
        "NVIDIA GeForce RTX 3070",
        "NVIDIA GeForce RTX 3090",
        "NVIDIA GeForce RTX 4060",
        "NVIDIA GeForce RTX 5090",
        "NVIDIA Future CUDA Device X",
    ],
)
def test_server_setup_has_no_gpu_sku_allowlist(
    tmp_path: Path, powershell: str, gpu_name: str
) -> None:
    result = _server_host_check(tmp_path, powershell, gpu_name=gpu_name)
    assert result["installable"] is True
    assert result["profileCandidate"] is True
    assert result["selectedDevice"]["name"] == gpu_name


@pytest.mark.parametrize("powershell", _powershell_hosts())
def test_server_setup_rejects_failed_gpu_query_without_false_device_detail(tmp_path: Path, powershell: str) -> None:
    result = _server_host_check(tmp_path, powershell, query_exit=7)
    assert result["installable"] is True
    assert result["ready"] is False
    assert result["warnings"][0]["code"] == "nvidia_smi_failed"


@pytest.mark.parametrize("powershell", _powershell_hosts())
def test_server_setup_rejects_driver_below_cuda_compatibility_floor(tmp_path: Path, powershell: str) -> None:
    result = _server_host_check(tmp_path, powershell, cuda_version="12.7")
    assert result["installable"] is True
    assert result["ready"] is False
    assert result["warnings"][0]["code"] == "driver_incompatible"


def test_server_model_setup_entrypoint_imports_server_from_arbitrary_working_directory(tmp_path: Path) -> None:
    script = WORKSPACE / "RotoWeaveServer" / "scripts" / "setup-model-center.py"
    server_python = WORKSPACE / "RotoWeaveServer" / ".venv" / "Scripts" / "python.exe"
    if not server_python.is_file():
        pytest.skip("RotoWeaveServer Setup environment is not present in a clean public checkout.")
    completed = subprocess.run(
        [
            str(server_python),
            "-I",
            "-c",
            "import runpy,sys; runpy.run_path(sys.argv[1], run_name='rotoweave_setup_import_test')",
            str(script),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout


def test_server_model_setup_uses_pathless_default_selection() -> None:
    script = WORKSPACE / "RotoWeaveServer" / "scripts" / "setup-model-center.py"
    spec = importlib.util.spec_from_file_location("setup_model_center_test", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    operation = {
        "id": "model-op-default-selection",
        "kind": "select_default",
        "state": "passed",
        "stage": "passed",
        "progress": 1.0,
    }
    calls: list[str] = []

    def select_default() -> dict[str, object]:
        calls.append("select-default")
        return operation

    center = SimpleNamespace(
        select_default=select_default,
        operation=lambda operation_id: operation,
    )
    service = SimpleNamespace(model_center=center)

    module.run_step(service, "select-default-models", center.select_default)

    assert calls == ["select-default"]


def test_service_activation_validation_uses_pathless_default_selection_api() -> None:
    script = (
        WORKSPACE
        / "validation"
        / "scripts"
        / "activate-independent-models-on-service.py"
    ).read_text(encoding="utf-8")

    assert "/api/admin/v2/model-selections/default" in script
    assert "/api/admin/v2/model-scans" not in script
    assert 'snapshot.get("roots")' not in script


def test_server_model_setup_keeps_installation_usable_when_all_profiles_are_unavailable() -> None:
    script = WORKSPACE / "RotoWeaveServer" / "scripts" / "setup-model-center.py"
    spec = importlib.util.spec_from_file_location("setup_model_center_no_profiles_test", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    operation = {
        "id": "model-op-self-test",
        "kind": "self_test",
        "state": "failed",
        "stage": "failed",
        "progress": 1.0,
        "error": module.NO_READY_PROFILE_ERROR,
    }
    center = SimpleNamespace(
        self_test=lambda: operation,
        operation=lambda operation_id: operation,
        snapshot=lambda: {
            "profiles": {
                "high": {"state": "blocked", "error": "out of memory"},
                "ultra": {"state": "blocked", "error": "out of memory"},
            }
        },
    )

    tested, ready_profiles = module.run_profile_self_test(SimpleNamespace(model_center=center))

    assert ready_profiles == []
    assert tested["profiles"]["high"]["state"] == "blocked"


def test_server_model_setup_does_not_hide_unexpected_self_test_failure() -> None:
    script = WORKSPACE / "RotoWeaveServer" / "scripts" / "setup-model-center.py"
    spec = importlib.util.spec_from_file_location("setup_model_center_failure_test", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    operation = {
        "id": "model-op-self-test",
        "kind": "self_test",
        "state": "failed",
        "stage": "failed",
        "progress": 1.0,
        "error": "self-test worker crashed",
    }
    center = SimpleNamespace(
        self_test=lambda: operation,
        operation=lambda operation_id: operation,
        snapshot=lambda: {
            "profiles": {
                "high": {"state": "blocked"},
                "ultra": {"state": "blocked"},
            }
        },
    )

    with pytest.raises(RuntimeError, match="self-test worker crashed"):
        module.run_profile_self_test(SimpleNamespace(model_center=center))


def test_client_launcher_rejects_noncurrent_schema_without_rewriting_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = tmp_path / "token.txt"
    ca = tmp_path / "ca.cer"
    token.write_text("a" * 32, encoding="utf-8")
    ca.write_bytes(b"certificate")
    config = tmp_path / "client.json"
    config.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "productVersion": "4.0.0",
                "remoteMatting": {
                    "enabled": True,
                    "endpoint": "https://192.168.1.40:8443/",
                    "bearerTokenFile": str(token),
                    "caCertificate": str(ca),
                },
            }
        ),
        encoding="utf-8",
    )

    before = config.read_bytes()
    with pytest.raises(ValueError, match="只支持 RotoWeave 4.0.0"):
        client_launcher.apply_config(config)
    assert config.read_bytes() == before
    assert token.read_text(encoding="utf-8") == "a" * 32
    assert ca.read_bytes() == b"certificate"
    assert "ROTOWEAVE_REMOTE_MATTING_URL" not in os.environ


def test_client_remote_settings_are_saved_without_security_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "client-state" / "client-launcher.json"
    monkeypatch.setenv("ROTOWEAVE_CLIENT_LAUNCHER_CONFIG", str(target))

    public = client_launcher.save_remote_settings(
        enabled=True,
        host="192.168.1.40",
        port=9443,
    )

    payload = json.loads(target.read_text(encoding="utf-8"))
    remote = payload["remoteMatting"]
    assert public == {
        "enabled": True,
        "endpoint": "http://192.168.1.40:9443",
        "host": "192.168.1.40",
        "port": 9443,
    }
    assert remote == {"enabled": True, "endpoint": public["endpoint"]}
    assert payload["schemaVersion"] == 2
    assert not (target.parent / "secrets" / "remote-matting").exists()
    assert os.environ["ROTOWEAVE_REMOTE_MATTING_URL"] == public["endpoint"]
    assert "ROTOWEAVE_REMOTE_MATTING_TOKEN" not in os.environ

    disabled = client_launcher.save_remote_settings(
        enabled=False,
        host="192.168.1.40",
        port=9443,
    )
    assert disabled["enabled"] is False
    assert "ROTOWEAVE_REMOTE_MATTING_URL" not in os.environ
    assert "ROTOWEAVE_REMOTE_MATTING_TOKEN" not in os.environ


def test_client_remote_settings_reject_invalid_inputs_without_replacing_working_config(
    tmp_path: Path,
) -> None:
    target = tmp_path / "client-launcher.json"
    client_launcher.apply_config(target)
    original = target.read_bytes()

    with pytest.raises(ValueError, match="固定局域网 IPv4"):
        client_launcher.save_remote_settings(
            enabled=True,
            host="public.example.com",
            port=8443,
            path=target,
        )
    assert target.read_bytes() == original

    with pytest.raises(ValueError, match="可信局域网 IPv4"):
        client_launcher.save_remote_settings(
            enabled=True,
            host="8.8.8.8",
            port=8443,
            path=target,
        )
    assert target.read_bytes() == original


def test_server_launcher_generates_trusted_lan_http_config_without_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ROTOWEAVE_REMOTE_DATA_ROOT", str(tmp_path / "server-data"))
    monkeypatch.setattr(server_launcher, "discover_lan_ipv4", lambda: "192.168.31.44")
    target = tmp_path / "server.json"

    actual, connection = server_launcher.apply_config(target)

    assert actual == target.resolve()
    assert connection["endpoint"] == "http://192.168.31.44:8443"
    assert "tokenFile" not in connection
    assert "caCertificate" not in connection
    persisted = json.loads(target.read_text(encoding="utf-8"))
    assert persisted["schemaVersion"] == 3
    assert persisted["apiHost"] == "192.168.31.44"
    assert not (tmp_path / "server-data" / "secrets").exists()
    settings = RemoteServerSettings()
    assert settings.data_root == (tmp_path / "server-data").resolve()
    assert settings.admin_host == "127.0.0.1"


def test_server_launcher_rejects_noncurrent_schema_without_rewriting_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "server-data"
    monkeypatch.setenv("ROTOWEAVE_REMOTE_DATA_ROOT", str(root))
    monkeypatch.setattr(server_launcher, "discover_lan_ipv4", lambda: "192.168.31.44")
    target = tmp_path / "server.json"
    payload = server_launcher.default_config()
    payload.update({"schemaVersion": 2, "apiHost": "192.168.1.40"})
    target.write_text(json.dumps(payload), encoding="utf-8")

    before = target.read_bytes()
    with pytest.raises(ValueError, match="只支持 RotoWeave 4.0.0"):
        server_launcher.apply_config(target)
    assert target.read_bytes() == before


@pytest.mark.parametrize("host", ["0.0.0.0", "8.8.8.8", "169.254.1.10", "server.local"])
def test_server_launcher_ignores_configured_api_host(
    tmp_path: Path, host: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(server_launcher, "discover_lan_ipv4", lambda: "192.168.31.44")
    target = tmp_path / "server.json"
    payload = server_launcher.default_config()
    payload["apiHost"] = host
    target.write_text(json.dumps(payload), encoding="utf-8")

    _, connection = server_launcher.apply_config(target)
    assert connection["endpoint"] == "http://192.168.31.44:8443"
    assert json.loads(target.read_text(encoding="utf-8"))["apiHost"] == "192.168.31.44"


def test_server_launcher_falls_back_to_loopback_when_lan_address_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unavailable() -> str:
        raise ValueError("未检测到局域网地址")

    monkeypatch.setattr(server_launcher, "discover_lan_ipv4", unavailable)
    _, connection = server_launcher.apply_config(tmp_path / "server.json")
    assert connection["endpoint"] == "http://127.0.0.1:8443"
    assert connection["networkWarning"] == "未检测到局域网地址"


def test_server_launcher_rejects_cross_version_config(tmp_path: Path) -> None:
    target = tmp_path / "server.json"
    payload = server_launcher.default_config()
    payload["productVersion"] = "3.0.0"
    target.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="只支持 RotoWeave 4.0.0"):
        server_launcher.apply_config(target)


def test_server_launcher_rejects_removed_configuration_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(server_launcher, "discover_lan_ipv4", lambda: "192.168.31.44")
    target = tmp_path / "server.json"
    payload = server_launcher.default_config()
    payload.update({"modelPackPath": "old-pack", "modelPackPublicKey": "old-key", "modelArchiveMaxGiB": 32})
    target.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="包含非当前字段"):
        server_launcher.apply_config(target)


def test_client_and_server_specs_are_independent_v4_launchers() -> None:
    client = (WORKSPACE / "RotoWeaveClient" / "RotoWeaveClient.spec").read_text(encoding="utf-8")
    server = (WORKSPACE / "RotoWeaveServer" / "RotoWeaveServer.spec").read_text(encoding="utf-8")
    client_start = (WORKSPACE / "RotoWeaveClient" / "Start.cmd").read_text(encoding="utf-8")
    server_start = (WORKSPACE / "RotoWeaveServer" / "Start.cmd").read_text(encoding="utf-8")
    client_stop = (WORKSPACE / "RotoWeaveClient" / "Stop.cmd").read_text(encoding="utf-8")
    server_stop = (WORKSPACE / "RotoWeaveServer" / "Stop.cmd").read_text(encoding="utf-8")

    assert 'name="RotoWeave-Client"' in client
    assert 'backend" / "client_launcher.py"' in client
    assert 'name="RotoWeave-Server"' in server
    assert 'server" / "launcher.py"' in server
    assert 'collect_submodules("pystray")' in server
    assert 'excludes=["tkinter", "matplotlib", "notebook"]' in server
    assert "console=False" in server
    assert 'release / "frontend"' not in server
    assert 'release / "models"' not in server
    assert "Start.ps1" in client_start
    assert "Start.ps1" in server_start
    assert "stop-rotoweave-client.ps1" in client_stop
    assert "stop-rotoweave-server.ps1" in server_stop
    for retired in (
        WORKSPACE / "RotoWeaveClient" / "Start-RotoWeave-Client.cmd",
        WORKSPACE / "RotoWeaveClient" / "Stop-RotoWeave-Client.cmd",
        WORKSPACE / "RotoWeaveServer" / "Start-RotoWeave-LAN.cmd",
        WORKSPACE / "RotoWeaveServer" / "Start-RotoWeave-Server.cmd",
        WORKSPACE / "RotoWeaveServer" / "Stop-RotoWeave-Server.cmd",
    ):
        assert not retired.exists()


def test_source_start_scripts_are_windows_powershell_compatible() -> None:
    for relative in (
        "RotoWeaveClient/Start.ps1",
        "RotoWeaveServer/Start.ps1",
    ):
        content = (WORKSPACE / relative).read_bytes()
        assert content.isascii(), (
            f"{relative} is launched by Windows PowerShell 5.1 and must remain ASCII "
            "unless it is deliberately stored with a UTF-8 BOM"
        )


def test_client_start_replaces_the_existing_client_only_after_preflight() -> None:
    script = (WORKSPACE / "RotoWeaveClient" / "Start.ps1").read_text(encoding="ascii")
    stop_call = (
        "& powershell.exe -NoLogo -NoProfile -NonInteractive "
        "-ExecutionPolicy Bypass -File $stopScript -ApiPort 8766"
    )
    stop_failure = 'throw "Unable to stop the existing RotoWeave client."'
    launch_call = "$clientProcess = Start-Process"

    assert '$stopScript = Join-Path $projectRoot "scripts\\stop-rotoweave-client.ps1"' in script
    assert script.index("if ($needsBuild)") < script.index(stop_call)
    assert script.index(stop_call) < script.index(stop_failure) < script.index(launch_call)


def test_client_start_detaches_the_hidden_client_so_the_command_window_can_close() -> None:
    script = (WORKSPACE / "RotoWeaveClient" / "Start.ps1").read_text(encoding="ascii")
    command = (WORKSPACE / "RotoWeaveClient" / "Start.cmd").read_text(encoding="ascii")

    assert "$clientProcess = Start-Process" in script
    assert "-FilePath $python" in script
    assert '-ArgumentList @("-m", "backend.client_launcher")' in script
    assert "-WindowStyle Hidden" in script
    assert "-PassThru" in script
    assert "if ($clientProcess.HasExited)" in script
    assert "& $python -m backend.client_launcher" not in script
    assert "pause" not in command.lower()


def test_source_start_scripts_inject_the_sibling_contracts_project() -> None:
    for relative, launcher_module in (
        ("RotoWeaveClient/Start.ps1", "$clientProcess = Start-Process"),
        ("RotoWeaveServer/Start.ps1", "$serverProcess = Start-Process"),
    ):
        script = (WORKSPACE / relative).read_text(encoding="ascii")
        injection = "$env:PYTHONPATH = $pythonPathEntries -join [System.IO.Path]::PathSeparator"

        assert "$pythonPathEntries = @($contractsRoot)" in script
        assert injection in script
        assert script.index(injection) < script.index(launcher_module)


def test_client_stop_does_not_terminate_the_browser_process_tree() -> None:
    script = (
        WORKSPACE / "RotoWeaveClient" / "scripts" / "stop-rotoweave-client.ps1"
    ).read_text(encoding="ascii")

    assert "Stop-Process -Id $clientProcessId" in script
    assert "taskkill.exe" not in script
    assert " /T" not in script


def test_client_package_validator_rejects_missing_protocol_manifest(tmp_path: Path) -> None:
    script = WORKSPACE / "validation" / "scripts" / "validate-launcher-packages.py"
    spec = importlib.util.spec_from_file_location("validate_launcher_packages", script)
    assert spec is not None and spec.loader is not None
    validator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(validator)

    root = tmp_path / "RotoWeave-Client"
    internal = root / "_internal"
    (root / "RotoWeave-Client.exe").parent.mkdir(parents=True)
    (root / "RotoWeave-Client.exe").write_bytes(b"launcher")
    (internal / "product.json").parent.mkdir(parents=True)
    (internal / "product.json").write_text('{"version":"4.0.0"}', encoding="utf-8")
    for relative in validator.CLIENT_REQUIRED_FILES:
        target = internal / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"asset")
    (internal / "contracts" / "protocols.json").write_text(
        '{"schemaVersion":1,"productVersion":"4.0.0"}', encoding="utf-8"
    )
    (internal / "frontend" / "index.html").write_text(
        '<script src="/assets/index.js"></script><link href="/assets/index.css" rel="stylesheet">',
        encoding="utf-8",
    )
    (internal / "frontend" / "assets" / "index.js").parent.mkdir(parents=True)
    (internal / "frontend" / "assets" / "index.js").write_bytes(b"js")
    (internal / "frontend" / "assets" / "index.css").write_bytes(b"css")
    assert validator.validate_client(root)["bytes"] == len(b"launcher")
    (internal / "contracts" / "protocols.json").unlink()
    with pytest.raises(SystemExit, match="contracts/protocols.json"):
        validator.validate_client(root)


def test_client_build_stages_and_validates_before_release_promotion() -> None:
    script = (WORKSPACE / "validation" / "scripts" / "build-windows-launchers.ps1").read_text(encoding="utf-8")

    assert "--distpath $stagingRoot" in script
    assert "--distpath $outputRoot" not in script
    assert script.index("--client $stagedClientRoot") < script.index("Move-Item -LiteralPath $stagedClientRoot")
    assert "the existing release was not modified" in script
    assert 'Join-Path $projectRoot "RotoWeaveClient"' in script
    assert "Temp\\CudaRuntime" not in script


def test_server_build_stages_and_validates_before_release_promotion() -> None:
    script = (
        WORKSPACE / "RotoWeaveServer" / "scripts" / "build-windows-server.ps1"
    ).read_text(encoding="utf-8")

    assert "--distpath $stagingRoot" in script
    assert "--distpath $outputRoot" not in script
    assert script.index("--server $stagedServerRoot") < script.index(
        "Move-Item -LiteralPath $stagedServerRoot"
    )
    assert "the existing release was not modified" in script
    assert "SERVER-MANIFEST.previous.json" in script
    assert "Promoted server package validation failed." in script


def test_retired_split_and_integrated_package_files_are_absent() -> None:
    retired = (
        "RotoWeaveClient/RotoWeave.spec",
        "RotoWeaveClient/backend/requirements.txt",
        "RotoWeaveClient/backend/requirements-win-lock.txt",
        "RotoWeaveClient/scripts/build-windows.ps1",
        "RotoWeaveClient/scripts/export-ultra-models-onnx.py",
        "RotoWeaveClient/scripts/measure-startup.ps1",
        "RotoWeaveClient/scripts/start-dev.ps1",
        "RotoWeaveClient/scripts/validate-bundled-models.py",
        "RotoWeaveServer/scripts/run-high-ultra-ab.ps1",
        "RotoWeaveServer/scripts/setup-cuda-environment.ps1",
        "Docs/LAN_SERVICE_LAUNCHER.zh-CN.md",
        "validation/Start-RotoWeave-Test.cmd",
        "validation/scripts/build-windows-isolated.ps1",
        "validation/scripts/classify-workspace-residue.py",
        "validation/scripts/download-verified-large-file.ps1",
        "validation/scripts/finalize-high-ultra-ab.py",
        "validation/scripts/install-local-sam3-ultra.ps1",
        "validation/scripts/package-latest.cmd",
        "validation/scripts/package-latest.ps1",
        "validation/scripts/requirements-ultra-export.txt",
        "validation/scripts/restore-offline-model-assets.ps1",
        "validation/scripts/self-test-local-sam3.py",
        "validation/scripts/start-dev-local-ultra.ps1",
        "validation/scripts/validate-signed-production-routes.py",
        "validation/scripts/write-release-manifest.py",
    )
    assert not [relative for relative in retired if (WORKSPACE / relative).exists()]


def test_server_start_command_uses_only_the_source_server_project() -> None:
    launcher = (WORKSPACE / "RotoWeaveServer" / "Start.cmd").read_text(encoding="utf-8")
    start_script = (WORKSPACE / "RotoWeaveServer" / "Start.ps1").read_text(encoding="utf-8")

    assert r"Start.ps1" in launcher
    assert "ROTOWEAVE_NO_PAUSE" in launcher
    assert "pause" in launcher.lower()
    assert "release\\server-only" not in launcher
    assert "RotoWeave-Server.exe" not in launcher
    assert "repair-server-runtimes" not in start_script
    assert 'import PIL, pystray' in start_script
    assert '-ArgumentList @("-m", "server.launcher")' in start_script
    assert "-WindowStyle Hidden" in start_script
    assert 'Join-Path $serverDataRoot "server.pid"' in start_script
    assert "$serverProcess.HasExited" in start_script
    assert "did not become ready within 30 seconds" in start_script
    assert "notification-area icon" in start_script
    assert not (WORKSPACE / "RotoWeaveServer" / "scripts" / "repair-server-runtimes.ps1").exists()


def test_server_tray_dependencies_are_locked_for_source_and_packaged_launchers() -> None:
    requirements = (WORKSPACE / "RotoWeaveServer" / "requirements.txt").read_text(encoding="utf-8")
    lock = (WORKSPACE / "RotoWeaveServer" / "requirements-win-lock.txt").read_text(encoding="utf-8")

    for dependency in ("pystray==0.19.5", "six==1.17.0"):
        assert dependency in requirements
        assert dependency in lock


def test_server_windowed_launcher_tolerates_missing_console_streams(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server_launcher.sys, "stdout", None)
    monkeypatch.setattr(server_launcher.sys, "stderr", None)

    server_launcher._console_message("windowed output")
    server_launcher._console_message("windowed error", error=True)


def test_server_launcher_main_builds_host_and_enters_tray(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []

    class FakeHost:
        def __init__(self, settings) -> None:
            assert isinstance(settings, RemoteServerSettings)
            self.alive = True
            self.last_error = None
            events.append("host-created")

        def start(self) -> None:
            events.append("host-started")

        def stop(self) -> None:
            events.append("host-stopped")

    root = tmp_path / "server-data"
    connection = {
        "endpoint": "http://127.0.0.1:18443",
        "admin": "http://127.0.0.1:18444",
        "modelLibrary": str(tmp_path / "models"),
        "openAdminPage": False,
    }
    monkeypatch.setenv("ROTOWEAVE_REMOTE_DATA_ROOT", str(root))
    monkeypatch.setenv("ROTOWEAVE_REMOTE_HOST", "127.0.0.1")
    monkeypatch.setenv("ROTOWEAVE_REMOTE_PORT", "18443")
    monkeypatch.setenv("ROTOWEAVE_REMOTE_ADMIN_PORT", "18444")
    monkeypatch.setattr(server_launcher, "_configure_logging", lambda _root: root / "logs" / "launcher.log")
    monkeypatch.setattr(server_launcher, "apply_config", lambda: (root / "server-launcher.json", connection))
    monkeypatch.setattr(server_launcher, "_ensure_ports_available", lambda _connection: None)
    monkeypatch.setattr(server_launcher, "ServerHost", FakeHost)
    monkeypatch.setattr(server_launcher, "_write_pid_marker", lambda _path: events.append("pid-written"))
    monkeypatch.setattr(server_launcher, "_remove_owned_pid_marker", lambda _path: events.append("pid-removed"))
    monkeypatch.setattr(server_launcher, "_run_with_tray", lambda *_args: events.append("tray-entered"))
    monkeypatch.setattr(server_launcher, "pystray", object())

    server_launcher.main()

    assert events == [
        "host-created",
        "host-started",
        "pid-written",
        "tray-entered",
        "host-stopped",
        "pid-removed",
    ]


def test_server_launcher_opens_admin_page_when_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    class ReadyResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

    opened: list[str] = []
    monkeypatch.setattr(server_launcher.urllib.request, "urlopen", lambda *_args, **_kwargs: ReadyResponse())
    monkeypatch.setattr(server_launcher, "_open_admin", lambda url: opened.append(url) or True)

    server_launcher._open_admin_when_ready("http://127.0.0.1:8444")

    assert opened == ["http://127.0.0.1:8444"]


def test_server_host_starts_and_stops_both_managed_listeners(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeService:
        def __init__(self, _settings) -> None:
            self.started = False
            self.stopped = False

        def start(self) -> None:
            self.started = True

        def stop(self) -> None:
            self.stopped = True

    class FakeServer:
        def __init__(self, _config) -> None:
            self.should_exit = False
            self.force_exit = False

        def run(self) -> None:
            while not self.should_exit and not self.force_exit:
                time.sleep(0.005)

    service = FakeService(None)
    servers: list[FakeServer] = []

    def create_server(config) -> FakeServer:
        server = FakeServer(config)
        servers.append(server)
        return server

    monkeypatch.setattr(server_launcher, "RemoteService", lambda _settings: service)
    monkeypatch.setattr(server_launcher, "create_admin_app", lambda _service: object())
    monkeypatch.setattr(server_launcher, "create_remote_app", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(server_launcher.uvicorn, "Config", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(server_launcher.uvicorn, "Server", create_server)
    monkeypatch.setattr(server_launcher, "_port_is_listening", lambda *_args: True)
    settings = SimpleNamespace(admin_host="127.0.0.1", admin_port=8444, api_host="127.0.0.1", api_port=8443)

    host = server_launcher.ServerHost(settings)
    host.start(timeout_seconds=0.2)
    assert host.alive is True
    assert service.started is True
    assert len(servers) == 2

    host.stop()
    assert host.alive is False
    assert service.stopped is True
    assert all(server.should_exit for server in servers)


def test_server_host_real_listeners_release_after_graceful_stop(tmp_path: Path) -> None:
    script = r'''
import json
import os
import socket
import urllib.request
from server.config import RemoteServerSettings
from server.launcher import ServerHost

def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]

api_port = free_port()
admin_port = free_port()
while admin_port == api_port:
    admin_port = free_port()
os.environ["ROTOWEAVE_REMOTE_HOST"] = "127.0.0.1"
os.environ["ROTOWEAVE_REMOTE_PORT"] = str(api_port)
os.environ["ROTOWEAVE_REMOTE_ADMIN_PORT"] = str(admin_port)
settings = RemoteServerSettings()
host = ServerHost(settings)
host.start(timeout_seconds=15)
with urllib.request.urlopen(f"http://127.0.0.1:{admin_port}/api/admin/v2/overview", timeout=3) as response:
    payload = json.load(response)
assert response.status == 200
assert payload["service"].startswith("RotoWeave")
host.stop()
assert not host.alive
for port in (api_port, admin_port):
    try:
        socket.create_connection(("127.0.0.1", port), timeout=0.3)
    except OSError:
        continue
    raise AssertionError(f"port remains open: {port}")
print(f"REAL_HOST_OK api={api_port} admin={admin_port} ports_released=true")
'''
    environment = os.environ.copy()
    python_path = [str(WORKSPACE / "RotoWeaveContracts"), str(WORKSPACE / "RotoWeaveServer")]
    if environment.get("PYTHONPATH"):
        python_path.append(environment["PYTHONPATH"])
    environment.update(
        {
            "PYTHONPATH": os.pathsep.join(python_path),
            "ROTOWEAVE_REMOTE_DATA_ROOT": str(tmp_path / "server-data"),
            "ROTOWEAVE_LAUNCHER_NO_BROWSER": "1",
            "ROTOWEAVE_LAUNCHER_NO_TRAY": "1",
        }
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=environment,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout
    assert "REAL_HOST_OK" in completed.stdout
    database = tmp_path / "server-data" / "queue.sqlite3"
    renamed = database.with_suffix(".released")
    database.replace(renamed)
    renamed.replace(database)


def test_server_tray_exit_requires_confirmation_for_active_jobs(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeHost:
        stopped = False

        def stop(self) -> None:
            self.stopped = True

    class FakeIcon:
        stopped = False

        def stop(self) -> None:
            self.stopped = True

    host = FakeHost()
    icon = FakeIcon()
    stopped = threading.Event()
    monkeypatch.setattr(server_launcher, "_message", lambda *_args: 7)

    assert server_launcher._exit_from_tray(host, icon, stopped, active=2) is False
    assert host.stopped is False
    assert icon.stopped is False
    assert stopped.is_set() is False

    monkeypatch.setattr(server_launcher, "_message", lambda *_args: 6)
    assert server_launcher._exit_from_tray(host, icon, stopped, active=2) is True
    assert host.stopped is True
    assert icon.stopped is True
    assert stopped.is_set() is True


def test_server_tray_snapshot_reports_active_queue_and_model_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    host = SimpleNamespace(alive=True)
    monkeypatch.setattr(
        server_launcher,
        "_json_request",
        lambda _url: {
            "queue": {"states": {"queued": 2, "running": 1}},
            "startup": {"state": "ready"},
            "worker": {"state": "profile-unavailable"},
        },
    )
    assert server_launcher._service_snapshot("http://127.0.0.1:8444", host) == {
        "state": "processing",
        "active": 3,
        "label": "正在处理 3 个任务",
    }

    monkeypatch.setattr(
        server_launcher,
        "_json_request",
        lambda _url: {
            "queue": {"states": {}},
            "startup": {"state": "ready"},
            "worker": {"state": "profile-unavailable"},
        },
    )
    assert server_launcher._service_snapshot("http://127.0.0.1:8444", host)["state"] == "warning"


def test_server_launcher_detects_an_existing_listener() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(5)
        port = int(listener.getsockname()[1])

        assert server_launcher._port_is_listening("127.0.0.1", port) is True
        with pytest.raises(RuntimeError, match="Stop.cmd"):
            server_launcher._ensure_ports_available(
                {
                    "endpoint": f"http://127.0.0.1:{port}",
                    "admin": "http://127.0.0.1:65534",
                }
            )


def test_server_launcher_pid_marker_is_owned_and_atomic(tmp_path: Path) -> None:
    marker = tmp_path / "server.pid"

    server_launcher._write_pid_marker(marker)
    payload = json.loads(marker.read_text(encoding="utf-8"))
    assert payload["productVersion"] == "4.0.0"
    assert payload["pid"] == os.getpid()
    assert not marker.with_suffix(".pid.tmp").exists()

    server_launcher._remove_owned_pid_marker(marker)
    assert not marker.exists()


def test_server_stop_script_validates_process_identity_and_closes_the_tree() -> None:
    command = (WORKSPACE / "RotoWeaveServer" / "Stop.cmd").read_text(encoding="utf-8")
    script = (WORKSPACE / "RotoWeaveServer" / "scripts" / "stop-rotoweave-server.ps1").read_text(encoding="utf-8")
    root_stop = (WORKSPACE / "scripts" / "Stop-RotoWeave.ps1").read_text(encoding="utf-8")

    assert "stop-rotoweave-server.ps1" in command
    assert "RotoWeaveServer\\scripts\\stop-rotoweave-server.ps1" in root_stop
    assert "server.pid" in script
    assert "Get-NetTCPConnection" in script
    assert 'Get-Process -Name "RotoWeave-Server"' in script
    assert "Get-CimInstance Win32_Process" in script
    assert "server(?:\\.launcher)?" in script
    assert "taskkill.exe" in script
    assert "Invoke-TaskKill" in script
    assert "RedirectStandardError" in script
    assert "Wait-ProcessExit" in script
    assert "Stop-Process -Id $serverProcessId -Force" in script
    assert "Server process remains active" in script
    assert "Refusing to stop a non-RotoWeave listener" in script


def test_server_runtime_staging_uses_fixed_product_runtimes_without_weights() -> None:
    preparation = (WORKSPACE / "RotoWeaveServer" / "scripts" / "prepare-server-runtimes.py").read_text(encoding="utf-8")

    assert 'WORKSPACE / "server-runtimes"' in preparation
    assert '"model-packs"' not in preparation
    assert "FORBIDDEN_WEIGHT_SUFFIXES" in preparation
    assert '"modelWeightsIncluded": False' in preparation
