@echo off
setlocal EnableExtensions EnableDelayedExpansion

rem One-click Windows launcher for backend (:8000) and Streamlit (:8501).
rem Keep this file ASCII-only so cmd.exe can parse it reliably on any locale.

cd /d "%~dp0.."

set "ROOT=%CD%"
set "PY=%ROOT%\.venv\Scripts\python.exe"
set "SETUP_MARKER=%ROOT%\.venv\.github-newbie-finder-ready"
set "BACKEND_LOG=logs\backend.log"
set "STREAMLIT_LOG=logs\streamlit.log"

if not exist "logs" mkdir "logs"

call :EnsureEnv
if !errorlevel! neq 0 (
  pause
  exit /b 1
)

call :PortBusy 8000
if !errorlevel! equ 0 (
  echo [backend ] already listening on port 8000. Skipping.
) else (
  echo [backend ] starting... log: %BACKEND_LOG%
  start "GitHub-Finder Backend" /MIN cmd /c ""%PY%" -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload >> "%BACKEND_LOG%" 2>&1"
)

call :PortBusy 8501
if !errorlevel! equ 0 (
  echo [streamlit] already listening on port 8501. Skipping.
) else (
  echo [streamlit] starting... log: %STREAMLIT_LOG%
  start "GitHub-Finder Streamlit" /MIN cmd /c ""%PY%" -m streamlit run streamlit_app.py --server.port 8501 --server.headless true >> "%STREAMLIT_LOG%" 2>&1"
)

echo [browser ] waiting for Streamlit...
set /a tries=0
:wait
set /a tries+=1
if !tries! gtr 30 goto :open
powershell -NoProfile -ExecutionPolicy Bypass -Command "try { Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:8501' -TimeoutSec 1 | Out-Null; exit 0 } catch { exit 1 }" >nul 2>&1
if !errorlevel! neq 0 (
  timeout /t 1 /nobreak >nul
  goto :wait
)

:open
start "" "http://localhost:8501"
echo.
echo [done] Opened http://localhost:8501
echo        Services continue in the minimized cmd windows.
echo        To stop them, double-click scripts\stop.bat.
echo.
pause
endlocal
exit /b 0

:PortBusy
powershell -NoProfile -ExecutionPolicy Bypass -Command "$c = Get-NetTCPConnection -LocalPort %1 -State Listen -ErrorAction SilentlyContinue; if ($c) { exit 0 } else { exit 1 }" >nul 2>&1
exit /b %errorlevel%

:EnsureEnv
set "BOOTSTRAP_PY="
if exist "%PY%" goto :InstallDeps

where python >nul 2>&1
if !errorlevel! equ 0 set "BOOTSTRAP_PY=python"

if not defined BOOTSTRAP_PY (
  where py >nul 2>&1
  if !errorlevel! equ 0 set "BOOTSTRAP_PY=py -3"
)

if not defined BOOTSTRAP_PY (
  echo [error] Python was not found.
  echo.
  echo Install Python 3.10 or newer from https://www.python.org/downloads/
  echo During setup, check "Add python.exe to PATH".
  echo Then double-click this file again.
  exit /b 1
)

echo [setup  ] creating .venv...
%BOOTSTRAP_PY% -m venv ".venv"
if !errorlevel! neq 0 (
  echo [error] Failed to create .venv.
  exit /b 1
)

:InstallDeps
if not exist "%PY%" (
  echo [error] Missing %PY%.
  exit /b 1
)

if not exist ".env" if exist ".env.example" (
  echo [setup  ] creating .env from .env.example...
  copy /Y ".env.example" ".env" >nul
)

if exist "%SETUP_MARKER%" exit /b 0

echo [setup  ] installing requirements...
"%PY%" -m pip install -r requirements.txt
if !errorlevel! neq 0 (
  echo [error] Dependency installation failed.
  exit /b 1
)

echo [setup  ] initializing database...
"%PY%" -m app.cli init-db
if !errorlevel! neq 0 (
  echo [error] Database initialization failed.
  exit /b 1
)

echo ready>"%SETUP_MARKER%"
exit /b 0
