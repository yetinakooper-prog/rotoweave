from __future__ import annotations

import os
import threading
import uuid
from pathlib import Path


_DIALOG_LOCK = threading.Lock()


class WindowsFileDialogError(RuntimeError):
    """The modern Windows shell dialog could not return a usable folder."""


def _hresult_hex(value: int) -> str:
    return f"0x{value & 0xFFFFFFFF:08X}"


def _raise_for_hresult(value: int, action: str) -> None:
    if value < 0:
        raise WindowsFileDialogError(f"{action}（HRESULT {_hresult_hex(value)}）。")


def _show_windows_folder_dialog(title: str) -> str | None:
    import ctypes
    from ctypes import wintypes

    class Guid(ctypes.Structure):
        _fields_ = [
            ("Data1", ctypes.c_uint32),
            ("Data2", ctypes.c_uint16),
            ("Data3", ctypes.c_uint16),
            ("Data4", ctypes.c_ubyte * 8),
        ]

        @classmethod
        def parse(cls, value: str) -> Guid:
            return cls.from_buffer_copy(uuid.UUID(value).bytes_le)

    def method(pointer, index, result_type, *argument_types):
        vtable = ctypes.cast(
            pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))
        ).contents
        return ctypes.WINFUNCTYPE(
            result_type, ctypes.c_void_p, *argument_types
        )(vtable[index])

    coinit_apartment_threaded = 0x2
    rpc_e_changed_mode = -2147417850
    error_cancelled = -2147023673
    clsctx_inproc_server = 0x1
    fos_nochangedir = 0x00000008
    fos_pickfolders = 0x00000020
    fos_forcefilesystem = 0x00000040
    fos_pathmustexist = 0x00000800
    sigdn_filesyspath = 0x80058000

    clsid_file_open_dialog = Guid.parse("DC1C5A9C-E88A-4DDE-A5A1-60F82A20AEF7")
    iid_file_open_dialog = Guid.parse("D57C7288-D4AD-4768-BE02-9D969532D960")

    ole32 = ctypes.WinDLL("ole32", use_last_error=True)
    ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    ole32.CoInitializeEx.restype = ctypes.c_long
    ole32.CoUninitialize.argtypes = []
    ole32.CoUninitialize.restype = None
    ole32.CoCreateInstance.argtypes = [
        ctypes.POINTER(Guid),
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(Guid),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    ole32.CoCreateInstance.restype = ctypes.c_long
    ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    ole32.CoTaskMemFree.restype = None

    initialized = False
    dialog = ctypes.c_void_p()
    item = ctypes.c_void_p()
    display_name = ctypes.c_void_p()
    release_dialog = None
    release_item = None
    result = int(ole32.CoInitializeEx(None, coinit_apartment_threaded))
    if result in {0, 1}:
        initialized = True
    elif result == rpc_e_changed_mode:
        raise WindowsFileDialogError(
            "当前线程的 Windows 组件模式不支持新版文件浏览器。"
        )
    else:
        _raise_for_hresult(result, "无法初始化新版 Windows 文件浏览器")

    try:
        result = int(
            ole32.CoCreateInstance(
                ctypes.byref(clsid_file_open_dialog),
                None,
                clsctx_inproc_server,
                ctypes.byref(iid_file_open_dialog),
                ctypes.byref(dialog),
            )
        )
        _raise_for_hresult(result, "无法创建新版 Windows 文件浏览器")
        if not dialog.value:
            raise WindowsFileDialogError("Windows 未创建可用的新版文件浏览器。")

        release_dialog = method(dialog, 2, wintypes.ULONG)
        show = method(dialog, 3, ctypes.c_long, wintypes.HWND)
        set_options = method(dialog, 9, ctypes.c_long, wintypes.DWORD)
        get_options = method(
            dialog, 10, ctypes.c_long, ctypes.POINTER(wintypes.DWORD)
        )
        set_title = method(dialog, 17, ctypes.c_long, wintypes.LPCWSTR)
        get_result = method(
            dialog, 20, ctypes.c_long, ctypes.POINTER(ctypes.c_void_p)
        )

        options = wintypes.DWORD()
        _raise_for_hresult(
            int(get_options(dialog, ctypes.byref(options))),
            "无法读取文件浏览器选项",
        )
        options.value |= (
            fos_nochangedir
            | fos_pickfolders
            | fos_forcefilesystem
            | fos_pathmustexist
        )
        _raise_for_hresult(int(set_options(dialog, options)), "无法设置文件夹选择模式")
        _raise_for_hresult(int(set_title(dialog, title)), "无法设置文件浏览器标题")

        result = int(show(dialog, None))
        if result == error_cancelled:
            return None
        _raise_for_hresult(result, "无法打开新版 Windows 文件浏览器")
        _raise_for_hresult(int(get_result(dialog, ctypes.byref(item))), "无法读取所选文件夹")
        if not item.value:
            raise WindowsFileDialogError("Windows 未返回所选文件夹。")

        release_item = method(item, 2, wintypes.ULONG)
        get_display_name = method(
            item,
            5,
            ctypes.c_long,
            ctypes.c_uint,
            ctypes.POINTER(ctypes.c_void_p),
        )
        _raise_for_hresult(
            int(get_display_name(item, sigdn_filesyspath, ctypes.byref(display_name))),
            "Windows 未返回所选文件夹路径",
        )
        if not display_name.value:
            raise WindowsFileDialogError("Windows 未返回所选文件夹路径。")
        return ctypes.wstring_at(display_name.value)
    finally:
        if display_name.value:
            ole32.CoTaskMemFree(display_name)
        if item.value and release_item is not None:
            release_item(item)
        if dialog.value and release_dialog is not None:
            release_dialog(dialog)
        if initialized:
            ole32.CoUninitialize()


def choose_windows_folder(title: str) -> Path | None:
    """Choose one existing folder through the modern Windows Explorer dialog."""

    if os.name != "nt":
        raise WindowsFileDialogError("新版文件浏览器仅支持 Windows。")
    with _DIALOG_LOCK:
        selected = _show_windows_folder_dialog(title)
    if not selected:
        return None
    try:
        path = Path(selected).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise WindowsFileDialogError("Windows 返回的文件夹路径无效。") from exc
    if not path.is_dir() or path.is_symlink():
        raise WindowsFileDialogError("请选择本机普通文件夹。")
    return path
