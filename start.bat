@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "APP=%~dp0app.py"
set "LOG=%~dp0startup_error.log"

where python >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found.
    echo Install Python 3.10+ and check "Add python.exe to PATH".
    echo.
    pause
    exit /b 1
)

python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" 2>"%LOG%"
if errorlevel 1 (
    echo [ERROR] Python 3.10+ required.
    echo.
    type "%LOG%" 2>nul
    echo.
    pause
    exit /b 1
)

python -c "import tkinter, numpy" 2>"%LOG%"
if errorlevel 1 (
    echo [ERROR] Missing dependency: numpy and tkinter are required.
    echo Run: python -m pip install numpy
    echo.
    type "%LOG%" 2>nul
    echo.
    pause
    exit /b 1
)

if exist "%LOG%" del "%LOG%" >nul 2>&1

where pythonw >nul 2>&1
if not errorlevel 1 (
    start "stall-cutter" /D "%~dp0" pythonw "%APP%"
    ping 127.0.0.1 -n 2 >nul
    if exist "%LOG%" (
        echo [ERROR] App failed to start. See startup_error.log
        echo.
        type "%LOG%"
        echo.
        pause
        exit /b 1
    )
    exit /b 0
)

python "%APP%"
if errorlevel 1 (
    echo.
    echo [ERROR] App failed to start.
    if exist "%LOG%" type "%LOG%"
    pause
    exit /b 1
)
exit /b 0
