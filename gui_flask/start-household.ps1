# MYKRO Household Chat Launcher
# Starts the household web GUI on port 5001, accessible to all devices on your LAN.
# Requires: Gateway + model servers already running (use start-all.ps1 first).
#
# Usage:
#   .\start-household.ps1              # Start on default port 5001
#   .\start-household.ps1 -Port 8888   # Custom port
param(
    [int]$Port = 5001
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

$python = (Get-Command python -ErrorAction Stop).Source
& $python -m pip install -r requirements.txt --quiet

# ── Circuit Gate Helper ─────────────────────────────────────────────────────
function Test-Circuit($circuitId, [switch]$Activate) {
    $args_ = @("circuit_check.py", $circuitId)
    if ($Activate) { $args_ += "--activate" }
    $raw = & $python @args_ 2>$null
    $result = $raw | ConvertFrom-Json -ErrorAction SilentlyContinue
    if ($LASTEXITCODE -ne 0) {
        $reason = if ($result.reason) { " ($($result.reason))" } else { "" }
        Write-Warning "Circuit $circuitId is BLOCKED: $($result.status)$reason"
        return $false
    }
    if ($result.label) { Write-Host "Circuit $circuitId ($($result.label)) — OK" }
    return $true
}

# ── Check Household Circuit ─────────────────────────────────────────────────
if (-not (Test-Circuit "CKT-HH-001")) {
    Write-Warning "Household circuit is blocked — cannot start."
    exit 1
}

# ── Check Gateway ───────────────────────────────────────────────────────────
$gatewayUp = $false
try {
    $c = New-Object System.Net.Sockets.TcpClient
    $c.Connect('127.0.0.1', 8090)
    $c.Close()
    $gatewayUp = $true
} catch {}

if (-not $gatewayUp) {
    Write-Warning "MYKRO Gateway not detected on port 8090."
    Write-Host "Start it first with: .\start-all.ps1"
    Write-Host "Continuing anyway — the chat will show errors until the gateway is up."
}

# ── Get LAN IP ──────────────────────────────────────────────────────────────
$lanIP = (Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.InterfaceAlias -notmatch 'Loopback' -and $_.IPAddress -ne '127.0.0.1' } | Select-Object -First 1).IPAddress
if ($lanIP) {
    Write-Host ""
    Write-Host "=== MYKRO Household Chat ==="
    Write-Host "Local:   http://localhost:$Port"
    Write-Host "Network: http://${lanIP}:$Port"
    Write-Host ""
    Write-Host "Share the Network URL with household members."
    Write-Host "Chat logs saved to: logs\household_chat\"
    Write-Host ""
} else {
    Write-Host "Starting Household Chat on http://localhost:$Port"
}

# ── Firewall rule ───────────────────────────────────────────────────────────
$ruleName = "MYKRO Household Chat (port $Port)"
$existing = Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue
if (-not $existing) {
    Write-Host "Creating Windows Firewall rule for port $Port..."
    try {
        New-NetFirewallRule -DisplayName $ruleName -Direction Inbound -Action Allow `
            -Protocol TCP -LocalPort $Port -Profile Private,Domain `
            -Description "Allow LAN access to MYKRO Household Chat" | Out-Null
        Write-Host "Firewall rule created."
    } catch {
        Write-Warning "Could not create firewall rule (may need admin). Other devices might not connect."
        Write-Host "Run this script as Administrator, or manually allow TCP port $Port in Windows Firewall."
    }
}

# ── Launch ──────────────────────────────────────────────────────────────────
Test-Circuit "CKT-HH-001" -Activate | Out-Null
& $python household.py --port $Port --host 0.0.0.0
