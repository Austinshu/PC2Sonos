# PC2Sonos installer
# Run this from a normal PowerShell window, with the current directory
# set to this pc2sonos folder (or just double-click install.bat).

$AppDir = $PSScriptRoot
$transcriptPath = Join-Path $AppDir "install-log.txt"

try { Start-Transcript -Path $transcriptPath -Append -ErrorAction SilentlyContinue | Out-Null } catch {}

Write-Host "== PC2Sonos installer ==" -ForegroundColor Green

Write-Host "Checking for Python..."
$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
    Write-Host "Python was not found on PATH. Install Python 3.10+ from https://python.org (check 'Add to PATH' during install), then re-run this script." -ForegroundColor Red
    try { Stop-Transcript | Out-Null } catch {}
    exit 1
}
Write-Host "Using Python: $($python.Source)"

# If a previous (possibly broken) build is still running, stop it so the
# .exe isn't locked when we try to overwrite it.
Get-Process -Name "PC2Sonos" -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Milliseconds 500

Write-Host "Installing Python dependencies (soco, pyaudiowpatch, flask, pystray, pillow, pyinstaller)..."
python -m pip install --upgrade pip
python -m pip install -r "$AppDir\requirements.txt"
if ($LASTEXITCODE -ne 0) {
    Write-Host "pip install failed (exit code $LASTEXITCODE). See output above." -ForegroundColor Red
    try { Stop-Transcript | Out-Null } catch {}
    exit 1
}

$exePath = Join-Path $AppDir "PC2Sonos.exe"

# Remove any previous build output first, so a FAILED build this run can
# never be mistaken for success just because an old .exe is still sitting
# there from a previous run.
Remove-Item -Path $exePath -Force -ErrorAction SilentlyContinue
Remove-Item -Path (Join-Path $AppDir "build") -Recurse -Force -ErrorAction SilentlyContinue

Write-Host ""
Write-Host "Building PC2Sonos.exe (this bundles everything into one file, takes ~30-60s)..."
Push-Location $AppDir
python -m PyInstaller --onefile --noconsole --name PC2Sonos --distpath "$AppDir" --workpath "$AppDir\build" --specpath "$AppDir\build" main.py
$buildExitCode = $LASTEXITCODE
Pop-Location

if ($buildExitCode -ne 0) {
    Write-Host "PyInstaller exited with code $buildExitCode. See output above." -ForegroundColor Red
    try { Stop-Transcript | Out-Null } catch {}
    exit 1
}
if (-not (Test-Path $exePath)) {
    Write-Host "Build failed -- PC2Sonos.exe was not produced. Check the PyInstaller output above." -ForegroundColor Red
    try { Stop-Transcript | Out-Null } catch {}
    exit 1
}
Write-Host "Built: $exePath" -ForegroundColor Green

Write-Host ""
Write-Host "Creating Windows Startup shortcut so PC2Sonos launches automatically at login..."
$startupFolder = [Environment]::GetFolderPath("Startup")
$shortcutPath = Join-Path $startupFolder "PC2Sonos.lnk"

$WScriptShell = New-Object -ComObject WScript.Shell
$shortcut = $WScriptShell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $exePath
$shortcut.WorkingDirectory = $AppDir
$shortcut.WindowStyle = 7
$shortcut.Save()

# Also drop a shortcut on the Desktop so it's easy to find/pin/launch by hand.
$desktopShortcut = Join-Path ([Environment]::GetFolderPath("Desktop")) "PC2Sonos.lnk"
$shortcut2 = $WScriptShell.CreateShortcut($desktopShortcut)
$shortcut2.TargetPath = $exePath
$shortcut2.WorkingDirectory = $AppDir
$shortcut2.Save()

Write-Host "Startup shortcut created at: $shortcutPath" -ForegroundColor Green
Write-Host "Desktop shortcut created at: $desktopShortcut" -ForegroundColor Green
Write-Host ""
Write-Host "REMAINING MANUAL STEP (one-time):" -ForegroundColor Yellow
Write-Host "PC2Sonos needs VB-Audio Virtual Cable (free) installed and set as your"
Write-Host "Windows default PLAYBACK device, so it can intercept and delay your"
Write-Host "local audio before it hits your real speakers. Get it from:"
Write-Host "    https://vb-audio.com/Cable/"
Write-Host "After installing it: Settings > System > Sound > Output > 'CABLE Input (VB-Audio Virtual Cable)'"
Write-Host ""
Write-Host "Starting PC2Sonos.exe now..."
Start-Process $exePath -WorkingDirectory $AppDir
Write-Host "Done. Dashboard: http://127.0.0.1:5757" -ForegroundColor Green

try { Stop-Transcript | Out-Null } catch {}
