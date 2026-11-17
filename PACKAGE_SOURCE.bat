@echo off
rem Zip the Python source into dist/ for release. Double-click for the source-only
rem build, or pass flags through: PACKAGE_SOURCE.bat --include-renders --force
setlocal enabledelayedexpansion
set "HERE=%~dp0"
title Rivals 2 Palette Creator - package source

set "PY="
if exist "%HERE%.venv\Scripts\python.exe" set "PY=%HERE%.venv\Scripts\python.exe"
if not defined PY if exist "C:\Python314\python.exe" set "PY=C:\Python314\python.exe"
if not defined PY set "PY=python"

rem Flags on the command line win; a bare double-click gets asked instead.
set "ARGS=%*"
if not defined ARGS (
  echo ============================================================
  echo   Rivals 2 Palette Creator - package source
  echo ============================================================
  echo(
  echo The character renders are large and the app can refetch them
  echo from the Downloads tab, so they are left out by default.
  echo(
  set "RENDERS="
  set /p "RENDERS=Include the renders anyway? [y/N] "
  echo(
  if /i "!RENDERS!"=="y" set "ARGS=--include-renders"
)

"%PY%" "%HERE%scripts\package_source.py" !ARGS!
if errorlevel 1 goto :fail

echo(
pause
exit /b 0

:fail
echo(
echo Release FAILED. See the messages above.
echo(
pause
exit /b 1
