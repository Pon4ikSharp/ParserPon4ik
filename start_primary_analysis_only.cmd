@echo off
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "APP_PYTHON=%~dp0.venv\Scripts\python.exe"
if not exist "%APP_PYTHON%" (
  echo ПОМИЛКА: не знайдено %APP_PYTHON%
  echo Використовувати системний Python небезпечно, запуск скасовано.
  pause
  exit /b 1
)
"%APP_PYTHON%" tools\primary_analysis_mode.py start
pause
