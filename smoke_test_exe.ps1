param(
    [string]$ExePath = ".\dist\MKV Track Organizer\MKV Track Organizer.exe"
)

$ErrorActionPreference = "Stop"

$resolvedExe = Resolve-Path -LiteralPath $ExePath -ErrorAction Stop
$metadataSource = Get-Content -LiteralPath ".\app_metadata.py" -Raw
if ($metadataSource -notmatch 'APP_VERSION\s*=\s*"(?<version>\d+\.\d+\.\d+)"') {
    throw "Could not read APP_VERSION from app_metadata.py."
}
$expectedVersion = $Matches.version
$versionInfo = (Get-Item -LiteralPath $resolvedExe).VersionInfo
if ($versionInfo.ProductVersion -ne $expectedVersion) {
    throw "EXE version mismatch: expected $expectedVersion, found $($versionInfo.ProductVersion)."
}

$packageDir = Split-Path -Parent $resolvedExe
foreach ($relativePath in @("README.md", "CHANGELOG.md", "docs\TROUBLESHOOTING.md")) {
    $documentationPath = Join-Path $packageDir $relativePath
    if (-not (Test-Path -LiteralPath $documentationPath)) {
        throw "Packaged documentation is missing: $relativePath"
    }
}

$smokeRoot = Join-Path ([System.IO.Path]::GetTempPath()) "mkv-track-organizer-smoke-$([guid]::NewGuid())"
$resolvedTempRoot = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath())
$resolvedSmokeRoot = [System.IO.Path]::GetFullPath($smokeRoot)
if (-not $resolvedSmokeRoot.StartsWith($resolvedTempRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Refusing to use a smoke-test directory outside the system temp folder: $resolvedSmokeRoot"
}

$previousAppData = $env:APPDATA
New-Item -ItemType Directory -Path $resolvedSmokeRoot -Force | Out-Null
try {
    $env:APPDATA = $resolvedSmokeRoot
    $process = Start-Process -FilePath $resolvedExe -ArgumentList "--smoke-test" -PassThru -WindowStyle Hidden
    if (-not $process.WaitForExit(30000)) {
        $process.Kill()
        throw "Packaged application smoke test timed out after 30 seconds."
    }
    if ($process.ExitCode -ne 0) {
        throw "Packaged application smoke test failed with exit code $($process.ExitCode)."
    }
} finally {
    $env:APPDATA = $previousAppData
    if (Test-Path -LiteralPath $resolvedSmokeRoot) {
        Remove-Item -LiteralPath $resolvedSmokeRoot -Recurse -Force
    }
}

Write-Host "Smoke test passed: MKV Track Organizer $expectedVersion"
