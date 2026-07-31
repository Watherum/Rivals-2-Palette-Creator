@echo off
rem Launch the GUI with no console window. Use run_debug.bat to see tracebacks.
setlocal
set "HERE=%~dp0"

set "PYW="
if exist "%HERE%.venv\Scripts\pythonw.exe" set "PYW=%HERE%.venv\Scripts\pythonw.exe"
if not defined PYW if exist "C:\Python314\pythonw.exe" set "PYW=C:\Python314\pythonw.exe"
if not defined PYW set "PYW=pythonw"

start "" "%PYW%" "%HERE%main.py"
