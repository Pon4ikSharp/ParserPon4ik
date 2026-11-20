@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0recover_remote_control.ps1" %*
exit /b %errorlevel%
