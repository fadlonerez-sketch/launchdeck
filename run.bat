@echo off
rem Start LaunchDeck from source. Uses .venv if present, otherwise Python from PATH.
setlocal
cd /d "%~dp0"
set "PY="
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
if not defined PY (
    where py >nul 2>nul && set "PY=py -3"
)
if not defined PY (
    where python >nul 2>nul && set "PY=python"
)
if not defined PY (
    echo Python 3.11 or newer was not found. Install it from https://www.python.org/downloads/windows/
    pause
    exit /b 1
)
%PY% scripts\launch.py %*
if errorlevel 1 (
    pause
    exit /b 1
)
endlocal
