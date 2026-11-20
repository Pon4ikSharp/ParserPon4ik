@echo off
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "APP_PYTHON=%~dp0.venv\Scripts\python.exe"
if not exist "%APP_PYTHON%" (
  echo ПОМИЛКА: не знайдено %APP_PYTHON%
  pause
  exit /b 1
)
"%APP_PYTHON%" tools\primary_analysis_stats.py
if errorlevel 1 (
  echo.
  echo Не вдалося отримати статистику. Перевірте runtime\logs\collector.log та DATABASE_URL у .env.
)
echo.
pause
