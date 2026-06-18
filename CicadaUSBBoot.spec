# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for Cicada USB Boot Tool (onefile, windowed)."""

from pathlib import Path

block_cipher = None
project_dir = Path(SPECPATH)

datas: list[tuple[str, str]] = []

img256 = project_dir / "img" / "256"
if img256.is_dir():
    datas.append((str(img256), "img/256"))

for icon_name in ("cicada_icon.ico", "icon.ico", "cicada_icon.png"):
    icon_file = project_dir / icon_name
    if icon_file.is_file():
        datas.append((str(icon_file), "."))

exe_icon: str | None = None
for icon_name in ("cicada_icon.ico", "icon.ico"):
    icon_file = project_dir / icon_name
    if icon_file.is_file():
        exe_icon = str(icon_file)
        break

a = Analysis(
    ["cicada_usb_tool.py"],
    pathex=[str(project_dir)],
    binaries=[],
    datas=datas,
    hiddenimports=[
        "cicada_usb_tool_frosted",
        "cicada_errors",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="CicadaUSBBoot",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=exe_icon,
)
