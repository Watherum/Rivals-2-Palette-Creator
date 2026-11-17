@echo off
rem Build the exe and zip it with the downloaded renders, so users get the whole
rem library in one download instead of each fetching it from the wiki.
rem   dist\Rivals-2-Palette-Creator-v<version>-with-renders.zip
rem Extracted, the exe sits next to Resources\Character_Renders_Source and finds
rem it on its own. Any previous zip for this version is deleted and rebuilt.
rem Extra flags pass through: BUILD_RELEASE.bat --clean
setlocal
set "HERE=%~dp0"
title Rivals 2 Palette Creator - build release

set "PY="
if exist "C:\Python314\python.exe" set "PY=C:\Python314\python.exe"
if not defined PY set "PY=python"

"%PY%" "%HERE%scripts\build_exe.py" --include-renders --force %*
if errorlevel 1 goto :fail

echo(
pause
exit /b 0

:fail
echo(
echo Release build FAILED. See the messages above.
echo(
pause
exit /b 1
