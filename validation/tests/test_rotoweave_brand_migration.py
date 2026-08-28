from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from contracts.brand_migration import (
    CLIENT_DIRECTORY,
    LEGACY_CLIENT_DIRECTORY,
    LEGACY_SERVER_DIRECTORY,
    MODEL_SETTINGS_MIGRATION,
    SERVER_DIRECTORY,
    migrate_client_local_app_data,
    migrate_server_local_app_data,
)
from contracts.legacy_compat import (
    LegacyIdentityConflict,
    compatible_environment_value,
    compatible_header_value,
)
from backend.app.workspace_format import (
    LEGACY_WORKSPACE_DOMAIN_KIND,
    LEGACY_WORKSPACE_KIND,
    LEGACY_WORKSPACE_MANIFEST,
    WORKSPACE_DOMAIN,
    WORKSPACE_MANIFEST,
    atomic_write_json,
    create_workspace,
    finalize_aggregate,
    inspect_legacy_workspace,
    migrate_legacy_workspace,
    read_json,
)


def test_environment_prefers_canonical_and_rejects_conflict() -> None:
    assert compatible_environment_value(
        "ROTOWEAVE_MODELS_ROOT", environ={"AIFRAME_MODELS_ROOT": "D:/models"}
    ) == "D:/models"
    assert compatible_environment_value(
        "ROTOWEAVE_MODELS_ROOT",
        environ={"ROTOWEAVE_MODELS_ROOT": "D:/models", "AIFRAME_MODELS_ROOT": "D:/models"},
    ) == "D:/models"
    with pytest.raises(LegacyIdentityConflict, match="值冲突"):
        compatible_environment_value(
            "ROTOWEAVE_MODELS_ROOT",
            environ={"ROTOWEAVE_MODELS_ROOT": "D:/new", "AIFRAME_MODELS_ROOT": "D:/old"},
        )


def test_headers_accept_legacy_and_reject_dual_conflict() -> None:
    assert compatible_header_value(
        {"X-AIFrame-Protocol-Version": "1"},
        "X-RotoWeave-Protocol-Version",
        "X-AIFrame-Protocol-Version",
    ) == "1"
    with pytest.raises(LegacyIdentityConflict, match="值冲突"):
        compatible_header_value(
            {"X-RotoWeave-Protocol-Version": "1", "X-AIFrame-Protocol-Version": "2"},
            "X-RotoWeave-Protocol-Version",
            "X-AIFrame-Protocol-Version",
        )


def test_client_local_app_data_uses_staging_and_preserves_legacy(tmp_path: Path) -> None:
    source = tmp_path / LEGACY_CLIENT_DIRECTORY
    source.mkdir()
    (source / "client-launcher.json").write_text('{"schemaVersion":2}\n', encoding="utf-8")
    (source / "workspaces").mkdir()
    (source / "workspaces" / "keep.txt").write_text("keep", encoding="utf-8")
    target = migrate_client_local_app_data(tmp_path)
    assert target == tmp_path / CLIENT_DIRECTORY
    assert source.is_dir()
    assert (target / "workspaces" / "keep.txt").read_text(encoding="utf-8") == "keep"
    assert json.loads((target / "rotoweave-brand-migration.json").read_text(encoding="utf-8"))["role"] == "client"


