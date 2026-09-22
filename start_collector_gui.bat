@echo off
setlocal

rem One-click launcher for the local collector Web GUI.
set "ROOT=%~dp0"
set "APP=%ROOT%collector_webapp"
set "PYTHON=python"

where python >nul 2>nul
if errorlevel 1 (
    echo Python was not found in PATH.
    echo Please install Python 3.10+ and enable the Add Python to PATH option.
    pause
    exit /b 1
)

rem Reuse an existing service when the GUI is already running.
powershell -NoProfile -ExecutionPolicy Bypass -Command "$c=Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue; if($c){exit 0}else{exit 1}"
if errorlevel 1 (
    powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%PYTHON%' -ArgumentList 'app.py' -WorkingDirectory '%APP%' -WindowStyle Minimized"
    powershell -NoProfile -Command "Start-Sleep -Seconds 3"
)

start "" "http://127.0.0.1:8765/"
endlocal
