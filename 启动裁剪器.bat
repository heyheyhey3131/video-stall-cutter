@echo off
chcp 65001 >nul
cd /d "%~dp0"

where pythonw >nul 2>&1
if %errorlevel%==0 (
    start "" pythonw "%~dp0app.py"
    exit /b 0
)

where python >nul 2>&1
if %errorlevel%==0 (
    python "%~dp0app.py"
    if errorlevel 1 pause
    exit /b %errorlevel%
)

echo 没有找到 Python。请先安装 Python 3.10+ 并勾选 Add to PATH。
pause
exit /b 1
