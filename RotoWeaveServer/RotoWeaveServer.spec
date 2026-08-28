import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

workspace = Path(SPECPATH).resolve()
contracts_root = workspace.parent / "RotoWeaveContracts"
models_root = workspace.parent / "RotoWeaveModels"
runtime = workspace / "runtime"
server_runtimes = Path(
    os.environ.get("ROTOWEAVE_SERVER_RUNTIMES_STAGE")
    or runtime / "server-runtimes"
).resolve()
if not server_runtimes.is_dir():
    raise RuntimeError("Fixed High/Ultra server runtime staging is missing.")

datas = [
    (str(contracts_root / "product.json"), "."),
    (str(contracts_root / "contracts"), "contracts"),
    (str(workspace / "worker"), "worker-runtime/worker"),
    (str(contracts_root / "contracts"), "worker-runtime/contracts"),
    (str(contracts_root / "product.json"), "worker-runtime"),
    (str(workspace / "README.md"), "."),
    (str(workspace / "server-admin" / "dist"), "server-admin"),
    (str(server_runtimes), "server-runtimes"),
]
hiddenimports = collect_submodules("uvicorn") + collect_submodules("pystray") + [
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
]

analysis = Analysis(
    [str(workspace / "server" / "launcher.py")],
    pathex=[str(workspace)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "notebook"],
    noarchive=False,
    optimize=1,
)
analysis.binaries = [
    item
    for item in analysis.binaries
    if Path(item[0]).name.lower() != "onnxruntime_providers_tensorrt.dll"
    and not item[0].lower().startswith("nvidia\\")
]
pyz = PYZ(analysis.pure)

exe = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="RotoWeave-Server",
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
    name="RotoWeave-Server",
)
