@echo off
setlocal
set "OPTIMUS_CONTINUE_SCRIPT=%~dp0Continue-OptimusUpdates.ps1"
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%OPTIMUS_CONTINUE_SCRIPT%" %*
exit /b %ERRORLEVEL%
