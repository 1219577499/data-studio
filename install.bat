@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"
title Data Studio - setup

echo.
echo   ==========================================================
echo     Data Studio - one time setup
echo   ==========================================================
echo.
echo   Creates a private virtual environment in .venv and installs
echo   the required packages into it.
echo   Nothing is installed system-wide, no admin rights needed.
echo.

set "BASE_PY="

for /f "delims=" %%P in ('where python 2^>nul') do (
    if not defined BASE_PY set "BASE_PY=%%P"
)
if not defined BASE_PY (
    for /f "delims=" %%P in ('py -c "import sys;print(sys.executable)" 2^>nul') do (
        if not defined BASE_PY set "BASE_PY=%%P"
    )
)

if not defined BASE_PY (
    echo   [ERROR] Python was not found on this computer.
    echo.
    echo   1^) Download it from https://www.python.org/downloads/
    echo   2^) During installation, tick "Add python.exe to PATH"
    echo   3^) Close this window and run install.bat again
    echo.
    pause
    exit /b 1
)

echo   Using Python: %BASE_PY%
echo.
"%BASE_PY%" "%~dp0setup_env.py"
set "RC=%ERRORLEVEL%"

echo.
if "%RC%"=="0" (
    echo   Setup finished. Now double-click  run.bat  to start.
) else (
    echo   Setup FAILED with exit code %RC%. Read the messages above.
)
echo.
pause
exit /b %RC%
