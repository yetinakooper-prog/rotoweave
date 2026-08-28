from __future__ import annotations

from pathlib import Path

import pytest

from contracts import deployment_bundles
from contracts import windows_file_dialog
from backend.app import workspace_session


def test_modern_folder_picker_resolves_existing_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(windows_file_dialog.os, "name", "nt")
    monkeypatch.setattr(
        windows_file_dialog,
        "_show_windows_folder_dialog",
        lambda title: str(tmp_path) if title == "选择目录" else None,
    )

    assert windows_file_dialog.choose_windows_folder("选择目录") == tmp_path.resolve()


def test_modern_folder_picker_preserves_cancel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(windows_file_dialog.os, "name", "nt")
    monkeypatch.setattr(
        windows_file_dialog, "_show_windows_folder_dialog", lambda _title: None
    )

    assert windows_file_dialog.choose_windows_folder("选择目录") is None


def test_modern_folder_picker_rejects_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    selected = tmp_path / "not-a-folder.txt"
    selected.write_text("test", encoding="utf-8")
    monkeypatch.setattr(windows_file_dialog.os, "name", "nt")
    monkeypatch.setattr(
        windows_file_dialog, "_show_windows_folder_dialog", lambda _title: str(selected)
    )

    with pytest.raises(windows_file_dialog.WindowsFileDialogError, match="普通文件夹"):
        windows_file_dialog.choose_windows_folder("选择目录")


def test_client_native_folder_entries_share_modern_picker() -> None:
    workspace_source = Path(workspace_session.__file__).read_text(encoding="utf-8")
    deployment_source = Path(deployment_bundles.__file__).read_text(encoding="utf-8")
    dialog_source = Path(windows_file_dialog.__file__).read_text(encoding="utf-8")

    assert "choose_windows_folder" in workspace_source
    assert "choose_windows_folder" in deployment_source
    assert "SHBrowseForFolderW" not in workspace_source
    assert "tkinter" not in deployment_source
    assert "fos_pickfolders" in dialog_source
    assert "clsid_file_open_dialog" in dialog_source


def test_workspace_folder_entry_preserves_selected_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        workspace_session, "choose_windows_folder", lambda _title: tmp_path.resolve()
    )

    assert workspace_session.choose_workspace_folder() == str(tmp_path.resolve())


def test_deployment_folder_entry_preserves_cancel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        deployment_bundles, "choose_windows_folder", lambda _title: None
    )

    assert deployment_bundles.choose_output_directory() is None
