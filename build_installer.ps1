# Builds the ONE-DOWNLOAD installer: PC2Sonos-Setup.exe
# Run on a Windows x64 machine with Python installed (only the build
# machine needs Python -- the produced installer runs anywhere).

$ErrorActionPreference = "Continue"
$Root = $PSScriptRoot
Start-Transcript -Path "$Root\installer-build-log.txt" -Append

Write-Host ""
Write-Host "=== PC2Sonos installer build ===" -ForegroundColor Cyan

# --- 1. dependencies -------------------------------------------------
Write-Host "[1/5] Installing build dependencies..."
python -m pip install -r "$Root\requirements.txt" --quiet
if ($LASTEXITCODE -ne 0) {
    Write-Host "pip install FAILED" -ForegroundColor Red
    Stop-Transcript; exit 1
}

# --- 2. the app itself ------------------------------------------------
Write-Host "[2/5] Building PC2Sonos.exe..."
Get-Process -Name "PC2Sonos" -ErrorAction SilentlyContinue |
    Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 1
Remove-Item "$Root\PC2Sonos.exe" -Force -ErrorAction SilentlyContinue
python -m PyInstaller --onefile --noconsole --name PC2Sonos `
    --distpath "$Root" --workpath "$Root\build" --specpath "$Root\build" `
    "$Root\main.py"
if ($LASTEXITCODE -ne 0 -or -not (Test-Path "$Root\PC2Sonos.exe")) {
    Write-Host "PC2Sonos.exe build FAILED" -ForegroundColor Red
    Stop-Transcript; exit 1
}

# --- 3. the uninstaller -------------------------------------------------
Write-Host "[3/5] Building PC2Sonos-Uninstall.exe..."
Remove-Item "$Root\PC2Sonos-Uninstall.exe" -Force -ErrorAction SilentlyContinue
python -m PyInstaller --onefile --uac-admin --name PC2Sonos-Uninstall `
    --distpath "$Root" --workpath "$Root\build" --specpath "$Root\build" `
    "$Root\uninstall.py"
if ($LASTEXITCODE -ne 0 -or -not (Test-Path "$Root\PC2Sonos-Uninstall.exe")) {
    Write-Host "PC2Sonos-Uninstall.exe build FAILED" -ForegroundColor Red
    Stop-Transcript; exit 1
}

# --- 4. payload (app + uninstaller + unmodified VB-CABLE driver pack) --
Write-Host "[4/5] Assembling installer payload..."
Remove-Item "$Root\payload" -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Path "$Root\payload\VBCABLE" -Force | Out-Null
Copy-Item "$Root\PC2Sonos.exe" "$Root\payload\PC2Sonos.exe"
Copy-Item "$Root\PC2Sonos-Uninstall.exe" "$Root\payload\PC2Sonos-Uninstall.exe"

$cableZip = Join-Path $env:USERPROFILE "Downloads\VBCABLE_Driver_Pack45.zip"
if (-not (Test-Path $cableZip)) {
    Write-Host "VBCABLE_Driver_Pack45.zip not found in Downloads" -ForegroundColor Red
    Stop-Transcript; exit 1
}
Expand-Archive -Path $cableZip -DestinationPath "$Root\payload\VBCABLE" -Force
if (-not (Test-Path "$Root\payload\VBCABLE\VBCABLE_Setup_x64.exe")) {
    Write-Host "driver pack extraction looks wrong (no VBCABLE_Setup_x64.exe)" -ForegroundColor Red
    Stop-Transcript; exit 1
}

# --- 5. the installer ---------------------------------------------------
Write-Host "[5/5] Building PC2Sonos-Setup.exe..."
Remove-Item "$Root\PC2Sonos-Setup.exe" -Force -ErrorAction SilentlyContinue
python -m PyInstaller --onefile --uac-admin --name PC2Sonos-Setup `
    --distpath "$Root" --workpath "$Root\build" --specpath "$Root\build" `
    --add-data "$Root\payload;payload" `
    "$Root\setup_installer.py"
if ($LASTEXITCODE -ne 0 -or -not (Test-Path "$Root\PC2Sonos-Setup.exe")) {
    Write-Host "PC2Sonos-Setup.exe build FAILED" -ForegroundColor Red
    Stop-Transcript; exit 1
}

$size = [math]::Round((Get-Item "$Root\PC2Sonos-Setup.exe").Length / 1MB, 1)
Write-Host ""
Write-Host "DONE: $Root\PC2Sonos-Setup.exe ($size MB)" -ForegroundColor Green
Write-Host "That single file is the whole product -- share it anywhere."
Stop-Transcript
