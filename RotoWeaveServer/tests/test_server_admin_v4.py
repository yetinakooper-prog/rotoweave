from __future__ import annotations

import json
import hashlib
import socket
import sqlite3
import threading
import time
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from server.api import create_admin_app
from server.config import NetworkSettingsError, RemoteServerSettings, discover_lan_ipv4
import server.config as config_module
from server.model_center import ModelCenter
import server.model_center as model_center_module
from server.repository import QueueRevisionConflict, RemoteQueueRepository
from server.service import RemoteService
from server.startup import StartupTracker
from contracts.deployment_bundles import DeploymentBundleManager


class NoopProcessor:
    def warmup(self):
        return {"hardware": {"gpu": "RTX 4090"}, "modelConfiguration": {"state": "ready", "verifiedFileCount": 5}}

    def process(self, *_args, **_kwargs):
        raise AssertionError("not used")

    def restart(self):
        return None

    def close(self):
        return None


class FailingActivationProcessor(NoopProcessor):
    def __init__(self) -> None:
        self.configured: list[tuple[str, str]] = []

    def configure_configuration(self, payload: dict, profile: str) -> None:
        self.configured.append((payload["configurationDigest"], profile))

    def warmup(self):
        if len(self.configured) == 1:
            raise RuntimeError("new configuration warmup failed")
        return super().warmup()


def _service(tmp_path: Path) -> RemoteService:
    settings = RemoteServerSettings(data_root=tmp_path / "server")
    return RemoteService(settings, processor=NoopProcessor())


def test_unknown_database_schema_is_rejected(tmp_path: Path) -> None:
    database = tmp_path / "queue.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE jobs(id TEXT PRIMARY KEY,idempotency_key TEXT UNIQUE,request_sha256 TEXT,
              submission_json TEXT,state TEXT,progress REAL,stage TEXT,error_json TEXT,input_path TEXT,
              result_path TEXT,result_sha256 TEXT,attempt INTEGER,cancel_requested INTEGER,created_at TEXT,
              updated_at TEXT,started_at TEXT,finished_at TEXT,expires_at TEXT);
            INSERT INTO jobs VALUES('old','key','hash','{}','queued',0,'queued',NULL,'input',NULL,NULL,0,0,
              '2026-01-01T00:00:00Z','2026-01-01T00:00:00Z',NULL,NULL,NULL);
            """
        )
    with pytest.raises(RuntimeError, match="schema 0 不受支持"):
        RemoteQueueRepository(database)


def test_schema3_database_is_backed_up_and_migrated_to_schema4(tmp_path: Path) -> None:
    database = tmp_path / "queue.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            PRAGMA foreign_keys=ON;
            CREATE TABLE model_library_roots(id TEXT PRIMARY KEY,label TEXT,path TEXT UNIQUE,priority INTEGER,enabled INTEGER,read_only INTEGER,created_at TEXT,updated_at TEXT);
            CREATE TABLE model_assets(id TEXT PRIMARY KEY,root_id TEXT REFERENCES model_library_roots(id),role TEXT,model_id TEXT,path TEXT UNIQUE,bytes INTEGER,sha256 TEXT,state TEXT,error_text TEXT,verified_at TEXT,created_at TEXT,updated_at TEXT);
            CREATE TABLE model_configurations(digest TEXT PRIMARY KEY,recipe_id TEXT,recipe_digest TEXT,state TEXT,active INTEGER,profile_states_json TEXT,created_at TEXT,activated_at TEXT);
            INSERT INTO model_library_roots VALUES('root','models','C:/models',0,1,0,'now','now');
            INSERT INTO model_assets VALUES('asset','root','roi_refine','vitmatte-base','C:/models/v.pth',10,'abc','mismatch','old mismatch',NULL,'now','now');
            INSERT INTO model_configurations VALUES('legacy','recipe','digest','ready',1,'{}','now','now');
            PRAGMA user_version=3;
            """
        )
    repository = RemoteQueueRepository(database)
    backups = list(tmp_path.glob("queue.sqlite3.schema3-backup-*") )
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as backup:
        assert backup.execute("PRAGMA user_version").fetchone()[0] == 3
    with repository.connect() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 4
        asset = connection.execute("SELECT state,error_text FROM model_assets WHERE id='asset'").fetchone()
        assert asset["state"] == "candidate" and asset["error_text"] is None
        configuration = connection.execute("SELECT schema_version,active FROM model_configurations WHERE digest='legacy'").fetchone()
        assert dict(configuration) == {"schema_version": 1, "active": 1}


