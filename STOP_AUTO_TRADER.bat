@echo off
cd /d "%~dp0"
title Auto Trader - STOP

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
set PYTHONPATH=%~dp0

for %%M in (demo paper live) do (
  echo --- %%M ---
  "%PY%" -m crypto_ls_research.execution.auto_ctl --stop --mode %%M
)

echo.
pause
