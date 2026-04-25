@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  py -3 -m venv .venv
)

".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements.txt
".venv\Scripts\python.exe" brightness_app.py

endlocal
