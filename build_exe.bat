@echo off
rem Build dist\LaunchDeck\LaunchDeck.exe with PyInstaller, then run the packaged self-test.
setlocal
cd /d "%~dp0"
set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
%PY% -m PyInstaller --version >nul 2>nul || (
    echo PyInstaller is missing. Install it with:  %PY% -m pip install -r requirements-dev.txt
    exit /b 1
)
%PY% -m PyInstaller --noconfirm --clean launchdeck.spec || exit /b 1
set "LAUNCHDECK_DATA_DIR=%TEMP%\launchdeck-selftest"
if exist "%LAUNCHDECK_DATA_DIR%" rmdir /s /q "%LAUNCHDECK_DATA_DIR%"
mkdir "%LAUNCHDECK_DATA_DIR%"
start "" /wait dist\LaunchDeck\LaunchDeck.exe --self-test "%LAUNCHDECK_DATA_DIR%\report.json"
if errorlevel 1 (
    echo Packaged self-test FAILED. See %LAUNCHDECK_DATA_DIR%\report.json
    exit /b 1
)
echo Packaged self-test passed: dist\LaunchDeck\LaunchDeck.exe
endlocal
