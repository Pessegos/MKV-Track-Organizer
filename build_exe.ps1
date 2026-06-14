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

$mode = if ($OneFile) { "--onefile" } else { "--onedir" }

Invoke-Python -Arguments @(
    "-m", "PyInstaller",
    "--noconfirm",
    "--clean",
    "--log-level", "WARN",
    "--windowed",
    "--name", "MKV Track Organizer",
    $mode,
    ".\mkv_track_organizer_gui.py"
)

Write-Host ""
Write-Host "Build complete."
if ($OneFile) {
    Write-Host "Executable: .\dist\MKV Track Organizer.exe"
} else {
    Write-Host "Executable: .\dist\MKV Track Organizer\MKV Track Organizer.exe"
}
