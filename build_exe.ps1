param(
    [switch]$OneFile,
    [switch]$SkipInstall,
    [string]$Python = ""
)

$ErrorActionPreference = "Stop"

function Get-PythonRunner {
    $candidates = @()
    if ($Python) {
        $candidates += ,@($Python)
    } else {
        $candidates += ,@("py", "-3.12")
        $candidates += ,@("py", "-3.11")
        $candidates += ,@("py", "-3.10")
        $candidates += ,@("python")
    }

    foreach ($candidate in $candidates) {
        $exe = $candidate[0]
        $candidateArgs = @()
        if ($candidate.Count -gt 1) {
            $candidateArgs = @($candidate[1..($candidate.Count - 1)])
        }

        try {
            $versionText = & $exe @candidateArgs "-c" "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')"
            if ($LASTEXITCODE -ne 0) {
                continue
            }

            $version = [version]$versionText.Trim()
            if ($version -lt [version]"3.10.1") {
                Write-Host "Skipping Python $version from '$($candidate -join ' ')'. Python 3.10.0 breaks PyInstaller analysis for this project."
                continue
            }

            return [pscustomobject]@{
                Exe = $exe
                Args = $candidateArgs
                Version = $version
                Display = ($candidate -join " ")
            }
        } catch {
            continue
        }
    }

    throw "No supported Python found. Install Python 3.12 or 3.11, or pass -Python with a Python 3.10.1+ executable path."
}

$pythonRunner = Get-PythonRunner
Write-Host "Using Python $($pythonRunner.Version): $($pythonRunner.Display)"

function Invoke-Python {
    param([string[]]$Arguments)

    & $pythonRunner.Exe @($pythonRunner.Args + $Arguments)
    if ($LASTEXITCODE -ne 0) {
        throw "Python command failed with exit code ${LASTEXITCODE}: $($Arguments -join ' ')"
    }
}

if (-not $SkipInstall) {
    Invoke-Python -Arguments @("-m", "pip", "install", "-r", "requirements.txt")
    Invoke-Python -Arguments @("-m", "pip", "install", "pyinstaller")
}

$appVersionOutput = @(Invoke-Python -Arguments @("-c", "from app_metadata import APP_VERSION; print(APP_VERSION)"))
$appVersion = ([string]$appVersionOutput[-1]).Trim()
if ($appVersion -notmatch '^\d+\.\d+\.\d+$') {
    throw "Invalid APP_VERSION in app_metadata.py: $appVersion"
}
$versionParts = @($appVersion.Split('.') | ForEach-Object { [int]$_ })
$fileVersion = "$appVersion.0"
$versionTuple = "$($versionParts[0]), $($versionParts[1]), $($versionParts[2]), 0"
$versionInfoPath = Join-Path ([System.IO.Path]::GetTempPath()) "mkv_track_organizer_version_$PID.txt"
$versionInfo = @"
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=($versionTuple),
    prodvers=($versionTuple),
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable('040904B0', [
        StringStruct('CompanyName', 'MKV Track Organizer contributors'),
        StringStruct('FileDescription', 'MKV Track Organizer desktop application'),
        StringStruct('FileVersion', '$fileVersion'),
        StringStruct('InternalName', 'MKV Track Organizer'),
        StringStruct('OriginalFilename', 'MKV Track Organizer.exe'),
        StringStruct('ProductName', 'MKV Track Organizer'),
        StringStruct('ProductVersion', '$appVersion')
      ])
    ]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"@
Set-Content -LiteralPath $versionInfoPath -Value $versionInfo -Encoding UTF8

$mode = if ($OneFile) { "--onefile" } else { "--onedir" }

try {
    Invoke-Python -Arguments @(
        "-m", "PyInstaller",
        "--noconfirm",
        "--clean",
        "--log-level", "WARN",
        "--windowed",
        "--name", "MKV Track Organizer",
        "--version-file", $versionInfoPath,
        "--manifest", ".\windows_app.manifest",
        $mode,
        ".\mkv_track_organizer_gui.py"
    )
} finally {
    Remove-Item -LiteralPath $versionInfoPath -Force -ErrorAction SilentlyContinue
}

$documentationTarget = if ($OneFile) { ".\dist" } else { ".\dist\MKV Track Organizer" }
Copy-Item -LiteralPath ".\README.md" -Destination $documentationTarget -Force
Copy-Item -LiteralPath ".\CHANGELOG.md" -Destination $documentationTarget -Force
$docsTarget = Join-Path $documentationTarget "docs"
New-Item -ItemType Directory -Path $docsTarget -Force | Out-Null
Copy-Item -LiteralPath ".\docs\TROUBLESHOOTING.md" -Destination $docsTarget -Force

Write-Host ""
Write-Host "Build complete: MKV Track Organizer $appVersion"
if ($OneFile) {
    Write-Host "Executable: .\dist\MKV Track Organizer.exe"
} else {
    Write-Host "Executable: .\dist\MKV Track Organizer\MKV Track Organizer.exe"
}
