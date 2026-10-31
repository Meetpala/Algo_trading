@echo off
setlocal
title APEX-3 Live Broker Establishment
cd /d "%~dp0"

echo ======================================================================
echo           APEX-3 AUTOMATED BROKER CONNECTION & LAUNCHER
echo ======================================================================
echo.

if exist .venv\Scripts\python.exe (
    set "PY_EXE=.venv\Scripts\python.exe"
) else (
    set "PY_EXE=python"
)

"%PY_EXE%" connect_live.py %*

if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [!] Process exited with code %ERRORLEVEL%.
    pause
)

