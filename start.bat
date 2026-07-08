@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\start.ps1"
if errorlevel 1 (
  echo.
  echo [ERROR] AutoGameTest failed to start.
  echo If data\logs\startup.log exists, please send it to the developer.
  echo.
  pause
  exit /b 1
)

echo.
echo [AutoGameTest] Started. You can close this window.
ping -n 4 127.0.0.1 >nul
exit /b 0
