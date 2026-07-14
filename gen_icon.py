#!/usr/bin/env python3
"""Generate app.ico for the build (used by GorgonChatTranslator.spec and by
build_exe.bat / the GitHub Actions workflow).

Uses the vendored appicon.py identicon generator, writing the icon into this
folder so the build is fully self-contained (no dependency on c:\\projects).
"""
import os
import shutil
import appicon

HERE = os.path.dirname(os.path.abspath(__file__))
appicon.ICON_DIR = HERE                       # write next to this script
src = appicon.make_icon("Gorgon Chat Translator")
dst = os.path.join(HERE, "app.ico")
shutil.copyfile(src, dst)
print("wrote", dst)
