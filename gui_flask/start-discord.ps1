$ErrorActionPreference = 'Stop'

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$WorkspaceRoot = Split-Path -Parent $ScriptDir
$VenvDir = Join-Path $WorkspaceRoot ".venv"

if (-not (Test-Path $VenvDir)) {
    Write-Host "Creating Python virtual environment..."
    python -m venv $VenvDir
}

$ActivateScript = Join-Path $VenvDir "Scripts\Activate.ps1"
& $ActivateScript

Write-Host "Installing dependencies..."
pip install -r (Join-Path $ScriptDir "requirements.txt") -q

# Add discord.py on demand if someone uses just this launcher
pip install "discord.py>=2.3.0" -q

Write-Host "Starting MYKRO Discord Bot..."
python (Join-Path $ScriptDir "discord_bot.py")
