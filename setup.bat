@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ============================================
echo   StepCast - setup / an zhuang huan jing
echo ============================================
echo.

python --version >nul 2>&1
if errorlevel 1 (
  echo [X] Python not found. Install Python 3.10+ from https://www.python.org/downloads/
  echo     Remember to check "Add Python to PATH".
  pause
  exit /b 1
)
for /f "delims=" %%v in ('python --version') do echo [1/4] %%v

if not exist .venv (
  echo [2/4] Creating virtual env .venv ...
  python -m venv .venv
) else (
  echo [2/4] Virtual env .venv already exists.
)

echo [3/4] Installing dependencies ^(first run may take a few minutes^) ...
.venv\Scripts\python.exe -m pip install --upgrade pip -q
.venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 (
  echo [X] pip install failed. Check your network / proxy.
  pause
  exit /b 1
)

echo [4/4] Checking ffmpeg ...
where ffmpeg >nul 2>&1
if errorlevel 1 (
  echo     [!] ffmpeg not on PATH - will fall back to the bundled imageio-ffmpeg.
  echo         For best speed install it:  winget install Gyan.FFmpeg
) else (
  echo     [OK] ffmpeg found.
)

echo.
echo Done. Next:
echo   1. Double-click run.bat to start the server.
echo   2. Chrome -^> chrome://extensions -^> Developer mode -^> Load unpacked -^> pick the "extension" folder.
echo.
pause
