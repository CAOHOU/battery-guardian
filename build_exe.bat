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
"%SystemRoot%\System32\timeout.exe" /t 8 >nul 2>nul
exit /b 1

:make_venv
echo [1/4] Creating build venv at .venv-build ...
%BASEPY% -m venv "%VENV%"
if not exist "%PY%" echo [ERROR] venv creation failed. & "%SystemRoot%\System32\timeout.exe" /t 8 >nul 2>nul & exit /b 1

:have_venv
"%PY%" -c "import PyInstaller" >nul 2>nul
if not errorlevel 1 goto gen_meta

echo [2/4] Installing PyInstaller ...
"%PY%" -m pip install --upgrade pip >nul 2>nul
"%PY%" -m pip install pyinstaller
"%PY%" -c "import PyInstaller" >nul 2>nul
if errorlevel 1 echo [ERROR] PyInstaller install failed. & "%SystemRoot%\System32\timeout.exe" /t 8 >nul 2>nul & exit /b 1

:gen_meta
echo [3/4] Generating version_info.txt ...
"%PY%" gen_version_info.py
if errorlevel 1 echo [ERROR] gen_version_info.py failed. & "%SystemRoot%\System32\timeout.exe" /t 8 >nul 2>nul & exit /b 1

:build
echo [4/4] Building ...
rem 先结束正在运行的实例，否则 dist 里的旧 exe 被占用，PyInstaller 覆盖会失败
taskkill /f /im BatteryGuardian.exe >nul 2>nul
rem 排除不用的模块：ssl/_hashlib 会带进 libcrypto(约 5MB) + libssl(约 0.8MB)
"%PY%" -m PyInstaller --noconfirm --clean --onefile --windowed ^
  --name BatteryGuardian ^
  --icon "assets\icon.ico" ^
  --add-data "assets;assets" ^
  --version-file "version_info.txt" ^
  --exclude-module ssl ^
  --exclude-module _ssl ^
  --exclude-module _hashlib ^
  --exclude-module hashlib ^
  --exclude-module _decimal ^
  --exclude-module decimal ^
  --exclude-module _lzma ^
  --exclude-module lzma ^
  --exclude-module _bz2 ^
  --exclude-module bz2 ^
  --exclude-module _zstd ^
  --exclude-module _sqlite3 ^
  --exclude-module sqlite3 ^
  --exclude-module _multiprocessing ^
  --exclude-module multiprocessing ^
  --exclude-module asyncio ^
  --exclude-module unittest ^
  --exclude-module doctest ^
  --exclude-module pydoc ^
  --exclude-module pdb ^
  --exclude-module email ^
  --exclude-module http ^
  --exclude-module urllib ^
  --exclude-module xml ^
  --exclude-module lib2to3 ^
  --exclude-module setuptools ^
  --exclude-module pkg_resources ^
  --exclude-module pip ^
  --exclude-module wheel ^
  --exclude-module test ^
  --exclude-module tkinter.test ^
  --exclude-module idlelib ^
  --exclude-module turtle ^
  --exclude-module turtledemo ^
  --exclude-module curses ^
  battery_guardian.py
if errorlevel 1 echo [ERROR] Build failed. & "%SystemRoot%\System32\timeout.exe" /t 8 >nul 2>nul & exit /b 1

echo.
echo [5/5] Verifying exe ...
rem 排除模块后如果代码里 import 了它们，exe 会卡死而不是报错，
rem 所以每次打包都真跑一次 --self-test，超时即失败。
"%PY%" verify_exe.py
if errorlevel 1 (
  echo.
  echo [ERROR] exe verification failed.
  rem 不用 pause：脚本被自动调用时会永久挂住，看不出是失败还是卡死。
  rem 给 15 秒窗口让双击的人看清错误，然后自己退出。
  echo Exiting in 15 seconds...
  "%SystemRoot%\System32\timeout.exe" /t 15 >nul 2>nul
  exit /b 1
)

echo.
echo Done.  dist\BatteryGuardian.exe
echo.
