@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONDONTWRITEBYTECODE=1
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

rem Stop any previous server instance still holding port 8000 (e.g. from an older folder)
for /f "tokens=5" %%a in ('netstat -aon ^| findstr ":8000 " ^| findstr "LISTENING"') do (
  echo Stopping previous server instance on port 8000 ^(PID %%a^)...
  taskkill /F /PID %%a >nul 2>nul
)

set "VENV_DIR=.venv"
if not exist ".venv\Scripts\python.exe" (
  if exist ".venv\bin\python" (
    set "VENV_DIR=.venv-win"
  )
)

if not exist "%VENV_DIR%\Scripts\python.exe" (
  echo [1/3] Creating virtual environment for Windows...
  py -3 -m venv "%VENV_DIR%" 2>nul || python -m venv "%VENV_DIR%" || goto :nopython
  echo [2/3] Installing dependencies...
  "%VENV_DIR%\Scripts\python.exe" -m pip install -r requirements.txt || goto :fail
  echo [3/3] Installing Chromium for Playwright...
  "%VENV_DIR%\Scripts\python.exe" -m playwright install chromium || goto :fail
)

"%VENV_DIR%\Scripts\python.exe" -c "import cv2, numpy" >nul 2>nul || (
  echo Installing local image analysis dependencies...
  "%VENV_DIR%\Scripts\python.exe" -m pip install opencv-python-headless || goto :fail
)

echo Starting server: http://127.0.0.1:8000  (close this window to stop)
start "" cmd /c "timeout /t 3 >nul & start http://127.0.0.1:8000/?v=%RANDOM%"
"%VENV_DIR%\Scripts\python.exe" -m uvicorn app.main:app --port 8000
pause
exit /b 0

:nopython
echo Python 3.11+ not found. Install it from https://www.python.org/downloads/ and tick "Add python.exe to PATH".
pause
exit /b 1

:fail
echo Installation failed, see the error above.
pause
exit /b 1
