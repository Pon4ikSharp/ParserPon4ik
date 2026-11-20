@echo off
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
if not exist ".venv\Scripts\python.exe" (
  echo Python environment not found. Run installation first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" tools\parser_only_mode.py start
if errorlevel 1 pause
