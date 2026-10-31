# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller recipe for the single windowed/interactive-console executable."""
import os
import sys
from pathlib import Path

project_root = Path(SPECPATH).resolve()
module_paths = [
    project_root / "Extractor",
    project_root / "TruckLib.Core",
    project_root / "TruckLib.Models",
    project_root / "TruckLib.Sii",
    project_root / "TruckLib.HashFs",
    project_root / "GisDeflate",
]

analysis = Analysis(
    [str(project_root / "Extractor" / "extractor.py")],
    pathex=[str(path) for path in module_paths],
    binaries=[],
    datas=[
        (str(project_root / "Extractor" / "DeepStartPaths.txt.gz"), "."),
        (str(project_root / "assets" / "app-icon.ico"), "assets"),
        (str(project_root / "assets" / "app-icon-48.png"), "assets"),
        (str(project_root / "assets" / "app-icon-64.png"), "assets"),
    ],
    hiddenimports=[
        "aes", "cityhash", "file_types", "gdeflate_tables", "gisdeflate",
        "gui", "plugin_runtime", "tkinter", "tkinter.filedialog", "tkinter.messagebox",
        "texture", "trucklib_core", "trucklib_hashfs", "trucklib_models",
        "trucklib_sii", "utils", "zip_reader",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=1,
)

pyz = PYZ(analysis.pure)

exe = EXE(
    pyz,
    analysis.scripts,
    analysis.binaries,
    analysis.datas,
    [],
    name="UniversalSCSExtractor",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    # Linux keeps stdout/stderr attached for normal CLI use.  Windows and
    # macOS use the application's GUI/interactive-console handling instead.
    console=sys.platform.startswith("linux"),
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=([os.environ["UNIVERSAL_SCS_BUILD_ICON"]]
          if os.environ.get("UNIVERSAL_SCS_BUILD_ICON") else
          ([str(project_root / "assets" / "app-icon.ico")]
           if sys.platform == "win32" else [])),
)

if sys.platform == "darwin":
    app = BUNDLE(
        exe,
        name="Universal SCS Extractor.app",
        icon=os.environ.get("UNIVERSAL_SCS_BUILD_ICON"),
        bundle_identifier="org.universalscs.extractor",
    )
