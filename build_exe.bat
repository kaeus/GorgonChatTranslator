@echo off
REM Build Gorgon Chat Translator into a single standalone .exe (no Python
REM needed to run it). Output: dist\GorgonChatTranslator.exe
REM Self-contained: uses the vendored appicon.py and a locally generated app.ico
REM (the GitHub Actions workflow runs the same steps).
setlocal
cd /d "%~dp0"

python -m pip install --upgrade pyinstaller --quiet
python -m pip install --upgrade -r requirements.txt --quiet

REM Generate the exe icon (app.ico) from the vendored identicon generator.
python gen_icon.py

REM The .spec uses collect_all() to bundle langdetect's language profiles and
REM deep_translator's data -- do NOT switch to a bare --onefile command line
REM without those, or detection/translation will crash in the packaged exe.
python -m PyInstaller --noconfirm --clean GorgonChatTranslator.spec

echo.
echo Done. Executable is at: %~dp0dist\GorgonChatTranslator.exe
pause
