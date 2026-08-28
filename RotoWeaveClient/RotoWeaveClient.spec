from pathlib import Path

from PyInstaller.utils.hooks import collect_dynamic_libs, collect_submodules

workspace = Path(SPECPATH).resolve()
contracts_root = workspace.parent / "RotoWeaveContracts"
runtime = workspace / "runtime"

datas = [
    (str(contracts_root / "product.json"), "."),
    (str(contracts_root / "contracts"), "contracts"),
    (str(runtime / "frontend"), "frontend"),
    (str(runtime / "tools"), "tools"),
]

hiddenimports = collect_submodules("uvicorn") + collect_submodules("pystray") + [
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
]

cuda_binaries = []
for package in (
    "nvidia.cuda_runtime",
    "nvidia.cublas",
    "nvidia.cudnn",
    "nvidia.cufft",
    "nvidia.curand",
    "nvidia.cuda_nvrtc",
    "nvidia.nvjitlink",
):
    try:
        cuda_binaries += [(source, "cuda") for source, _destination in collect_dynamic_libs(package)]
    except Exception:
        pass

analysis = Analysis(
    [str(workspace / "backend" / "client_launcher.py")],
    pathex=[str(workspace)],
    binaries=cuda_binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "notebook"],
    noarchive=False,
    optimize=1,
)
ffmpeg_runtime_names = {
    path.name.lower() for path in (runtime / "tools" / "ffmpeg" / "bin").glob("*.dll")
}
analysis.binaries = [
    item
    for item in analysis.binaries
    if Path(item[0]).name.lower() != "onnxruntime_providers_tensorrt.dll"
    and not item[0].lower().startswith("nvidia\\")
    and not (
        "/" not in item[0]
        and "\\" not in item[0]
        and Path(item[0]).name.lower() in ffmpeg_runtime_names
    )
]
pyz = PYZ(analysis.pure)

exe = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="RotoWeave-Client",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

collection = COLLECT(
    exe,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="RotoWeave-Client",
)
