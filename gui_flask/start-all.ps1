# MYKRO Full Stack Launcher — starts model servers + Gateway API + optional GUI
# Usage:
#   .\start-all.ps1                    # Start everything
#   .\start-all.ps1 -NoGUI             # Headless (servers + gateway only)
#   .\start-all.ps1 -NoSmallModel      # Skip small model server
#   .\start-all.ps1 -GatewayOnly       # Only start Gateway API (models already running)
param(
    [switch]$NoGUI,
    [switch]$NoSmallModel,
    [switch]$GatewayOnly,
    [ValidateSet('mykro-auto', 'mykro-large', 'mykro-small')]
    [string]$Model = 'mykro-auto',
    [switch]$Debug
)

$here = Split-Path -Parent $MyInvocation.MyCommand.Definition
Set-Location $here
$projectRoot = Split-Path -Parent $here

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
Write-Host "Installing requirements..."
& $python -m pip install -r requirements.txt --quiet

# ── Circuit Gate Helper ─────────────────────────────────────────────────────
function Test-Circuit {
    param(
        [string]$circuitId,
        [switch]$Activate
    )
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

# ── Paths ───────────────────────────────────────────────────────────────────
$llamaExe = $env:MYKRO_LLAMA_SERVER
if (-not $llamaExe) {
    try {
        $llamaExe = (Get-Command llama-server.exe -ErrorAction Stop).Source
    } catch {
        $llamaExe = ""
    }
}
$modelDirs = @((Join-Path $projectRoot "model"))
if ($env:MYKRO_MODEL_DIR) {
    $modelDirs += $env:MYKRO_MODEL_DIR
}
$largeModel = Resolve-ModelPath -ModelDirectories $modelDirs -PreferredNames @("Qwen2.5-Coder-7B-Instruct-f16.gguf")
$smallModel = Resolve-ModelPath -ModelDirectories $modelDirs -PreferredNames @("Qwen2.5-Coder-1.5B-Instruct-Q8_0.gguf")
$gatewayPort = 8090
$startLargeModel = $Model -eq 'mykro-auto' -or $Model -eq 'mykro-large'
$startSmallModel = (-not $NoSmallModel) -and ($Model -eq 'mykro-auto' -or $Model -eq 'mykro-small')

function Wait-ForPort {
    param(
        [int]$port,
        [string]$label,
        [int]$timeout = 120
    )
    Write-Host "Waiting for $label on port $port..."
    $waited = 0
    while ($waited -lt $timeout) {
        Start-Sleep -Seconds 2
        $waited += 2
        try {
            $c = New-Object System.Net.Sockets.TcpClient
            $c.Connect('127.0.0.1', $port)
            $c.Close()
            Write-Host "$label is ready on port $port."
            return $true
        } catch { }
        if ($waited % 10 -eq 0) { Write-Host "  Still waiting... ($waited`s)" }
    }
    Write-Warning "$label did not respond within $timeout`s."
    return $false
}

function Test-Port {
    param(
        [int]$port
    )
    try {
        $c = New-Object System.Net.Sockets.TcpClient
        $c.Connect('127.0.0.1', $port)
        $c.Close()
        return $true
    } catch { return $false }
}

function Resolve-ModelPath {
    param(
        [string[]]$ModelDirectories,
        [string[]]$PreferredNames
    )
    foreach ($modelDirectory in $ModelDirectories) {
        if (-not $modelDirectory -or -not (Test-Path $modelDirectory)) {
            continue
        }
        foreach ($modelName in $PreferredNames) {
            $candidate = Join-Path $modelDirectory $modelName
            if (Test-Path $candidate) {
                return $candidate
            }
        }
        $fallback = Get-ChildItem -Path $modelDirectory -Filter *.gguf -File -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($fallback) {
            return $fallback.FullName
        }
    }
    return $null
}

function Get-PortOwner {
    param(
        [int]$port
    )
    try {
        $conn = Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction Stop | Select-Object -First 1
        if (-not $conn) { return $null }
        $pid = [int]$conn.OwningProcess
        $proc = Get-Process -Id $pid -ErrorAction SilentlyContinue
        $cmd = ""
        try {
            $wmi = Get-CimInstance Win32_Process -Filter "ProcessId=$pid" -ErrorAction SilentlyContinue
            if ($wmi -and $wmi.CommandLine) { $cmd = [string]$wmi.CommandLine }
        } catch { }
        return [pscustomobject]@{
            Port = $port
            Pid = $pid
            Name = if ($proc) { $proc.ProcessName } else { "unknown" }
            CommandLine = $cmd
        }
    } catch {
        return $null
    }
}

function Clear-StalePortOwner {
    param(
        [int]$port,
        [string]$label,
        [string[]]$expectedHints
    )
    $owner = Get-PortOwner $port
    if (-not $owner) {
        Write-Host "Preflight: $label port $port is free."
        return
    }

    Write-Warning "Preflight: $label port $port already in use by PID $($owner.Pid) ($($owner.Name))."
    if ($owner.CommandLine) {
        Write-Host "  CommandLine: $($owner.CommandLine)"
    }

    $safeToStop = $false
    $cmdLower = [string]($owner.CommandLine).ToLowerInvariant()
    foreach ($hint in $expectedHints) {
        if ($cmdLower.Contains([string]$hint)) {
            $safeToStop = $true
            break
        }
    }

    if ($safeToStop) {
        Write-Host "  Preflight: stopping stale process on port $port ..."
        try {
            Stop-Process -Id $owner.Pid -Force -ErrorAction Stop
            Start-Sleep -Milliseconds 350
            if (-not (Test-Port $port)) {
                Write-Host "  Preflight: cleared port $port successfully."
            } else {
                Write-Warning "  Preflight: port $port still appears occupied after stop attempt."
            }
        } catch {
            Write-Warning "  Preflight: failed to stop PID $($owner.Pid): $($_.Exception.Message)"
        }
    } else {
        Write-Warning "  Preflight: leaving port $port as-is (owner is not recognized as MYKRO-managed)."
    }
}

Write-Host ""
Write-Host "=== MYKRO Startup Preflight ==="
if (-not $GatewayOnly) {
    Clear-StalePortOwner $largePort "Large model" @("llama-server", "8080")
    if ($startSmallModel) {
        Clear-StalePortOwner $smallPort "Small model" @("llama-server", "8081")
    }
}
Clear-StalePortOwner $gatewayPort "Gateway" @("gateway.py", "gui_flask\\gateway.py")
Write-Host "==============================="
Write-Host ""

# ── Launch Model Servers ────────────────────────────────────────────────────
if (-not $GatewayOnly) {
    if (-not $llamaExe -or -not (Test-Path $llamaExe)) {
        Write-Warning "llama-server.exe not found at: $llamaExe"
        Write-Host "Install via: winget install ggml.llamacpp"
        exit 1
    }

    if (-not (Test-Path $modelDir) -or -not (Get-ChildItem -Path $modelDir -Filter *.gguf -File -ErrorAction SilentlyContinue)) {
        Write-Warning "No GGUF model files found under: $modelDir"
        Write-Host "The gateway can still start, but all model routes will report unreachable until model files are added."
    }

    # Large model (7B, CPU-only)
    if ($startLargeModel) {
        if (-not (Test-Circuit "CKT-MDL-001")) {
            Write-Host "Skipping large model — circuit tripped."
            $startLargeModel = $false
        } elseif (Test-Port $largePort) {
            Write-Host "Large model already running on port $largePort - skipping."
            Test-Circuit "CKT-MDL-001" -Activate | Out-Null
        } elseif (Test-Path $largeModel) {
            Write-Host "Starting large model server (Qwen2.5-Coder-7B, port $largePort)..."
            $largeArgs = "-m `"$largeModel`" --port $largePort --host 127.0.0.1 -c 8192 -t 20 -tb 40 -ngl 0 --device none --numa distribute"
            Start-Process -FilePath $llamaExe -ArgumentList $largeArgs -WindowStyle Minimized
            Wait-ForPort $largePort "Large model" 120
            Test-Circuit "CKT-MDL-001" -Activate | Out-Null
        } else {
            Write-Warning "Large model not found: $largeModel"
        }
    }

    # Small model (1.5B, GPU if available)
    if ($startSmallModel) {
        if (-not (Test-Circuit "CKT-MDL-002")) {
            Write-Host "Skipping small model — circuit tripped."
            $startSmallModel = $false
        } elseif (Test-Port $smallPort) {
            Write-Host "Small model already running on port $smallPort - skipping."
            Test-Circuit "CKT-MDL-002" -Activate | Out-Null
        } elseif (Test-Path $smallModel) {
            Write-Host "Starting small model server (Qwen2.5-Coder-1.5B, port $smallPort)..."
            $smallArgs = "-m `"$smallModel`" --port $smallPort --host 127.0.0.1 -c 4096 -t 20 -tb 40 -ngl 0 --device none --numa distribute"
            Start-Process -FilePath $llamaExe -ArgumentList $smallArgs -WindowStyle Minimized
            Wait-ForPort $smallPort "Small model" 60
            Test-Circuit "CKT-MDL-002" -Activate | Out-Null
        } else {
            Write-Host "Small model not found at: $smallModel"
            Write-Host "Download from HuggingFace and place in the model/ directory."
            Write-Host "Continuing with large model only..."
        }
    }

}

# ── Launch Gateway API ──────────────────────────────────────────────────────
if (-not (Test-Circuit "CKT-GW-001")) {
    Write-Warning "Gateway circuit is blocked — cannot start."
    exit 1
}
if (Test-Port $gatewayPort) {
    Write-Host "Gateway API already running on port $gatewayPort - skipping."
    Test-Circuit "CKT-GW-001" -Activate | Out-Null
} else {
    Write-Host "Starting MYKRO Gateway API on port $gatewayPort..."
    Start-Process -FilePath $python -ArgumentList "gateway.py" -WorkingDirectory $here -WindowStyle Minimized
    Wait-ForPort $gatewayPort "Gateway API" 15
    Test-Circuit "CKT-GW-001" -Activate | Out-Null
}

# ── Health Check ────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "=== MYKRO System Status ==="
try {
    $health = Invoke-RestMethod -Uri "http://127.0.0.1:$gatewayPort/health" -TimeoutSec 15
    Write-Host "Gateway: $($health.gateway)"
    foreach ($m in $health.models.PSObject.Properties) {
        Write-Host "  $($m.Name): $($m.Value)"
    }
} catch {
    Write-Warning "Could not reach Gateway API. Check logs."
}
Write-Host ""
Write-Host "Gateway API: http://127.0.0.1:$gatewayPort"
Write-Host "Models API:  http://127.0.0.1:$gatewayPort/v1/models"
Write-Host "Chat API:    http://127.0.0.1:$gatewayPort/v1/chat/completions"
Write-Host ""

# ── Optional GUI ────────────────────────────────────────────────────────────
if (-not $NoGUI) {
    if (-not (Test-Circuit "CKT-GUI-001")) {
        Write-Warning "GUI circuit is blocked — skipping GUI launch."
    } else {
        $guiArgs = 'orchestrator_tk.py'
        if ($Debug) { $guiArgs += ' --debug' }
        Write-Host "Starting MYKRO GUI..."
        Test-Circuit "CKT-GUI-001" -Activate | Out-Null
        & $python $guiArgs
    }
}
