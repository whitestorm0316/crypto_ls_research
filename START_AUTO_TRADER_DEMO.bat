@echo off
cd /d "%~dp0"
title Auto Trader - DEMO - daily, follows backtest grid

set "PY=C:\Users\50651\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
if not exist "%PY%" (
  echo [ERROR] python not found:
  echo   %PY%
  pause
  exit /b 1
)

chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8:replace
set PYTHONUNBUFFERED=1
set PYTHONPATH=%~dp0

"%PY%" "%~dp0scripts\auto_demo.py" %*

echo.
echo [exit code %ERRORLEVEL%]
pause
