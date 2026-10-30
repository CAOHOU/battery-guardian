@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

set "FOUND="

for /f "delims=" %%N in ('dir /b /ad "%LOCALAPPDATA%\Programs\Python\Python3*" 2^>nul') do (
  set "CAND=%LOCALAPPDATA%\Programs\Python\%%N"
  if exist "!CAND!\python.exe" if exist "!CAND!\pythonw.exe" (
    "!CAND!\python.exe" -c "import tkinter" >nul 2>nul
    if not errorlevel 1 set "FOUND=!CAND!\pythonw.exe"
  )
)

if not defined FOUND (
  py -3 -c "import tkinter" >nul 2>nul
  if not errorlevel 1 set "FOUND=py -3w"
)

if not defined FOUND (
  python -c "import tkinter" >nul 2>nul
  if not errorlevel 1 set "FOUND=pythonw"
)

if not defined FOUND (
  echo.
  echo Python 3 with tkinter was not found.
  echo Install it from https://www.python.org/downloads/
  echo Make sure "tcl/tk and IDLE" is included in the installer.
  echo.
  pause
  exit /b 1
)

start "" !FOUND! "battery_guardian.py"
exit /b 0
