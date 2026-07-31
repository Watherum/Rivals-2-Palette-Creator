@echo off
rem Same as run_palette_creator.bat but keeps a console open so import errors
rem and tracebacks are visible.
setlocal
set "HERE=%~dp0"

set "PY="
if exist "%HERE%.venv\Scripts\python.exe" set "PY=%HERE%.venv\Scripts\python.exe"
if not defined PY if exist "C:\Python314\python.exe" set "PY=C:\Python314\python.exe"
if not defined PY set "PY=python"

"%PY%" "%HERE%main.py"
echo(
echo [exit code %errorlevel%]
pause
