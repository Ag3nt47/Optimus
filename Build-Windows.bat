@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Run Install-Source.bat first.
  exit /b 1
)
".venv\Scripts\python.exe" -m pytest -q
if errorlevel 1 exit /b 1
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean --windowed --onedir --name Optimus --icon src\optimus\assets\optimus.ico --paths src --collect-all optimus --collect-submodules psutil --add-data "LICENSE;." --add-data "NOTICE;." launch.py

if errorlevel 1 exit /b 1
".venv\Scripts\python.exe" tools\package_source.py --source . --destination dist\Optimus\source

if errorlevel 1 exit /b 1
".venv\Scripts\python.exe" tools\package_licenses.py --source . --destination dist\Optimus
