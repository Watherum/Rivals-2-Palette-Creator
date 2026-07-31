@echo off
setlocal enabledelayedexpansion
set "HERE=%~dp0"
title Rivals 2 Palette Creator - install requirements

echo ============================================================
echo   Rivals 2 Palette Creator - dependency install
echo ============================================================
echo(

rem --- locate an interpreter -------------------------------------------------
set "PY="
if exist "%HERE%.venv\Scripts\python.exe" set "PY=%HERE%.venv\Scripts\python.exe"
if not defined PY if exist "C:\Python314\python.exe" set "PY=C:\Python314\python.exe"
if not defined PY (
  for /f "delims=" %%P in ('where python 2^>nul') do (
    if not defined PY set "PY=%%P"
  )
)
if not defined PY (
  echo ERROR: No Python interpreter found.
  echo Install Python 3.11 or newer from https://www.python.org/downloads/
  echo and be sure to tick "Add python.exe to PATH".
  goto :fail
)

echo Interpreter: %PY%
"%PY%" --version
echo(

rem --- tkinter is bundled with CPython but can be deselected at install time --
echo Checking tkinter...
"%PY%" -c "import tkinter" 2>nul
if errorlevel 1 (
  echo(
  echo ERROR: tkinter is missing from this Python installation.
  echo Re-run the Python installer, choose "Modify", and enable
  echo "tcl/tk and IDLE". The GUI cannot start without it.
  goto :fail
)
echo   ok
echo(

echo Upgrading pip...
"%PY%" -m pip install --upgrade pip --quiet
echo(

rem --- --only-binary keeps a missing wheel from starting a 40-minute build ----
echo Installing requirements (prebuilt wheels only)...
"%PY%" -m pip install --only-binary=:all: -r "%HERE%requirements.txt"
if errorlevel 1 (
  echo(
  echo ------------------------------------------------------------
  echo A prebuilt wheel was unavailable for this Python version.
  echo(
  echo   scipy and scikit-image are the usual culprits on a Python
  echo   release that is newer than their latest wheels.
  echo(
  echo Options:
  echo   1. Install Python 3.12 or 3.13 alongside and re-run this script.
  echo   2. Re-run allowing source builds ^(slow, needs MSVC Build Tools^):
  echo        "%PY%" -m pip install -r "%HERE%requirements.txt"
  echo ------------------------------------------------------------
  goto :fail
)
echo(

echo Verifying imports...
"%PY%" -c "import tkinter, customtkinter, numpy, scipy, skimage, PIL, requests; print('  All dependencies OK')"
if errorlevel 1 goto :fail
echo(

echo ============================================================
echo   Done. Launch the app with run_palette_creator.bat
echo ============================================================
echo(
pause
exit /b 0

:fail
echo(
echo Install FAILED. See the messages above.
echo(
pause
exit /b 1
