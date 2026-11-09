# MYKRO tier setup. Writes %LOCALAPPDATA%\MYKRO\config.json and exits.
# Launch the app separately (python orch.py, or dist\MYKRO.exe) after this succeeds.
$root = Split-Path -Parent $MyInvocation.MyCommand.Definition
Set-Location $root

$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    $python = "python"
}

& $python -m gui_flask.setup_menu
exit $LASTEXITCODE
