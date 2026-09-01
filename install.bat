@echo off
setlocal
cd /d "%~dp0"
echo == PC2Sonos installer ==
echo This installs Python packages, builds PC2Sonos.exe, sets it to run at
echo startup, and launches it. Takes about a minute.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1"
echo.
echo Done. PC2Sonos.exe is now in this folder and on your Desktop.
echo This window will close in a few seconds...
timeout /t 10 >nul
