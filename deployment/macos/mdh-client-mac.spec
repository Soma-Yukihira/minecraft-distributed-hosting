# PyInstaller spec of the macOS client (Apple chips): an .app bundle. Built by build.sh, on a Mac.
# -*- mode: python -*-
import os
from pathlib import Path

ROOT = Path(SPECPATH).parents[1]
NAME = "MinecraftDistributedHosting"
VERSION = os.environ["MDH_VERSION"]

a = Analysis(
    [str(ROOT / "deployment" / "windows" / "launcher.py")],
    pathex=[str(ROOT / "shared"), str(ROOT / "client")],
    hiddenimports=["mdh_client.gui.app", "mdh_client.system.watchdog"],
    # Same exclusions as on Windows (deployment/windows/mdh-client.spec).
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
    target_arch="arm64",
    # No Apple Developer account: PyInstaller signs "ad hoc" (required on Apple chips).
    codesign_identity=None,
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name=NAME, upx=False)
app = BUNDLE(
    coll,
    name="Minecraft Distributed Hosting.app",
    icon=str(ROOT / "build" / "app.icns"),
    bundle_identifier="fr.mdh.client",
    version=VERSION,
    info_plist={
        "CFBundleDisplayName": "Minecraft Distributed Hosting",
        "CFBundleShortVersionString": VERSION,
        "CFBundleVersion": VERSION,
        "LSMinimumSystemVersion": "12.0",
        "NSHighResolutionCapable": True,
        "NSHumanReadableCopyright": "Minecraft Distributed Hosting",
    },
)
