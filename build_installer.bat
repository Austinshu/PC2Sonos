@echo off
echo Building the one-download PC2Sonos-Setup.exe ...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0build_installer.ps1"
echo.
echo (Full log: installer-build-log.txt)
timeout /t 15
