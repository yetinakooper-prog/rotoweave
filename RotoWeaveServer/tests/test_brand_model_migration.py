from __future__ import annotations

from pathlib import Path

from server.model_center import ModelCenter
from server.repository import RemoteQueueRepository, utc_now


def test_startup_migration_keeps_only_latest_active_configuration_and_references(tmp_path: Path) -> None:
    repository = RemoteQueueRepository(tmp_path / "data" / "queue.sqlite3")
    models = tmp_path / "models"
    models.mkdir()
    current_file = models / "current.pth"
    stale_file = models / "stale.pth"
    current_file.write_bytes(b"current")
    stale_file.write_bytes(b"stale")
    unused_root = tmp_path / "unused"
    unused_root.mkdir()
    now = utc_now()
    with repository.transaction() as connection:
        connection.execute(
            "INSERT INTO model_library_roots VALUES(?,?,?,0,1,0,?,?)",
            ("root-current", "current", str(models), now, now),
        )
        connection.execute(
            "INSERT INTO model_library_roots VALUES(?,?,?,1,1,0,?,?)",
            ("root-unused", "unused", str(unused_root), now, now),
        )
        for asset_id, path, role in [
            ("asset-current", current_file, "roi_refine"),
            ("asset-stale", stale_file, "alpha_and_tracking"),
        ]:
            connection.execute(
                "INSERT INTO model_assets(id,root_id,role,model_id,path,bytes,sha256,state,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,'candidate',?,?)",
                (asset_id, "root-current", role, role, str(path), path.stat().st_size, asset_id, now, now),
            )
        connection.execute("INSERT INTO model_bindings VALUES('roi_refine','asset-current',?)", (now,))
        connection.execute(
            "INSERT INTO model_configurations VALUES('old','recipe','digest','ready',1,'{}',2,?,?)",
            (now, "2026-01-01T00:00:00Z"),
        )
        connection.execute(
            "INSERT INTO model_configurations VALUES('current','recipe','digest','ready',1,'{}',2,?,?)",
            (now, "2026-08-27T00:00:00Z"),
        )
        connection.execute("INSERT INTO model_configuration_assets VALUES('current','roi_refine','asset-current')")

    ModelCenter(repository, tmp_path / "runtime")

    with repository.connect() as connection:
        assert [row["digest"] for row in connection.execute("SELECT digest FROM model_configurations")] == ["current"]
        assert [row["id"] for row in connection.execute("SELECT id FROM model_assets")] == ["asset-current"]
        assert connection.execute("SELECT id FROM model_library_roots WHERE id='root-unused'").fetchone() is None
    assert current_file.is_file() and stale_file.is_file()
