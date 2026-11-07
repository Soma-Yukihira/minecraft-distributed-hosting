# PyInstaller spec of the Windows client (one folder, no console). Built by build.ps1.
# -*- mode: python -*-
from pathlib import Path

ROOT = Path(SPECPATH).parents[1]
NAME = "MinecraftDistributedHosting"

a = Analysis(
    [str(ROOT / "deployment" / "windows" / "launcher.py")],
    pathex=[str(ROOT / "shared"), str(ROOT / "client")],
    hiddenimports=["mdh_client.gui.app"],
    # The control plane, test tools and Qt modules the client never uses stay out of the package.
    excludes=["mdh_server", "fastapi", "uvicorn", "argon2", "pytest", "hypothesis", "tkinter",
              "PyInstaller", "pip_audit",
              "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebChannel",
              "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuickWidgets", "PySide6.Qt3DCore",
              "PySide6.QtMultimedia", "PySide6.QtPdf", "PySide6.QtCharts", "PySide6.QtDataVisualization",
              "PySide6.QtBluetooth", "PySide6.QtPositioning", "PySide6.QtSql", "PySide6.QtTest"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=NAME,
    console=False,
    icon=str(ROOT / "build" / "app.ico"),
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name=NAME, upx=False)
