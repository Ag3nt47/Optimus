@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Run Install-Source.bat first.
  exit /b 1
)
".venv\Scripts\python.exe" -m pytest -q
if errorlevel 1 exit /b 1
".venv\Scripts\python.exe" tools\build_native.py --expected-target Windows-x64
if errorlevel 1 exit /b 1

