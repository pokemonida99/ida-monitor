@echo off
rem One-click launcher for ida-monitor
cd /d "%~dp0"

rem If the server is already running on 8765, just open the browser
powershell -NoProfile -Command "try { (New-Object Net.Sockets.TcpClient('127.0.0.1',8765)).Close(); exit 0 } catch { exit 1 }" >nul 2>&1
if %errorlevel%==0 (
  start "" http://127.0.0.1:8765
  exit /b
)

rem Start the server minimized, wait a moment, then open the browser
start "ida-monitor" /min uv run --no-project python server.py
timeout /t 3 /nobreak >nul
start "" http://127.0.0.1:8765
