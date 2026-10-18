@echo off
setlocal
cd /d "%~dp0"
if exist "Optimus.exe" (
  start "" "%~dp0Optimus.exe"
  exit /b 0
)
if not exist ".venv\Scripts\pythonw.exe" (
  echo Install the source dependencies first using Install-Source.bat.
  pause
  exit /b 1
)
start "" "%~dp0.venv\Scripts\pythonw.exe" -m optimus
