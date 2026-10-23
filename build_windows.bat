@echo off
setlocal EnableDelayedExpansion
title Building MagnetDL.exe
cd /d "%~dp0"

echo.
echo === MagnetDL builder ===
echo.

REM libtorrent only has Windows packages for Python 3.9 - 3.13.
REM Find a Python in that range (newest first) using the "py" launcher.
set "PY="
for %%V in (3.13 3.12 3.11 3.10 3.9) do (
    if not defined PY (
        py -%%V -c "import sys" >nul 2>nul && set "PY=py -%%V"
    )
)

REM Fall back to plain "python" if its version is supported
if not defined PY (
    python -c "import sys; sys.exit(0 if (3,9)<=sys.version_info[:2]<=(3,13) else 1)" >nul 2>nul && set "PY=python"
)

if not defined PY (
    echo No compatible Python found.
    echo.
    echo MagnetDL's download engine ^(libtorrent^) needs Python 3.13 or older,
    echo and this PC only has a newer or no Python installed:
    py --list 2>nul || python --version 2>nul
    echo.
    echo Fix: install Python 3.13 ^(it can sit alongside your current Python^):
    echo   https://www.python.org/downloads/windows/
    echo   Pick the newest "Python 3.13.x" and its "Windows installer ^(64-bit^)".
    echo Then run this file again.
    echo.
    start "" "https://www.python.org/downloads/windows/"
    pause
    exit /b 1
)

for /f "delims=" %%i in ('!PY! -c "import sys,platform; print(sys.version.split()[0], platform.architecture()[0])"') do set "PYVER=%%i"
echo Using Python !PYVER!
echo.

REM Install what the build needs
echo Installing libtorrent and PyInstaller...
!PY! -m pip install --upgrade pip >nul
!PY! -m pip install --upgrade libtorrent pyinstaller
if errorlevel 1 (
    echo Could not install the required packages. Scroll up to see why.
    pause
    exit /b 1
)

REM Make sure libtorrent actually loads on this PC
!PY! -c "import libtorrent" 2>nul
if errorlevel 1 (
    echo.
    echo libtorrent installed but could not load. This usually means the
    echo Microsoft Visual C++ Redistributable is missing. Install it from:
    echo   https://aka.ms/vs/17/release/vc_redist.x64.exe
    echo then run this file again.
    pause
    exit /b 1
)

REM Build the single-file app
echo.
echo Building MagnetDL.exe (this takes a minute)...
!PY! -m PyInstaller --noconfirm --clean --onefile --windowed ^
    --name MagnetDL --hidden-import libtorrent magnetdl.py
if errorlevel 1 (
    echo Build failed. Scroll up to see the error.
    pause
    exit /b 1
)

copy /y "dist\MagnetDL.exe" "MagnetDL.exe" >nul
rmdir /s /q build >nul 2>nul
del /q MagnetDL.spec >nul 2>nul

echo.
echo Done! MagnetDL.exe is in this folder:
echo   %~dp0MagnetDL.exe
echo You can move it anywhere, e.g. your Desktop.
echo.
pause
