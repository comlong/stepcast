@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem Build the Windows version to share with colleagues: dist\StepCast\StepCast.exe and dist\StepCast-<version>-win64.zip

if not exist .venv\Scripts\python.exe (
  echo [X] .venv not found. Run setup.bat first.
  pause
  exit /b 1
)
.venv\Scripts\python.exe -m pip install -q "pyinstaller>=6.10"
.venv\Scripts\python.exe packaging\build_exe.py %*
if errorlevel 1 (
  echo [X] Build failed.
  pause
  exit /b 1
)
echo.
echo Done: dist\StepCast
pause
