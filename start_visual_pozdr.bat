@echo off
setlocal

cd /d "%~dp0"
set "PYTHON=.venv\Scripts\python.exe"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

echo ========================================
echo Visual Pozdr - event launcher
echo ========================================
echo.

if not exist "%PYTHON%" (
  echo ERROR: Python virtual environment was not found.
  echo Run these commands first:
  echo   py -m venv .venv
  echo   .venv\Scripts\python.exe -m pip install -r requirements.txt
  echo.
  pause
  exit /b 1
)

if not exist ".env" (
  echo ERROR: .env file was not found.
  echo Create it from the example:
  echo   copy .env.example .env
  echo Then fill TELEGRAM_BOT_TOKEN and TELEGRAM_BOT_USERNAME.
  echo.
  pause
  exit /b 1
)

echo Running preflight checks...
"%PYTHON%" "scripts\preflight_check.py"
if errorlevel 1 (
  echo.
  pause
  exit /b 1
)

set "APP_HOST=127.0.0.1"
set "APP_PORT=8000"
for /f "usebackq tokens=1,* delims==" %%A in (`findstr /b "HOST=" .env`) do set "APP_HOST=%%B"
for /f "usebackq tokens=1,* delims==" %%A in (`findstr /b "PORT=" .env`) do set "APP_PORT=%%B"

echo.
echo Starting local server...
echo Admin panel:  http://%APP_HOST%:%APP_PORT%/admin
echo Screen page:  http://%APP_HOST%:%APP_PORT%/screen
echo Health check: http://%APP_HOST%:%APP_PORT%/api/health
echo.
echo Keep this window open until the event is finished.
echo Press Ctrl+C in this window to stop the server.
echo.

start "" cmd /c "timeout /t 3 /nobreak >nul && start http://%APP_HOST%:%APP_PORT%/admin"
"%PYTHON%" "run_server.py"

echo.
echo Server stopped.
pause
