@echo off
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" run.py --gui %*
) else (
  echo Run setup.ps1 once to create this project's Python environment.
  pause
)
