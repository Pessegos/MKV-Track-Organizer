param(
    [switch]$OneFile,
    [switch]$SkipInstall
)

$ErrorActionPreference = "Stop"

if (-not $SkipInstall) {
    python -m pip install -r requirements.txt
    python -m pip install pyinstaller
}

$mode = if ($OneFile) { "--onefile" } else { "--onedir" }

python -m PyInstaller `
    --noconfirm `
    --windowed `
    --name "MKV Track Organizer" `
    $mode `
    .\mkv_track_organizer_gui.py

Write-Host ""
Write-Host "Build complete."
if ($OneFile) {
    Write-Host "Executable: .\dist\MKV Track Organizer.exe"
} else {
    Write-Host "Executable: .\dist\MKV Track Organizer\MKV Track Organizer.exe"
}
