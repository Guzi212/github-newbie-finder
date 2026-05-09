@echo off
setlocal EnableExtensions

rem Stops only this project's backend and Streamlit processes.
rem Keep this file ASCII-only so cmd.exe can parse it reliably on any locale.

cd /d "%~dp0.."

echo [stop] stopping backend processes...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$root = [regex]::Escape((Resolve-Path '.').Path); $p = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -and $_.CommandLine -match 'uvicorn\s+app\.main:app' -and $_.CommandLine -match $root }; if ($p) { $p | ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop; Write-Host ('       killed PID ' + $_.ProcessId) } catch {} } } else { Write-Host '       none found' }"

echo [stop] stopping Streamlit processes...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$root = [regex]::Escape((Resolve-Path '.').Path); $p = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -and $_.CommandLine -match 'streamlit\s+run\s+streamlit_app\.py' -and $_.CommandLine -match $root }; if ($p) { $p | ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop; Write-Host ('       killed PID ' + $_.ProcessId) } catch {} } } else { Write-Host '       none found' }"

echo [stop] checking ports 8000 and 8501...
powershell -NoProfile -ExecutionPolicy Bypass -Command "foreach ($port in 8000,8501) { $conns = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue; if ($conns) { foreach ($c in $conns) { try { Stop-Process -Id $c.OwningProcess -Force -ErrorAction Stop; Write-Host ('       port ' + $port + ' was still busy; killed PID ' + $c.OwningProcess) } catch {} } } else { Write-Host ('       port ' + $port + ' is free') } }"

echo.
echo [done] Finished.
echo.
pause
endlocal
