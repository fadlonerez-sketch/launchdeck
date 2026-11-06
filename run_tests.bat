@echo off
rem Run the automated tests. They use temporary folders and an offscreen Qt platform only.
setlocal
cd /d "%~dp0"
set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
%PY% -m pytest %*
exit /b %errorlevel%
