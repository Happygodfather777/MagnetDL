@echo off
setlocal
cd /d "%~dp0"
set "PY="
for %%V in (3.13 3.12 3.11 3.10 3.9) do (
    if not defined PY ( py -%%V -c "import sys" >nul 2>nul && set "PY=%%V" )
)
if not defined PY (
    echo MagnetDL needs Python 3.13 or older. Get it from https://www.python.org/downloads/windows/
    pause
    exit /b 1
)
py -%PY% -c "import libtorrent" 2>nul || py -%PY% -m pip install libtorrent
start "" pyw -%PY% magnetdl.py
