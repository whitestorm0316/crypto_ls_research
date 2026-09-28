@echo off
REM ============================================================================
REM  参数中台 · 启动（Windows 双击）
REM ============================================================================
REM  为什么需要这个脚本，而不是在别处 `python webapp/server.py 8790`
REM  ---------------------------------------------------------------------------
REM  这不是为了"方便"，是为了让**页面上的按钮能用**。
REM
REM  参数中台里有一个「启动自动任务」按钮，它会 spawn 一个常驻守护进程。
REM  如果 server.py 本身是从一个短命 shell（IDE 任务、工具调用、sandbox 包装）
REM  里起出来的，那么它 spawn 的守护进程属于同一条进程树，会在启动期
REM  （增量刷新那几十秒）被**静默回收**：日志断在半句、没有 traceback，
REM  页面上却显示"已拉起进程" —— 看起来就是"按钮没用"。
REM
REM  双击本脚本起的中台，父进程是 explorer.exe，**不在任何会被回收的进程树里**，
REM  于是它派生的守护进程能活下来，按钮才真正可用。
REM ============================================================================
REM ============================================================================
cd /d "%~dp0"
title Crypto LS - 参数中台 (http://127.0.0.1:8790)

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

echo.
echo   参数中台正在启动，稍后浏览器打开 http://127.0.0.1:8790
echo   本窗口请保持打开（关掉窗口 = 中台停止）。
echo   在这个中台里点「启动自动任务」才能生效。
echo.

"%PY%" "%~dp0webapp\server.py" 8790

echo.
echo [exit code %ERRORLEVEL%]
pause
