# MYKRO Web GUI Launcher
# Starts Flask web UI and opens browser.
# Usage:
#   .\start-web.ps1
#   .\start-web.ps1 -NoBrowser
#   .\start-web.ps1 -WithGateway
param(
    [switch]$NoBrowser,
    [switch]$WithGateway,
    [switch]$Debug
)

$here = Split-Path -Parent $MyInvocation.MyCommand.Definition
Set-Location $here

# ── Python environment ──────────────────────────────────────────────────────
if ($env:MYKRO_USE_SYSTEM_PYTHON -ne '1') {
    # README and run_mykro.py use the repository-root .venv. Older copies of
    # this launcher created gui_flask\.venv instead; keep using that if it
    # is the only environment present.
    $repoRoot = Split-Path -Parent $here
    $venv = $null
    foreach ($candidate in @((Join-Path $repoRoot '.venv'), (Join-Path $here '.venv'))) {
        if (Test-Path (Join-Path $candidate 'Scripts\python.exe')) {
            $venv = $candidate
            break
        }
    }
    if (-not $venv) {
        $venv = Join-Path $repoRoot '.venv'
        Write-Host "Creating virtual environment at $venv ..."
        python -m venv $venv
    }
    Write-Host "Activating $venv"
    & (Join-Path $venv 'Scripts\Activate.ps1')
}

try {
    $python = (Get-Command python -ErrorAction Stop).Source
} catch {
    Write-Error "Python not found. Set MYKRO_USE_SYSTEM_PYTHON=1 to use system Python or install Python."
    exit 1
}

Write-Host "Installing requirements (if needed)..."
& $python -m pip install -r requirements.txt --quiet

function Wait-ForPort($port, $label, $timeout = 25) {
    $waited = 0
    while ($waited -lt $timeout) {
        Start-Sleep -Seconds 1
        $waited += 1
        try {
            $c = New-Object System.Net.Sockets.TcpClient
            $c.Connect('127.0.0.1', $port)
            $c.Close()
            Write-Host "$label is ready on port $port."
            return $true
        } catch { }
    }
    Write-Warning "$label did not respond within $timeout seconds."
    return $false
}

function Test-Port($port) {
    try {
        $c = New-Object System.Net.Sockets.TcpClient
        $c.Connect('127.0.0.1', $port)
        $c.Close()
        return $true
    } catch { return $false }
}

# Optional: start Gateway too (for wiring + /v1/chat/completions path)
if ($WithGateway) {
    if (Test-Port 8090) {
        Write-Host "Gateway already running on port 8090 - skipping."
    } else {
        Write-Host "Starting MYKRO Gateway API on port 8090..."
        Start-Process -FilePath $python -ArgumentList "gateway.py" -WorkingDirectory $here -WindowStyle Minimized
        Wait-ForPort 8090 "Gateway API" 20 | Out-Null
    }
}

# Start Flask Web GUI
if (Test-Port 5000) {
    Write-Host "Web GUI already running on http://127.0.0.1:5000"
} else {
    Write-Host "Starting MYKRO Web GUI on http://127.0.0.1:5000 ..."
    $runArgs = 'web_ui.py'
    if ($Debug) {
        & $python $runArgs
        exit $LASTEXITCODE
    }
    Start-Process -FilePath $python -ArgumentList $runArgs -WorkingDirectory $here -WindowStyle Minimized
    Wait-ForPort 5000 "Web GUI" 20 | Out-Null
}

if (-not $NoBrowser) {
    Start-Process "http://127.0.0.1:5000"
}

Write-Host ""
Write-Host "Web GUI: http://127.0.0.1:5000"
Write-Host "Gateway: http://127.0.0.1:8090 (optional; use -WithGateway to auto-start)"
Write-Host ""
Write-Host "Tip: Run '.\start-desktop.ps1' for the Tk desktop GUI window."
Write-Host "     Run '.\start-web.ps1 -WithGateway' for full wiring."
