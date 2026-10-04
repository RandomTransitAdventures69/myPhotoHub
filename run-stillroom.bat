@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo [Stillroom] Creating Python environment...
  py -3 -m venv .venv
  if errorlevel 1 (
    echo Could not create a Python environment. Install Python 3.11 or newer from python.org and enable "Add Python to PATH".
    pause
    exit /b 1
  )
)
echo [Stillroom] Checking dependencies...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements.txt
if errorlevel 1 (
  echo Dependency installation failed.
  pause
  exit /b 1
)
echo.
echo Stillroom is starting. Keep this window open.
echo Open http://127.0.0.1:5055 on this PC. Note that this is your local IP, meaning it isnt accessable outside your network(s) or VLAN(s)
echo for remote access, use Tailscale or your reverse proxy of choice, unless you port forward or some other hackerman things idk man
echo thanks to chatgpt, this horrid app exists
echo Available network URLs:
for /f "tokens=2 delims=:" %%a in ('ipconfig ^| findstr /c:"IPv4 Address"') do (
    set "IP=%%a"
    :: This removes any leading spaces from the IP string
    set "IP=!IP: =!"
    echo   http://!IP!:5055
)
echo.
".venv\Scripts\python.exe" app.py
pause
