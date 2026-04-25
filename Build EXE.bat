@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  py -3 -m venv .venv
)

".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements.txt pyinstaller
".venv\Scripts\python.exe" -m PyInstaller --noconsole --onefile --name "External Monitor Brightness" brightness_app.py

echo.
echo Built: %cd%\dist\External Monitor Brightness.exe
pause

endlocal
