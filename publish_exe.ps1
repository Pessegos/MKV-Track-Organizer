param(
    [Parameter(Mandatory = $true)]
    [string]$Version,
    [switch]$OneFile,
    [switch]$Draft,
    [switch]$Prerelease,
    [switch]$SkipInstall
)

$ErrorActionPreference = "Stop"

$tag = $Version.Trim()
if (-not $tag) {
    throw "Version is required, for example: .\publish_exe.ps1 -Version v1.0.0"
}
if (-not $tag.StartsWith("v")) {
    $tag = "v$tag"
}

$metadataSource = Get-Content -LiteralPath ".\app_metadata.py" -Raw
if ($metadataSource -notmatch 'APP_VERSION\s*=\s*"(?<version>\d+\.\d+\.\d+)"') {
    throw "Could not read APP_VERSION from app_metadata.py."
}
$expectedTag = "v$($Matches.version)"
if ($tag -ne $expectedTag) {
    throw "Release tag $tag does not match APP_VERSION $($Matches.version). Expected $expectedTag."
}

$gh = Get-Command gh -ErrorAction SilentlyContinue
if (-not $gh) {
    throw "GitHub CLI was not found. Install it, run 'gh auth login', then retry this command."
}

$buildArgs = @()
if ($OneFile) {
    $buildArgs += "-OneFile"
}
if ($SkipInstall) {
    $buildArgs += "-SkipInstall"
}

& .\build_exe.ps1 @buildArgs
if ($LASTEXITCODE -ne 0) {
    throw "EXE build failed with exit code ${LASTEXITCODE}."
}

if ($OneFile) {
    $asset = ".\dist\MKV Track Organizer.exe"
} else {
    $appDir = ".\dist\MKV Track Organizer"
    $asset = ".\dist\MKV Track Organizer-$tag-win-x64.zip"
    if (Test-Path -LiteralPath $asset) {
        Remove-Item -LiteralPath $asset -Force
    }
    Compress-Archive -Path "$appDir\*" -DestinationPath $asset -Force
}

if (-not (Test-Path -LiteralPath $asset)) {
    throw "Release asset was not created: $asset"
}

if (-not $OneFile) {
    & .\smoke_test_exe.ps1
    if ($LASTEXITCODE -ne 0) {
        throw "EXE smoke test failed with exit code ${LASTEXITCODE}."
    }
}

gh release view $tag *> $null
if ($LASTEXITCODE -eq 0) {
    gh release upload $tag $asset --clobber
} else {
    $releaseArgs = @("release", "create", $tag, $asset, "--title", "MKV Track Organizer $tag")
    $releaseNotesFile = ".\.github\release-notes\$tag.md"
    if (Test-Path -LiteralPath $releaseNotesFile) {
        $releaseArgs += @("--notes-file", $releaseNotesFile)
    } else {
        $releaseArgs += "--generate-notes"
    }
    if ($Draft) {
        $releaseArgs += "--draft"
    }
    if ($Prerelease) {
        $releaseArgs += "--prerelease"
    }
    gh @releaseArgs
}
if ($LASTEXITCODE -ne 0) {
    throw "GitHub release publish failed with exit code ${LASTEXITCODE}."
}

Write-Host "Published $asset to GitHub release $tag."
