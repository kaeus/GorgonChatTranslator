# -*- mode: python ; coding: utf-8 -*-
# Build with:  pyinstaller GorgonChatTranslator.spec
# or just run build_exe.bat.
from PyInstaller.utils.hooks import collect_all

datas, binaries, hiddenimports = [("version.txt", ".")], [], ["appicon"]
# deep_translator and langdetect ship data/submodules that PyInstaller misses
# on its own -- langdetect in particular loads per-language profile files that
# MUST be bundled or detection raises at runtime. collect_all grabs them.
for _pkg in ("langdetect", "deep_translator"):
    d, b, h = collect_all(_pkg)
    datas += d
    binaries += b
    hiddenimports += h

a = Analysis(
    ['chat_translator.py'],
    pathex=['.'],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['torch', 'torchvision', 'cv2', 'scipy', 'numba', 'llvmlite',
              'matplotlib', 'IPython', 'PIL', 'PyQt5', 'PySide2', 'pandas'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='GorgonChatTranslator',
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
    icon=['app.ico'],
)
