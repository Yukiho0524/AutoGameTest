@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

set "PY_CMD="

py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)" >nul 2>nul
if %errorlevel%==0 (
  set "PY_CMD=py -3"
) else (
  python -c "import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)" >nul 2>nul
  if %errorlevel%==0 (
    set "PY_CMD=python"
  )
)

if not defined PY_CMD (
  echo [ERROR] 找不到可用的 Python 3.10+。
  echo.
  echo 請安裝 Python 3.10 或更新版本，安裝時勾選 Add python.exe to PATH。
  echo https://www.python.org/downloads/windows/
  echo.
  pause
  exit /b 1
)

%PY_CMD% tools\launch.py
if errorlevel 1 (
  echo.
  echo [ERROR] AutoGameTest 啟動失敗。
  echo 請把 data\logs\startup.log 的內容貼給開發者。
  echo.
  pause
  exit /b 1
)

echo.
echo [AutoGameTest] 已啟動。可以關閉這個視窗。
timeout /t 3 >nul
exit /b 0
