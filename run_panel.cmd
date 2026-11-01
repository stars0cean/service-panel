@echo off
chcp 65001 >nul
title Service Panel
setlocal

rem Always run relative to this script
set "DIR=%~dp0"
if "%DIR:~-1%"=="\" set "DIR=%DIR:~0,-1%"

rem Prefer local venv, fall back to python on PATH
set "PY=%DIR%\.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=%DIR%\venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

rem Startup tasks inject proxy env vars that break child processes
for %%k in (http_proxy HTTP_PROXY https_proxy HTTPS_PROXY all_proxy ALL_PROXY) do set "%%k="

cd /d "%DIR%"
echo Starting Service Panel  http://127.0.0.1:8899
echo.
"%PY%" -X utf8 "%DIR%\panel.py" %*
echo.
echo Panel exited with code %ERRORLEVEL%.
pause
