@echo off
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
if not exist ".venv\Scripts\python.exe" (
  echo Python environment not found. Run installation first.
  pause
  exit /b 1
)
start "ParserPon4ik Panel" /min ".venv\Scripts\python.exe" control_panel.py
