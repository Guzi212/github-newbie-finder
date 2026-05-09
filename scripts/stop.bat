@echo off
chcp 65001 >nul
setlocal

REM ============================================================
REM 双击此文件停止后台服务: backend (uvicorn) + Streamlit。
REM 仅按命令行特征 (uvicorn app.main / streamlit streamlit_app)
REM 匹配本项目进程，不会误杀其它 uvicorn / streamlit。
REM ============================================================

cd /d "%~dp0.."

echo [stop] 关闭 backend (uvicorn app.main) ...
powershell -NoProfile -Command "$p = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'uvicorn.*app\.main' }; if ($p) { $p | ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop; Write-Host ('       killed PID ' + $_.ProcessId) } catch {} } } else { Write-Host '       (未在运行)' }"

echo [stop] 关闭 streamlit ...
powershell -NoProfile -Command "$p = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'streamlit.*streamlit_app' }; if ($p) { $p | ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop; Write-Host ('       killed PID ' + $_.ProcessId) } catch {} } } else { Write-Host '       (未在运行)' }"

REM 兜底: 端口仍占用就强杀
echo [stop] 检查端口...
powershell -NoProfile -Command "foreach ($port in 8000,8501) { $conns = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue; if ($conns) { foreach ($c in $conns) { try { Stop-Process -Id $c.OwningProcess -Force -ErrorAction Stop; Write-Host ('       :' + $port + ' still busy -> killed PID ' + $c.OwningProcess) } catch {} } } }"

echo.
echo [done] 完成。可关闭此窗口。
echo.
pause
endlocal
