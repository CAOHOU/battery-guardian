@echo off
setlocal
cd /d "%~dp0"

set "VENV=%~dp0.venv-build"
set "PY=%VENV%\Scripts\python.exe"
set "BASEPY="

if exist "%PY%" goto have_venv

rem 必须用带 tkinter 的 Python 建虚拟环境，否则打出来的 exe 开不了窗口
for /f "delims=" %%N in ('dir /b /ad "%LOCALAPPDATA%\Programs\Python\Python3*" 2^>nul') do (
  if exist "%LOCALAPPDATA%\Programs\Python\%%N\python.exe" (
    "%LOCALAPPDATA%\Programs\Python\%%N\python.exe" -c "import tkinter" >nul 2>nul
    if not errorlevel 1 set "BASEPY=%LOCALAPPDATA%\Programs\Python\%%N\python.exe"
  )
)
if defined BASEPY goto make_venv

py -3 -c "import tkinter" >nul 2>nul
if not errorlevel 1 set "BASEPY=py -3"
if defined BASEPY goto make_venv

echo [ERROR] No Python 3 with tkinter found.
echo Install from https://www.python.org/downloads/
echo Make sure "tcl/tk and IDLE" is checked in the installer.
timeout /t 8 >nul
exit /b 1

:make_venv
echo [1/3] Creating build venv at .venv-build ...
%BASEPY% -m venv "%VENV%"
if not exist "%PY%" echo [ERROR] venv creation failed. & timeout /t 8 >nul & exit /b 1

:have_venv
"%PY%" -c "import PyInstaller" >nul 2>nul
if not errorlevel 1 goto build

echo [2/3] Installing PyInstaller ...
"%PY%" -m pip install --upgrade pip >nul 2>nul
"%PY%" -m pip install pyinstaller
"%PY%" -c "import PyInstaller" >nul 2>nul
if errorlevel 1 echo [ERROR] PyInstaller install failed. & timeout /t 8 >nul & exit /b 1

:build
echo [3/3] Building ...
"%PY%" -m PyInstaller --noconfirm --clean --onefile --windowed --name BatteryGuardian --icon "assets\icon.ico" --add-data "assets;assets" battery_guardian.py
if errorlevel 1 echo [ERROR] Build failed. & timeout /t 8 >nul & exit /b 1

echo.
echo Done.  dist\BatteryGuardian.exe
echo.
