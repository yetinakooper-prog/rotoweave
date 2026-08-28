from __future__ import annotations

import os
from pathlib import Path
from typing import Protocol


class ModelPicker(Protocol):
    def choose_folder(self) -> Path | None: ...

    def choose_file(self, display_name: str) -> Path | None: ...


class WindowsNativeModelPicker:
    """Host-only Win32 file/folder dialogs; selected files stay in place."""

    def choose_folder(self) -> Path | None:
        if os.name != "nt":
            raise RuntimeError("Windows 原生模型文件夹选择器在当前环境不可用。")
        import ctypes
        from ctypes import wintypes

        class BrowseInfo(ctypes.Structure):
            _fields_ = [
                ("hwndOwner", wintypes.HWND),
                ("pidlRoot", ctypes.c_void_p),
                ("pszDisplayName", wintypes.LPWSTR),
                ("lpszTitle", wintypes.LPCWSTR),
                ("ulFlags", wintypes.UINT),
                ("lpfn", ctypes.c_void_p),
                ("lParam", wintypes.LPARAM),
                ("iImage", ctypes.c_int),
            ]

        ole32 = ctypes.WinDLL("ole32", use_last_error=True)
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
        ole32.CoInitializeEx.restype = ctypes.c_long
        result = ole32.CoInitializeEx(None, 0x2)
        initialized = result in {0, 1}
        if not initialized and result != -2147417850:
            raise RuntimeError("无法初始化 Windows 模型文件夹选择器。")
        ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
        ole32.CoTaskMemFree.restype = None
        display_name = ctypes.create_unicode_buffer(32768)
        browse = BrowseInfo(
            None,
            None,
            ctypes.cast(display_name, wintypes.LPWSTR),
            "选择 RotoWeave 模型文件夹",
            0x0001 | 0x0010 | 0x0040,
            None,
            0,
            0,
        )
        shell32.SHBrowseForFolderW.argtypes = [ctypes.POINTER(BrowseInfo)]
        shell32.SHBrowseForFolderW.restype = ctypes.c_void_p
        item_id_list = None
        try:
            item_id_list = shell32.SHBrowseForFolderW(ctypes.byref(browse))
            if not item_id_list:
                return None
            selected = ctypes.create_unicode_buffer(32768)
            get_path = getattr(shell32, "SHGetPathFromIDListEx", None)
            if get_path is not None:
                get_path.argtypes = [ctypes.c_void_p, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
                get_path.restype = wintypes.BOOL
                resolved = bool(get_path(item_id_list, selected, len(selected), 0))
            else:
                shell32.SHGetPathFromIDListW.argtypes = [ctypes.c_void_p, wintypes.LPWSTR]
                shell32.SHGetPathFromIDListW.restype = wintypes.BOOL
                resolved = bool(shell32.SHGetPathFromIDListW(item_id_list, selected))
            if not resolved or not selected.value:
                raise RuntimeError("Windows 未返回有效的模型文件夹。")
            return Path(selected.value)
        finally:
            if item_id_list:
                ole32.CoTaskMemFree(item_id_list)
            if initialized:
                ole32.CoUninitialize()

    def choose_file(self, display_name: str) -> Path | None:
        if os.name != "nt":
            raise RuntimeError("Windows 原生模型文件选择器在当前环境不可用。")
        import ctypes
        from ctypes import wintypes

        class OpenFileName(ctypes.Structure):
            _fields_ = [
                ("lStructSize", wintypes.DWORD), ("hwndOwner", wintypes.HWND),
                ("hInstance", wintypes.HINSTANCE), ("lpstrFilter", wintypes.LPCWSTR),
                ("lpstrCustomFilter", wintypes.LPWSTR), ("nMaxCustFilter", wintypes.DWORD),
                ("nFilterIndex", wintypes.DWORD), ("lpstrFile", wintypes.LPWSTR),
                ("nMaxFile", wintypes.DWORD), ("lpstrFileTitle", wintypes.LPWSTR),
                ("nMaxFileTitle", wintypes.DWORD), ("lpstrInitialDir", wintypes.LPCWSTR),
                ("lpstrTitle", wintypes.LPCWSTR), ("Flags", wintypes.DWORD),
                ("nFileOffset", wintypes.WORD), ("nFileExtension", wintypes.WORD),
                ("lpstrDefExt", wintypes.LPCWSTR), ("lCustData", wintypes.LPARAM),
                ("lpfnHook", ctypes.c_void_p), ("lpTemplateName", wintypes.LPCWSTR),
                ("pvReserved", ctypes.c_void_p), ("dwReserved", wintypes.DWORD),
                ("FlagsEx", wintypes.DWORD),
            ]

        buffer = ctypes.create_unicode_buffer(32768)
        dialog = OpenFileName()
        dialog.lStructSize = ctypes.sizeof(OpenFileName)
        dialog.lpstrFilter = "模型文件 (*.pt;*.pth;*.safetensors;*.ckpt;*.bin)\0*.pt;*.pth;*.safetensors;*.ckpt;*.bin\0所有文件\0*.*\0\0"
        dialog.lpstrFile = ctypes.cast(buffer, wintypes.LPWSTR)
        dialog.nMaxFile = len(buffer)
        dialog.lpstrTitle = f"选择 {display_name} 模型文件"
        dialog.Flags = 0x00001000 | 0x00000800 | 0x00000008
        comdlg32 = ctypes.WinDLL("comdlg32", use_last_error=True)
        comdlg32.GetOpenFileNameW.argtypes = [ctypes.POINTER(OpenFileName)]
        comdlg32.GetOpenFileNameW.restype = wintypes.BOOL
        if not comdlg32.GetOpenFileNameW(ctypes.byref(dialog)):
            error = int(comdlg32.CommDlgExtendedError())
            if error == 0:
                return None
            raise RuntimeError(f"Windows 模型文件选择器失败（{error}）。")
        return Path(buffer.value)
