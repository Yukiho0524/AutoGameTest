@echo off
chcp 65001 >nul
cd /d "%~dp0"

where py >nul 2>nul
if %errorlevel%==0 (
  set "PY=py -3"
) else (
  where python >nul 2>nul
  if errorlevel 1 (
    echo [ERROR] 找不到 Python 3。請先安裝 Python 3.11 或更新版本。
    echo https://www.python.org/downloads/
    pause
    exit /b 1
  )
  set "PY=python"
)

echo [AutoGameTest] Running environment check...
%PY% tools\doctor.py
if errorlevel 1 (
  echo.
  echo [ERROR] 環境檢查未通過，請依照上方提示修正後再執行。
  pause
  exit /b 1
)

echo.
echo [AutoGameTest] Starting control panel...
echo Open http://127.0.0.1:8777 in your browser.
%PY% server.py
pause

