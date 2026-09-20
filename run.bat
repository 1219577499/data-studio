@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"
title Data Studio

rem ===================================================================
rem  Data Studio launcher
rem  Finds a Python that already has the dependencies, then starts the
rem  server.  Search order:
rem    1. .venv next to this file   (created by install.bat)
rem    2. any python on PATH that can import fastapi + pandas
rem    3. the py launcher
rem    4. this machine's dev interpreter, if it happens to exist
rem  ASCII only on purpose: cmd parses .bat with the ANSI code page, so
rem  UTF-8 Chinese text in here would be mangled into garbage bytes.
rem ===================================================================

set "PY="

if exist "%~dp0.venv\Scripts\python.exe" set "PY=%~dp0.venv\Scripts\python.exe"

if not defined PY (
    for /f "delims=" %%P in ('where python 2^>nul') do (
        if not defined PY (
            "%%P" -c "import fastapi, pandas, uvicorn" >nul 2>&1
            if not errorlevel 1 set "PY=%%P"
        )
    )
)

if not defined PY (
    for /f "delims=" %%P in ('py -c "import sys;print(sys.executable)" 2^>nul') do (
        if not defined PY (
            "%%P" -c "import fastapi, pandas, uvicorn" >nul 2>&1
            if not errorlevel 1 set "PY=%%P"
        )
    )
)


if not defined PY (
    echo.
    echo   ==========================================================
    echo     Python with the required packages was NOT found.
    echo   ==========================================================
    echo.
    echo   Please run  install.bat  first - it sets everything up
    echo   automatically ^(no admin rights needed^).
    echo.
    echo   Or do it manually:
    echo       pip install -r requirements.txt
    echo.
    echo   Need Python?  https://www.python.org/downloads/
    echo   Remember to tick "Add python.exe to PATH" during setup.
    echo.
    pause
    exit /b 1
)

echo.
echo   ============================================
echo     Data Studio
echo   ============================================
echo     URL    : http://127.0.0.1:8848
echo     Python : %PY%
echo.
echo     The browser will open automatically.
echo     Close this window to stop the server.
echo   ============================================
echo.

start "" http://127.0.0.1:8848
"%PY%" "%~dp0app.py" --port 8848

echo.
echo   Server stopped.
pause
