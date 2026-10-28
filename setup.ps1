# One command after clone: A / B / C / D, desktop icon, build MYKRO.exe, launch it.
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $MyInvocation.MyCommand.Path)

function Find-Python {
    foreach ($name in @("py", "python")) {
        if (Get-Command $name -ErrorAction SilentlyContinue) {
            return $name
        }
    }
    return $null
}

$python = Find-Python
if (-not $python) {
    Write-Host "Python is not on PATH. Installing Python 3.12 with winget..."
    winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements
    $candidate = Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"
    if (Test-Path $candidate) {
        & $candidate setup.py @args
        exit $LASTEXITCODE
    }
    Write-Host "Install Python 3.12 from https://www.python.org/downloads/windows/ and run this script again."
    exit 1
}

& $python setup.py @args
exit $LASTEXITCODE
