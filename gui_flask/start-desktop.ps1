# Launcher for the MYKRO desktop GUI + local llama-server model backend
# param() MUST be the first statement in the script — do NOT move it
param(
    [switch]$Debug,       # pass --debug to the GUI for verbose logging
    [switch]$NoModel      # skip launching llama-server (if already running)
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

try {
    $python = (Get-Command python -ErrorAction Stop).Source
} catch {
    Write-Error "Python not found. Set MYKRO_USE_SYSTEM_PYTHON=1 to use system Python or install Python."
    exit 1
}

Write-Host "Installing requirements (if needed)..."
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

# Note: Desktop automation (Automation tab) requires Tesseract OCR for screen reading.
# Install from: https://github.com/UB-Mannheim/tesseract/wiki
# The Automation tab still works without it (OCR is skipped).

# ── llama-server ────────────────────────────────────────────────────────────
$llamaExe = $env:MYKRO_LLAMA_SERVER
if (-not $llamaExe) {
    try {
        $llamaExe = (Get-Command llama-server.exe -ErrorAction Stop).Source
    } catch {
        $llamaExe = ""
    }
}
$modelDirs = @(
    (Join-Path $projectRoot "model")
)
if ($env:MYKRO_MODEL_DIR) {
    $modelDirs += $env:MYKRO_MODEL_DIR
}

function Resolve-DesktopModelPath {
    param(
        [string[]]$ModelDirectories
    )

    $preferredModels = @(
        'Qwen2.5-Coder-7B-Instruct-f16.gguf',
        'Qwen2.5-Coder-1.5B-Instruct-Q8_0.gguf'
    )

    foreach ($modelDirectory in $ModelDirectories) {
        if (-not $modelDirectory -or -not (Test-Path $modelDirectory)) {
            continue
        }
        foreach ($modelName in $preferredModels) {
            $candidate = Join-Path $modelDirectory $modelName
            if (Test-Path $candidate) {
                return $candidate
            }
        }
    }

    foreach ($modelDirectory in $ModelDirectories) {
        if (Test-Path $modelDirectory) {
            $fallback = Get-ChildItem -Path $modelDirectory -Filter *.gguf -File | Select-Object -First 1
            if ($fallback) {
                return $fallback.FullName
            }
        }
    }

    return $null
}

$modelPath = Resolve-DesktopModelPath -ModelDirectories $modelDirs
$llamaPort = 8080

if (-not $NoModel) {
    # Determine which circuit this model maps to
    $modelCircuit = "CKT-MDL-001"
    if ($modelPath -and $modelPath -match "1\.5B") { $modelCircuit = "CKT-MDL-002" }

    if (-not (Test-Circuit $modelCircuit)) {
        Write-Host "Model circuit $modelCircuit is blocked — skipping model launch."
        $NoModel = $true
    }
}

if (-not $NoModel) {
    # Check whether llama-server is already accepting connections on the port
    $alreadyUp = $false
    try {
        $conn = New-Object System.Net.Sockets.TcpClient
        $conn.Connect('127.0.0.1', $llamaPort)
        $conn.Close()
        $alreadyUp = $true
    } catch { }

    if ($alreadyUp) {
        Write-Host "llama-server already running on port $llamaPort — skipping launch."
        Test-Circuit $modelCircuit -Activate | Out-Null
    } else {
        if (-not $llamaExe -or -not (Test-Path $llamaExe)) {
            Write-Warning "llama-server.exe not found at:`n  $llamaExe`nSet -NoModel to skip, or install llama.cpp via WinGet."
        } elseif (-not $modelPath) {
            Write-Warning "No GGUF model file found under any configured model directory:`n  $($modelDirs -join "`n  ")`nDesktop GUI will still open. Add a model file or configure a live endpoint in Settings."
        } else {
            $modelName = Split-Path $modelPath -Leaf
            Write-Host "Starting llama-server ($modelName, port $llamaPort)..."
            # -t 20  = one thread per physical core (10 cores × 2 sockets)
            # -tb 40 = one thread per logical processor (hyper-threading, 20 HT × 2 sockets)
            # --numa distribute = spread threads evenly across both CPU sockets (NUMA nodes)
            # --device none = disable Vulkan GPU backend entirely (CPU-only)
            $llamaArgs = "-m `"$modelPath`" --port $llamaPort --host 127.0.0.1 -c 4096 -t 20 -tb 40 -ngl 0 --device none --numa distribute"
            Start-Process -FilePath $llamaExe -ArgumentList $llamaArgs -WindowStyle Minimized

            # Wait up to 120 s for the server to accept connections
            Write-Host "Waiting for model server to load (16 GB model — may take 30-60 s)..."
            $waited = 0
            $ready  = $false
            while ($waited -lt 120) {
                Start-Sleep -Seconds 2
                $waited += 2
                try {
                    $c = New-Object System.Net.Sockets.TcpClient
                    $c.Connect('127.0.0.1', $llamaPort)
                    $c.Close()
                    $ready = $true
                    break
                } catch { }
                Write-Host "  Still loading... ($waited s)"
            }
            if ($ready) {
                Write-Host "Model server is ready."
                Test-Circuit $modelCircuit -Activate | Out-Null
            } else {
                Write-Warning "Model server did not respond within 120 s. GUI will still open; check logs."
            }
        }
    }
}

# ── GUI ─────────────────────────────────────────────────────────────────────
if (-not (Test-Circuit "CKT-GUI-001")) {
    Write-Warning "GUI circuit is blocked — cannot start."
    exit 1
}
$guiArgs = 'orchestrator_tk.py'
if ($Debug) { $guiArgs += ' --debug' }

Write-Host "Starting MYKRO GUI (orchestrator_tk.py)..."
Test-Circuit "CKT-GUI-001" -Activate | Out-Null
& $python $guiArgs
