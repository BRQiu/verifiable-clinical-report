@echo off
REM ============================================================
REM  Start the web UI. Windows only.
REM
REM  IMPORTANT: keep this file pure ASCII.
REM  cmd.exe parses .bat bytes using the system ANSI codepage
REM  (GBK on Chinese Windows). A UTF-8 encoded Chinese string
REM  here gets split into garbage bytes mid-command -- it once
REM  truncated "python" into "ython". All Chinese UI text lives
REM  in launch.py instead.
REM ============================================================
chcp 65001 >nul 2>&1
cd /d "%~dp0"

set "PYTHONPATH=%~dp0.pylibs"
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"

where python >nul 2>&1
if errorlevel 1 (
    echo [ERROR] python not found in PATH.
    echo         Install Python 3.10+ and tick "Add python.exe to PATH".
    echo.
    pause
    exit /b 1
)

python "%~dp0launch.py"
set "RC=%ERRORLEVEL%"

if not "%RC%"=="0" (
    echo.
    echo [ERROR] launcher exited with code %RC%
    pause
)
exit /b %RC%
