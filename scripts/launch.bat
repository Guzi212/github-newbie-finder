@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

REM ============================================================
REM 一键启动「GitHub 小白检索器」: backend (8000) + Streamlit (8501)
REM 双击即可。重复双击安全，不会重启已在跑的服务。
REM ============================================================

cd /d "%~dp0.."

if not exist ".venv\Scripts\python.exe" (
  echo [error] 找不到 .venv，请先在项目根目录依次运行:
  echo           python -m venv .venv
  echo           .venv\Scripts\activate
  echo           pip install -r requirements.txt
  echo           python -m app.cli init-db
  pause
  exit /b 1
)

if not exist logs mkdir logs

REM ---- backend (FastAPI :8000) ----
call :PortBusy 8000
if !errorlevel! equ 0 (
  echo [backend ] 已在 :8000 运行，跳过
) else (
  echo [backend ] 启动中...  日志: logs\backend.log
  start "GitHub-Finder Backend" /MIN cmd /c ".venv\Scripts\uvicorn.exe app.main:app --host 127.0.0.1 --port 8000 --reload >> logs\backend.log 2>&1"
)

REM ---- frontend (Streamlit :8501) ----
call :PortBusy 8501
if !errorlevel! equ 0 (
  echo [streamlit] 已在 :8501 运行，跳过
) else (
  echo [streamlit] 启动中...  日志: logs\streamlit.log
  start "GitHub-Finder Streamlit" /MIN cmd /c ".venv\Scripts\streamlit.exe run streamlit_app.py --server.port 8501 --server.headless true >> logs\streamlit.log 2>&1"
)

REM ---- 等 Streamlit 就绪 (最多 30s) ----
echo [browser ] 等待就绪...
set /a tries=0
:wait
set /a tries+=1
if !tries! gtr 30 goto :open
powershell -NoProfile -Command "try { Invoke-WebRequest -UseBasicParsing -Uri http://127.0.0.1:8501 -TimeoutSec 1 | Out-Null; exit 0 } catch { exit 1 }" >nul 2>&1
if !errorlevel! neq 0 (
  timeout /t 1 /nobreak >nul
  goto :wait
)

:open
start "" "http://localhost:8501"
echo.
echo [done] 已打开 http://localhost:8501
echo        可关闭此窗口；服务在两个最小化的 cmd 窗口里继续运行。
echo        停止: 双击 scripts\stop.bat
echo.
pause
endlocal
exit /b 0

:PortBusy
powershell -NoProfile -Command "if ((Get-NetTCPConnection -LocalPort %1 -State Listen -ErrorAction SilentlyContinue).Count -gt 0) { exit 0 } else { exit 1 }" >nul 2>&1
exit /b %errorlevel%
