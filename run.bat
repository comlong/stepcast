@echo off
chcp 65001 >nul
cd /d "%~dp0"

if exist .venv\Scripts\python.exe (
  set "PY=.venv\Scripts\python.exe"
) else (
  set "PY=python"
)

%PY% app.py %*
if errorlevel 1 pause