def test_server_migration_copies_only_stable_configuration(tmp_path: Path) -> None:
    source = tmp_path / LEGACY_SERVER_DIRECTORY
    source.mkdir()
    (source / "server-launcher.json").write_text('{"schemaVersion":3}\n', encoding="utf-8")
    database = source / "queue.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE model_library_roots(
                id TEXT PRIMARY KEY,label TEXT,path TEXT,priority INTEGER,
                enabled INTEGER,read_only INTEGER
            );
            CREATE TABLE model_assets(
                id TEXT PRIMARY KEY,role TEXT,model_id TEXT,path TEXT,
                bytes INTEGER,sha256 TEXT
            );
            CREATE TABLE model_bindings(role TEXT PRIMARY KEY,asset_id TEXT);
            CREATE TABLE jobs(id TEXT PRIMARY KEY);
            CREATE TABLE logs(id INTEGER PRIMARY KEY);
            CREATE TABLE model_self_test_receipts(configuration_digest TEXT);
            """
        )
        old_models = tmp_path / "AIFrameModels" / "library"
        connection.execute(
            "INSERT INTO model_library_roots VALUES(?,?,?,?,?,?)",
            ("root-old", "Legacy models", str(old_models), 1, 1, 1),
        )
        connection.execute(
            "INSERT INTO model_assets VALUES(?,?,?,?,?,?)",
            ("asset-old", "alpha_and_tracking", "sam2matting-bplus", str(old_models / "model.pt"), 7, "a" * 64),
        )
        connection.execute(
            "INSERT INTO model_bindings VALUES(?,?)",
            ("alpha_and_tracking", "asset-old"),
        )
        connection.execute("INSERT INTO jobs VALUES('do-not-migrate')")
        connection.execute("INSERT INTO logs VALUES(1)")
        connection.execute("INSERT INTO model_self_test_receipts VALUES('stale')")
    for name in ("server.pid", "queue.sqlite3-wal", "temporary-upload.bin"):
        (source / name).write_text("ephemeral", encoding="utf-8")
    (source / "logs").mkdir()
    target = migrate_server_local_app_data(tmp_path)
    assert target == tmp_path / SERVER_DIRECTORY
    assert (target / "server-launcher.json").is_file()
    migrated = json.loads((target / MODEL_SETTINGS_MIGRATION).read_text(encoding="utf-8"))
    assert migrated["queueMigrated"] is False
    assert migrated["selfTestReceiptsMigrated"] is False
    assert migrated["bindings"]["alpha_and_tracking"]["sha256"] == "a" * 64
    assert "RotoWeaveModels" in migrated["bindings"]["alpha_and_tracking"]["path"]
    assert not (target / "queue.sqlite3").exists()
    assert not (target / "model-configurations").exists()
    assert not (target / "server.pid").exists()
    assert not (target / "queue.sqlite3-wal").exists()
    assert not (target / "logs").exists()
    assert source.is_dir()


def test_workspace_migration_backs_up_then_writes_rotoweave_identity(tmp_path: Path) -> None:
    root = tmp_path / "legacy-workspace"
    create_workspace(root, "Legacy 4.0")
    manifest, _ = read_json(root / WORKSPACE_MANIFEST)
    legacy_manifest, _ = finalize_aggregate(
        {**manifest, "kind": LEGACY_WORKSPACE_KIND}, previous=manifest
    )
    atomic_write_json(root / LEGACY_WORKSPACE_MANIFEST, legacy_manifest)
    (root / WORKSPACE_MANIFEST).unlink()
    domain, _ = read_json(root / WORKSPACE_DOMAIN)
    legacy_domain, _ = finalize_aggregate(
        {**domain, "kind": LEGACY_WORKSPACE_DOMAIN_KIND}, previous=domain
    )
    atomic_write_json(root / WORKSPACE_DOMAIN, legacy_domain)

    assert inspect_legacy_workspace(root)["migratable"] is True
    result = migrate_legacy_workspace(root)
    assert result["state"] == "migrated"
    assert (root / WORKSPACE_MANIFEST).is_file()
    assert not (root / LEGACY_WORKSPACE_MANIFEST).exists()
    assert (root / "aiframe.json.aiframetools-4.0.bak").is_file()
    assert (root / "domain" / "workspace-state.aiframetools-4.0.bak.json").is_file()
    assert inspect_legacy_workspace(root)["state"] == "current"


def test_workspace_migration_rejects_dual_metadata(tmp_path: Path) -> None:
    root = tmp_path / "dual-workspace"
    create_workspace(root, "Dual")
    (root / LEGACY_WORKSPACE_MANIFEST).write_bytes((root / WORKSPACE_MANIFEST).read_bytes())
    with pytest.raises(Exception, match="同时存在"):
        inspect_legacy_workspace(root)
