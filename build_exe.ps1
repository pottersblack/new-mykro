param(
    [switch]$Desktop,
    [switch]$Launcher,
    [switch]$NoClean
)

$root = Split-Path -Parent $MyInvocation.MyCommand.Definition
$python = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) {
    Write-Error "Python executable not found at $python. Create the .venv first."
    exit 1
}

$script = Join-Path $root 'build_exe.py'
$arguments = @()
if ($Desktop) { $arguments += '--desktop' }
if ($Launcher) { $arguments += '--launcher' }
if ($NoClean) { $arguments += '--no-clean' }

Write-Host "Building MYKRO executables..."
& $python $script @arguments
