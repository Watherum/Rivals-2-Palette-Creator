@echo off
rem Build the standalone exe into dist/. Double-click, or pass flags through:
rem   BUILD_EXE.bat --include-renders --force
rem   BUILD_EXE.bat --clean
setlocal
set "HERE=%~dp0"
title Rivals 2 Palette Creator - build exe

rem Only used to create the private build venv (.venv-build); the exe is built
rem from that venv, not from this interpreter's installed packages.
set "PY="
if exist "C:\Python314\python.exe" set "PY=C:\Python314\python.exe"
if not defined PY set "PY=python"

"%PY%" "%HERE%scripts\build_exe.py" %*
if errorlevel 1 goto :fail

echo(
pause
exit /b 0

:fail
echo(
echo Build FAILED. See the messages above.
echo(
pause
exit /b 1