def test_schema3_migration_failure_rolls_back_and_keeps_backup(tmp_path: Path) -> None:
    database = tmp_path / "queue.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE model_assets(id TEXT PRIMARY KEY,root_id TEXT,role TEXT,model_id TEXT,path TEXT,bytes INTEGER,sha256 TEXT,state TEXT,error_text TEXT,verified_at TEXT,created_at TEXT,updated_at TEXT);
            CREATE TABLE model_configurations(digest TEXT PRIMARY KEY,recipe_id TEXT,recipe_digest TEXT,state TEXT,active INTEGER,profile_states_json TEXT,created_at TEXT,activated_at TEXT);
            CREATE TABLE model_asset_verification_receipts(blocker TEXT);
            PRAGMA user_version=3;
            """
        )
    with pytest.raises(sqlite3.OperationalError, match="already exists"):
        RemoteQueueRepository(database)
    assert len(list(tmp_path.glob("queue.sqlite3.schema3-backup-*"))) == 1
    with sqlite3.connect(database) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(model_assets)")}
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 3
        assert "verification_kind" not in columns


def test_fresh_database_contains_only_current_schema(tmp_path: Path) -> None:
    repository = RemoteQueueRepository(tmp_path / "queue.sqlite3")
    with repository.connect() as connection:
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(jobs)")}
        tables = {row["name"] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 4
    assert {"queue_order", "parent_job_id", "model_configuration_digest", "quality_profile"}.issubset(columns)
    assert {"queue_control", "operational_logs", "model_library_roots", "model_assets", "model_asset_verification_receipts", "model_bindings", "model_configurations", "model_self_test_receipts", "model_operations"}.issubset(tables)


def test_pause_reorder_revision_retry_delete_and_audit(tmp_path: Path) -> None:
    repository = RemoteQueueRepository(tmp_path / "queue.sqlite3")
    inputs = []
    for index in range(2):
        input_path = tmp_path / f"{index}.zip"
        input_path.write_bytes(b"input")
        inputs.append(input_path)
        repository.enqueue(job_id=f"job-{index}", idempotency_key=f"key-{index}", request_sha256="a" * 64,
                           submission={}, input_path=str(input_path))
    paused = repository.set_queue_control(paused=True)
    assert paused["paused"] is True and repository.claim_next() is None
    with pytest.raises(QueueRevisionConflict):
        repository.reorder(["job-1", "job-0"], paused["revision"] - 1)
    ordered = repository.reorder(["job-1", "job-0"], paused["revision"])
    assert ordered["revision"] > paused["revision"]
    with repository.transaction() as connection:
        connection.execute("UPDATE jobs SET state='failed' WHERE id='job-0'")
    retried = repository.retry("job-0", "job-retry", "retry-key", str(inputs[0]))
    assert retried["parent_job_id"] == "job-0"
    assert repository.delete("job-0", terminal_only=True)
    logs = repository.query_logs(event="job.deleted")
    assert logs["total"] == 1 and logs["items"][0]["job_id"] == "job-0"


@pytest.mark.anyio
async def test_admin_requires_loopback_host_origin_and_csrf(tmp_path: Path) -> None:
    service = _service(tmp_path)
    app = create_admin_app(service)
    local = httpx.ASGITransport(app=app, client=("127.0.0.1", 51000))
    async with httpx.AsyncClient(transport=local, base_url="http://127.0.0.1:8444") as client:
        assert (await client.get("/api/admin/v1/session")).status_code == 404
        assert (await client.post("/api/admin/v1/queue/pause", json={})).status_code == 404
        session = await client.get("/api/admin/v2/session")
        assert session.status_code == 200
        token = session.json()["csrfToken"]
        assert (await client.post("/api/admin/v2/queue/pause", json={})).status_code == 403
        assert (await client.post("/api/admin/v2/queue/pause", json={}, headers={"X-RotoWeave-Admin-CSRF": token})).status_code == 200
        assert (await client.post("/api/admin/v2/queue/resume", json={}, headers={"X-AIFrame-Admin-CSRF": token})).status_code == 200
        conflict = await client.post(
            "/api/admin/v2/queue/pause",
            json={},
            headers={
                "X-RotoWeave-Admin-CSRF": token,
                "X-AIFrame-Admin-CSRF": "different",
            },
        )
        assert conflict.status_code == 400
        assert conflict.json()["code"] == "invalid_request"
        assert conflict.json()["detail"]["code"] == "identity_conflict"
        assert (await client.get("/api/admin/v2/session", headers={"Origin": "https://evil.example"})).status_code == 403
        assert (await client.get("/api/admin/v2/session", headers={"Host": "evil.example"})).status_code == 403
        v2 = await client.get("/api/admin/v2/model-center")
        assert v2.status_code == 200 and v2.json()["recipe"]["id"] == "matting-high-ultra-v1"
    remote = httpx.ASGITransport(app=app, client=("10.1.2.3", 51001))
    async with httpx.AsyncClient(transport=remote, base_url="http://127.0.0.1:8444") as client:
        assert (await client.get("/api/admin/v2/session")).status_code == 403


@pytest.mark.anyio
async def test_admin_native_selection_api_and_retired_routes(tmp_path: Path) -> None:
    service = _service(tmp_path)
    library = tmp_path / "models"
    library.mkdir()
    candidate = library / "vitmatte-local.pth"
    candidate.write_bytes(b"local-version")
    class Picker:
        def choose_folder(self):
            return None
        def choose_file(self, _display_name: str):
            return candidate
    service.model_center._picker = Picker()
    app = create_admin_app(service)
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 41002))
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8444") as client:
        token = (await client.get("/api/admin/v2/session")).json()["csrfToken"]
        headers = {"X-RotoWeave-Admin-CSRF": token}
        cancelled = await client.post("/api/admin/v2/model-selections/folder-dialog", headers=headers)
        assert cancelled.status_code == 200 and cancelled.json() == {"cancelled": True}
        selected = await client.post("/api/admin/v2/model-selections/roi_refine/file-dialog", headers=headers)
        assert selected.status_code == 200 and selected.json()["cancelled"] is False
        operation = _wait_operation(service.model_center, selected.json()["operation"]["id"])
        assert operation["state"] == "passed"
        selected_slot = next(item for item in service.model_center.snapshot()["slots"] if item["role"] == "roi_refine")
        assert selected_slot["binding"]["path"] == str(candidate)
        for method, path in [
            ("POST", "/api/admin/v2/model-roots"),
            ("POST", "/api/admin/v2/model-scans"),
            ("POST", "/api/admin/v2/model-slots/roi_refine/candidates"),
            ("PUT", "/api/admin/v2/model-bindings/roi_refine"),
            ("POST", "/api/admin/v2/model-configurations/rollback"),
        ]:
            assert (await client.request(method, path, headers=headers, json={})).status_code == 404


@pytest.mark.anyio
async def test_deployment_bundle_admin_routes_keep_loopback_origin_and_csrf_gates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        DeploymentBundleManager,
        "plan",
        lambda self: {"role": self.role, "ready": True, "pageExportEnabled": True},
    )
    monkeypatch.setattr(
        DeploymentBundleManager,
        "select_directory",
        lambda self: {"selectionToken": "a" * 32, "displayPath": "E:\\Bundles"},
    )
    app = create_admin_app(_service(tmp_path))
    local = httpx.ASGITransport(app=app, client=("127.0.0.1", 51000))
    async with httpx.AsyncClient(transport=local, base_url="http://127.0.0.1:8444") as client:
        token = (await client.get("/api/admin/v2/session")).json()["csrfToken"]
        plan = await client.get("/api/admin/v2/deployment-bundles/plan")
        assert plan.status_code == 200 and plan.json()["role"] == "server"
        assert (await client.get("/api/admin/v2/deployment-bundles/plan", headers={"Origin": "https://evil.example"})).status_code == 403
        assert (await client.post("/api/admin/v2/deployment-bundles/output-directory-dialog")).status_code == 403
        selected = await client.post(
            "/api/admin/v2/deployment-bundles/output-directory-dialog",
            headers={"X-RotoWeave-Admin-CSRF": token},
        )
        assert selected.status_code == 200 and selected.json()["selectionToken"] == "a" * 32
    remote = httpx.ASGITransport(app=app, client=("10.1.2.3", 51001))
    async with httpx.AsyncClient(transport=remote, base_url="http://127.0.0.1:8444") as client:
        assert (await client.get("/api/admin/v2/deployment-bundles/plan")).status_code == 403


@pytest.mark.anyio
async def test_admin_network_settings_show_effective_endpoint_and_save_restart_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config_module, "discover_lan_ipv4", lambda: "192.168.31.44")
    monkeypatch.setattr(config_module, "_assert_endpoint_available", lambda _host, _port: None)
    service = _service(tmp_path)
    app = create_admin_app(service)
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 51000))
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8444") as client:
        token = (await client.get("/api/admin/v2/session")).json()["csrfToken"]
        overview = (await client.get("/api/admin/v2/overview")).json()
        assert overview["network"] == {
            "serviceHost": "192.168.31.44",
            "serviceEndpoint": "http://192.168.31.44:8443",
            "apiHost": "127.0.0.1",
            "apiPort": 8443,
            "endpoint": "http://127.0.0.1:8443",
            "apiPath": "/api/matting/v1",
            "scope": "loopback",
            "loopbackOnly": True,
            "adminHost": "127.0.0.1",
            "adminPort": 8444,
            "adminEndpoint": "http://127.0.0.1:8444",
            "configuredHost": "192.168.31.44",
            "configuredPort": 8443,
            "configuredEndpoint": "http://192.168.31.44:8443",
            "restartRequired": True,
            "configurationError": None,
            "addressError": None,
        }
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            free_port = int(probe.getsockname()[1])
        denied = await client.put(
            "/api/admin/v2/network-settings",
            json={"apiPort": free_port},
        )
        assert denied.status_code == 403
        saved = await client.put(
            "/api/admin/v2/network-settings",
            json={"apiPort": free_port},
            headers={"X-RotoWeave-Admin-CSRF": token},
        )
        assert saved.status_code == 200
        assert saved.json()["endpoint"] == "http://127.0.0.1:8443"
        assert saved.json()["serviceHost"] == "192.168.31.44"
        assert saved.json()["configuredEndpoint"] == f"http://192.168.31.44:{free_port}"
        assert saved.json()["restartRequired"] is True
        persisted = json.loads(service.settings.launcher_config_path.read_text(encoding="utf-8"))
        assert persisted["apiHost"] == "192.168.31.44"
        assert persisted["apiPort"] == free_port
        assert service.repository.query_logs(event="network.settings_saved")["total"] == 1


@pytest.mark.anyio
async def test_admin_network_settings_reject_invalid_or_occupied_endpoint_without_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config_module, "discover_lan_ipv4", lambda: "127.0.0.1")
    service = _service(tmp_path)
    app = create_admin_app(service)
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 51000))
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8444") as client:
        token = (await client.get("/api/admin/v2/session")).json()["csrfToken"]
        headers = {"X-RotoWeave-Admin-CSRF": token}
        initial = await client.put(
            "/api/admin/v2/network-settings",
            json={"apiPort": 8443},
            headers=headers,
        )
        assert initial.status_code == 200
        config_path = service.settings.launcher_config_path
        before = config_path.read_bytes()
        read_only_host = await client.put(
            "/api/admin/v2/network-settings",
            json={"apiHost": "0.0.0.0", "apiPort": 9000},
            headers=headers,
        )
        assert read_only_host.status_code == 422
        assert read_only_host.json()["code"] == "invalid_request"
        assert read_only_host.json()["detail"]["reason"] == "api_host_read_only"
        invalid_port = await client.put(
            "/api/admin/v2/network-settings",
            json={"apiPort": 1023},
            headers=headers,
        )
        assert invalid_port.status_code == 422
        assert invalid_port.json()["detail"]["reason"] == "invalid_api_port"
        admin_conflict = await client.put(
            "/api/admin/v2/network-settings",
            json={"apiPort": 8444},
            headers=headers,
        )
        assert admin_conflict.status_code == 409
        assert admin_conflict.json()["code"] == "conflict"
        assert admin_conflict.json()["detail"]["reason"] == "api_port_conflict"
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupied:
            occupied.bind(("127.0.0.1", 0))
            occupied.listen(1)
            occupied_port = int(occupied.getsockname()[1])
            unavailable = await client.put(
                "/api/admin/v2/network-settings",
                json={"apiPort": occupied_port},
                headers=headers,
            )
        assert unavailable.status_code == 409
        assert unavailable.json()["code"] == "conflict"
        assert unavailable.json()["detail"]["reason"] == "api_port_unavailable"
        assert config_path.read_bytes() == before


def test_lan_address_discovery_prefers_bindable_physical_private_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config_module, "_default_route_ipv4", lambda: "198.18.0.1")
    monkeypatch.setattr(
        config_module,
        "_hostname_ipv4_candidates",
        lambda: ["172.20.0.1", "192.168.31.44", "127.0.0.1"],
    )
    monkeypatch.setattr(config_module, "_host_is_bindable", lambda host: host == "192.168.31.44")
    assert discover_lan_ipv4() == "192.168.31.44"


def test_lan_address_discovery_rejects_missing_private_interface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config_module, "_default_route_ipv4", lambda: "198.18.0.1")
    monkeypatch.setattr(config_module, "_hostname_ipv4_candidates", lambda: ["127.0.0.1", "169.254.1.20"])
    with pytest.raises(NetworkSettingsError, match="未检测到可绑定"):
        discover_lan_ipv4()


def test_operational_log_redacts_secret_and_tracker_progress_is_derived(tmp_path: Path) -> None:
    repository = RemoteQueueRepository(tmp_path / "queue.sqlite3")
    repository.log(None, "error", "service.secret", {"token": "abc", "nested": {"private_key": "xyz"}})
    detail = repository.query_logs()["items"][0]["detail"]
    assert detail == {"nested": {"private_key": "[REDACTED]"}, "token": "[REDACTED]"}
    tracker = StartupTracker()
    tracker.update("configuration", "passed")
    tracker.update("storage", "passed")
    tracker.update("model_verification", "running", files_verified=5, files_total=10)
    snapshot = tracker.snapshot()
    assert snapshot["progress"] == round(2.5 / 8, 4)


def test_activation_failure_after_switch_enters_maintenance_without_rollback(tmp_path: Path) -> None:
    processor = FailingActivationProcessor()
    service = RemoteService(RemoteServerSettings(data_root=tmp_path / "server"), processor=processor)
    requested = {
        "configurationDigest": "requested",
        "profileExecutionReceipts": {"high": {"state": "passed"}},
    }
    with pytest.raises(RuntimeError, match="new configuration warmup failed"):
        service._activate_configuration(requested)

    assert processor.configured == [("requested", "high")]
    assert service._health["state"] == "maintenance"
    assert service._health["reason"] == "model-configuration-switch-failed"
    assert service.repository.queue_control()["mode"] == "maintenance"


def _wait_operation(center: ModelCenter, operation_id: str) -> dict:
    for _ in range(300):
        operation = center.operation(operation_id)
        if operation["state"] not in {"queued", "running"}:
            return operation
        time.sleep(0.01)
    raise AssertionError("model operation did not finish")


def test_native_picker_allows_only_one_dialog(tmp_path: Path) -> None:
    entered = threading.Event()
    release = threading.Event()
    class BlockingPicker:
        def choose_folder(self):
            entered.set()
            release.wait(2)
            return None
        def choose_file(self, _display_name: str):
            return None
    center = ModelCenter(RemoteQueueRepository(tmp_path / "queue.sqlite3"), tmp_path, picker=BlockingPicker())
    worker = threading.Thread(target=center.choose_folder)
    worker.start()
    assert entered.wait(1)
    with pytest.raises(ValueError, match="正在打开"):
        center.choose_file(model_center_module.ASSETS[0].role)
    release.set()
    worker.join(2)


def test_folder_selection_preserves_ambiguous_binding_and_reports_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    originals = model_center_module.ASSETS[:2]
    assets = []
    for index, source in enumerate(originals):
        payload = f"official-{index}".encode()
        assets.append(replace(source, bytes=len(payload), sha256=hashlib.sha256(payload).hexdigest()))
    monkeypatch.setattr(model_center_module, "ASSETS", tuple(assets))
    monkeypatch.setattr(model_center_module, "ASSET_BY_ROLE", {item.role: item for item in assets})
    monkeypatch.setattr(model_center_module, "ASSET_BY_SHA256", {item.sha256: item for item in assets})
    monkeypatch.setattr(model_center_module, "PROFILE_ROLES", {
        "high": tuple(item.role for item in assets),
        "ultra": tuple(item.role for item in assets),
    })
    center = ModelCenter(RemoteQueueRepository(tmp_path / "queue.sqlite3"), tmp_path)
    initial_root = tmp_path / "initial"
    initial_root.mkdir()
    initial = initial_root / "chosen.pth"
    initial.write_bytes(b"initial")
    assert _wait_operation(center, center.select_file(assets[0].role, initial)["id"])["state"] == "passed"
    previous_path = center.snapshot()["slots"][0]["binding"]["path"]

    folder = tmp_path / "folder"
    (folder / "a").mkdir(parents=True)
    (folder / "b").mkdir(parents=True)
    (folder / "a" / assets[0].filename).write_bytes(b"one")
    (folder / "b" / assets[0].filename).write_bytes(b"two")
    operation = _wait_operation(center, center.select_folder(folder)["id"])

    assert operation["state"] == "passed"
    assert assets[0].role in operation["detail"]["ambiguousRoles"]
    assert assets[1].role in operation["detail"]["missingRoles"]
    assert center.snapshot()["slots"][0]["binding"]["path"] == previous_path


def test_model_center_scans_exact_recipe_self_tests_both_profiles_and_activates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = RemoteQueueRepository(tmp_path / "queue.sqlite3")
    library = tmp_path / "models"
    library.mkdir()
    assets = []
    for index, source in enumerate(model_center_module.ASSETS):
        payload = (f"recipe-{source.role}-{index}".encode("utf-8")) * (index + 1)
        target = library / source.filename
        target.write_bytes(payload)
        assets.append(replace(source, bytes=len(payload), sha256=hashlib.sha256(payload).hexdigest()))
    monkeypatch.setattr(model_center_module, "ASSETS", tuple(assets))
    monkeypatch.setattr(model_center_module, "ASSET_BY_ROLE", {item.role: item for item in assets})
    monkeypatch.setattr(model_center_module, "ASSET_BY_SHA256", {item.sha256: item for item in assets})
    monkeypatch.setattr(model_center_module, "PROFILE_ROLES", {
        "high": tuple(item.role for item in assets if "high" in item.profiles),
        "ultra": tuple(item.role for item in assets if "ultra" in item.profiles),
    })
    monkeypatch.setattr(model_center_module, "RECIPE", {"id": "matting-high-ultra-v1", "digest": "recipe-test"})
    monkeypatch.setattr(model_center_module, "RECIPE_DIGEST", "recipe-test")
    monkeypatch.setattr(model_center_module, "_current_gpu", lambda _preferred=None: {"uuid": "GPU-test", "name": "NVIDIA Test CUDA Device", "driverVersion": "600.1"})
    center = ModelCenter(repository, tmp_path)
    scan = _wait_operation(center, center.select_folder(library)["id"])
    assert scan["state"] == "passed"
    initial = center.snapshot()
    assert all(slot["binding"] for slot in initial["slots"])
    assert all(slot["binding"]["state"] == "candidate" for slot in initial["slots"])
    verify = _wait_operation(center, center.verify_draft()["id"])
    assert verify["state"] == "passed"
    monkeypatch.setattr(center, "runtime_descriptor", lambda profile: {
        "profile": profile,
        "id": f"rotoweave-{profile}-runtime-v1",
        "digest": hashlib.sha256(f"runtime-{profile}".encode()).hexdigest(),
        "installed": True,
        "source": "test",
        "python": "python.exe",
    })
    tested: list[str] = []
    center.set_profile_tester(lambda profile, payload, _cancel: tested.append(profile) or {
        "state": "passed",
        "profile": profile,
        "configurationDigest": payload["profileConfigurationDigests"][profile],
        "runtimeDigest": center.runtime_descriptor(profile)["digest"],
        "gpuIdentity": "GPU-test",
        "driverVersion": "600.1",
        "executionModes": [{"mode": mode, "state": "passed"} for mode in ("full", "balanced", "constrained", "minimal")],
    })
    self_test = _wait_operation(center, center.self_test()["id"])
    assert self_test["state"] == "passed" and tested == ["high", "ultra"]
    assert all(item["state"] == "ready" for item in center.snapshot()["profiles"].values())
    activated: list[str] = []
    center.set_activator(lambda payload: activated.append(payload["configurationDigest"]) or {"residentProfile": "high"})
    activation = _wait_operation(center, center.activate()["id"])
    assert activation["state"] == "passed"
    snapshot = center.snapshot()
    assert snapshot["activeConfiguration"]["configurationDigest"] == activated[0]
    assert snapshot["recipe"]["id"] == "matting-high-ultra-v1"

    center.set_profile_tester(
        lambda profile, payload, _cancel: (
            {
                "state": "failed",
                "profile": profile,
                "configurationDigest": payload["profileConfigurationDigests"][profile],
                "runtimeDigest": center.runtime_descriptor(profile)["digest"],
                "error": "simulated Ultra incompatibility",
            }
            if profile == "ultra"
            else {
                "state": "passed",
                "profile": profile,
                "configurationDigest": payload["profileConfigurationDigests"][profile],
                "runtimeDigest": center.runtime_descriptor(profile)["digest"],
                "gpuIdentity": "GPU-test",
                "driverVersion": "600.1",
                "executionModes": [{"mode": mode, "state": "passed"} for mode in ("full", "balanced", "constrained", "minimal")],
            }
        )
    )
    partial_test = _wait_operation(center, center.self_test()["id"])
    assert partial_test["state"] == "passed"
    partial_profiles = center.snapshot()["profiles"]
    assert partial_profiles["high"]["state"] == "ready"
    assert partial_profiles["ultra"]["state"] == "blocked"
    partial_activation = _wait_operation(center, center.activate()["id"])
    assert partial_activation["state"] == "passed"

    # A canonical-name file with a different SHA can be bound, safely
    # inspected and qualified without being mislabeled as official.
    first_role = initial["slots"][0]["role"]
    alternate = library / "alternate"
    alternate.mkdir()
    alternate_file = alternate / assets[0].filename
    alternate_file.write_bytes(b"x" * assets[0].bytes)
    assert _wait_operation(center, center.select_file(first_role, alternate_file)["id"])["state"] == "passed"
    center.set_asset_inspector(
        lambda role, path, _cancel: {
            "state": "passed",
            "role": role,
            "path": str(path),
            "bytes": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "compatibilityPolicyDigest": model_center_module.MODEL_COMPATIBILITY_POLICY_DIGEST,
            "observation": {"tensorCount": 1, "tensors": [{"key": "weight", "shape": [1], "dtype": "float32"}]},
            "observationDigest": "observation-test",
        }
    )
    structural_verify = _wait_operation(center, center.verify_draft()["id"])
    assert structural_verify["state"] == "passed"
    structural_binding = center.snapshot()["slots"][0]["binding"]
    assert structural_binding["verificationKind"] == "structural"
    center.set_profile_tester(lambda profile, payload, _cancel: {
        "state": "passed",
        "profile": profile,
        "configurationDigest": payload["profileConfigurationDigests"][profile],
        "runtimeDigest": center.runtime_descriptor(profile)["digest"],
        "gpuIdentity": "GPU-test",
        "driverVersion": "600.1",
        "executionModes": [{"mode": mode, "state": "passed"} for mode in ("full", "balanced", "constrained", "minimal")],
    })
    compatible_test = _wait_operation(center, center.self_test()["id"])
    assert compatible_test["state"] == "passed"
    compatible_profiles = center.snapshot()["profiles"]
    assert compatible_profiles["high"]["qualification"] == "local-compatible"
    assert "alpha_and_tracking" in compatible_profiles["high"]["localCompatibleRoles"]

    # An incompatible Ultra-only candidate cannot invalidate or leak into the
    # High Profile configuration and its existing self-test receipt.
    bad_sam3 = library / "sam3-local.pt"
    bad_sam3.write_bytes(b"not-a-safe-checkpoint")
    assert _wait_operation(center, center.select_file("ultra_alpha", bad_sam3)["id"])["state"] == "passed"
    def isolate_inspector(role: str, path: Path, _cancel) -> dict:
        if role == "ultra_alpha":
            raise RuntimeError("simulated unsafe SAM3")
        observation = {"tensorCount": 1, "tensors": [{"key": "weight", "shape": [1], "dtype": "float32"}]}
        return {
            "state": "passed",
            "role": role,
            "path": str(path),
            "bytes": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "compatibilityPolicyDigest": model_center_module.MODEL_COMPATIBILITY_POLICY_DIGEST,
            "observation": observation,
            "observationDigest": "observation-test",
        }

    center.set_asset_inspector(isolate_inspector)
    isolated_verify = _wait_operation(center, center.verify_draft()["id"])
    assert isolated_verify["state"] == "passed"
    isolated_profiles = center.snapshot()["profiles"]
    assert isolated_profiles["high"]["state"] == "ready"
    assert isolated_profiles["ultra"]["state"] == "blocked"
    tested.clear()
    center.set_profile_tester(lambda profile, payload, _cancel: tested.append(profile) or {
        "state": "passed",
        "profile": profile,
        "configurationDigest": payload["profileConfigurationDigests"][profile],
        "runtimeDigest": center.runtime_descriptor(profile)["digest"],
        "gpuIdentity": "GPU-test",
        "driverVersion": "600.1",
        "executionModes": [{"mode": mode, "state": "passed"} for mode in ("full", "balanced", "constrained", "minimal")],
    })
    assert _wait_operation(center, center.self_test()["id"])["state"] == "passed"
    assert tested == ["high"]
    assert _wait_operation(center, center.activate()["id"])["state"] == "passed"
    active_assets = center.active_configuration()["assets"]
    assert "ultra_alpha" not in active_assets
    with repository.transaction() as connection:
        connection.execute(
            "UPDATE model_asset_verification_receipts SET receipt_json='{}' WHERE role='alpha_and_tracking'"
        )
    active_digest = center.active_configuration()["configurationDigest"]
    assert center.profile_states_for_configuration(active_digest)["high"]["state"] == "blocked"
