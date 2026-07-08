$ErrorActionPreference = "SilentlyContinue"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = Split-Path -Parent $ScriptDir
$LogFile = Join-Path $Root "data\logs\startup.log"

function Write-StartupLog {
    param([string]$Message)
    Write-Host $Message
    try {
        $logDir = Split-Path -Parent $LogFile
        if (-not (Test-Path $logDir)) {
            New-Item -ItemType Directory -Path $logDir -Force | Out-Null
        }
        Add-Content -Path $LogFile -Value $Message -Encoding UTF8
    } catch {
    }
}

function Test-PythonCandidate {
    param(
        [string]$File,
        [string[]]$Args = @()
    )
    if ([string]::IsNullOrWhiteSpace($File)) {
        return $null
    }
    if ($File -match "^[A-Za-z]:\\" -and -not (Test-Path $File)) {
        return $null
    }
    $code = "import sys; print(sys.executable); print('{}.{}.{}'.format(*sys.version_info[:3])); raise SystemExit(0 if sys.version_info >= (3, 10) else 1)"
    try {
        $output = & $File @Args -c $code 2>$null
        if ($LASTEXITCODE -eq 0) {
            return [pscustomobject]@{
                File = $File
                Args = $Args
                Exe = ($output | Select-Object -First 1)
                Version = ($output | Select-Object -Skip 1 -First 1)
            }
        }
    } catch {
    }
    return $null
}

$candidates = New-Object System.Collections.ArrayList
$seen = @{}

function Add-Candidate {
    param(
        [string]$File,
        [string[]]$Args = @()
    )
    if ([string]::IsNullOrWhiteSpace($File)) {
        return
    }
    $key = "$File|$($Args -join ' ')"
    if ($seen.ContainsKey($key)) {
        return
    }
    $seen[$key] = $true
    [void]$candidates.Add([pscustomobject]@{ File = $File; Args = $Args })
}

# Python launcher and PATH commands.
foreach ($v in @("3.14", "3.13", "3.12", "3.11", "3.10", "3")) {
    Add-Candidate "py" @("-$v")
}
foreach ($cmd in @("python", "python3", "python3.14", "python3.13", "python3.12", "python3.11", "python3.10")) {
    Add-Candidate $cmd @()
}

# Python.org installer registry entries.
$registryRoots = @(
    "HKCU:\Software\Python\PythonCore",
    "HKLM:\Software\Python\PythonCore",
    "HKLM:\Software\WOW6432Node\Python\PythonCore"
)
foreach ($rootKey in $registryRoots) {
    foreach ($versionKey in Get-ChildItem $rootKey -ErrorAction SilentlyContinue) {
        $installPathKey = Join-Path $versionKey.PSPath "InstallPath"
        $props = Get-ItemProperty -Path $installPathKey -ErrorAction SilentlyContinue
        if ($props.ExecutablePath) {
            Add-Candidate $props.ExecutablePath @()
        }
        $installKey = Get-Item -Path $installPathKey -ErrorAction SilentlyContinue
        if ($installKey) {
            $installDir = $installKey.GetValue("")
            if ($installDir) {
                Add-Candidate (Join-Path $installDir "python.exe") @()
            }
        }
    }
}

# Common install locations when PATH and py launcher are unavailable.
$versionDirs = @("Python314", "Python313", "Python312", "Python311", "Python310")
$baseDirs = @(
    (Join-Path $env:LOCALAPPDATA "Programs\Python"),
    (Join-Path $env:LOCALAPPDATA "Python"),
    $env:ProgramFiles,
    ${env:ProgramFiles(x86)}
)
foreach ($base in $baseDirs) {
    if ([string]::IsNullOrWhiteSpace($base)) {
        continue
    }
    foreach ($dir in $versionDirs) {
        Add-Candidate (Join-Path (Join-Path $base $dir) "python.exe") @()
    }
}

$python = $null
foreach ($candidate in $candidates) {
    $python = Test-PythonCandidate $candidate.File $candidate.Args
    if ($python) {
        break
    }
}

if (-not $python) {
    Write-StartupLog "[ERROR] Python 3.10+ was not found."
    Write-StartupLog ""
    Write-StartupLog "Checked py launcher, python/python3 commands, Windows registry, and common install folders."
    Write-StartupLog "If Python 3.13 is installed, install the Windows x64 version or enable Add python.exe to PATH."
    Write-StartupLog "Download: https://www.python.org/downloads/windows/"
    exit 1
}

Write-StartupLog "[AutoGameTest] Using Python $($python.Version): $($python.Exe)"

$launch = Join-Path $Root "tools\launch.py"
& $python.File @($python.Args) $launch
exit $LASTEXITCODE
