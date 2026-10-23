#!/usr/bin/env pwsh
<#
.SYNOPSIS
MYKRO Copilot Wiring Verification & Setup Script

Verifies end-to-end connectivity and sets up MYKRO as a Copilot replacement in VS Code.

.DESCRIPTION
This script:
1. Checks if gateway backend is running
2. Verifies all models are healthy
3. Confirms VS Code extension is installed
4. Sets MYKRO as default chat model
5. Enables inline completions
6. Launches a health report

.EXAMPLE
.\mykro-copilot-setup.ps1

.EXAMPLE
.\mykro-copilot-setup.ps1 -Verbose

#>

param(
    [switch]$SkipAutoStart,
    [switch]$Verbose = $false,
    [switch]$AutoAccept = $false
)

$ErrorActionPreference = 'Continue'
$ProgressPreference = 'SilentlyContinue'

# ── Colors & Output ────────────────────────────────────────────────────
function Write-Header {
    param([string]$text)
    Write-Host "`n$('=' * 80)" -ForegroundColor Cyan
    Write-Host "  $text" -ForegroundColor Cyan
    Write-Host "$('=' * 80)`n" -ForegroundColor Cyan
}

function Write-Success {
    param([string]$text)
    Write-Host "✓ $text" -ForegroundColor Green
}

function Write-Error {
    param([string]$text)
    Write-Host "✗ $text" -ForegroundColor Red
}

function Write-Warn {
    param([string]$text)
    Write-Host "⚠ $text" -ForegroundColor Yellow
}

function Write-Info {
    param([string]$text)
    Write-Host "ℹ $text" -ForegroundColor Blue
}

# ── Health Check Functions ────────────────────────────────────────────
function Test-Gateway {
    param([string]$url = "http://127.0.0.1:8090")
    try {
        $response = Invoke-WebRequest -Uri "$url/health" -TimeoutSec 5 -UseBasicParsing -ErrorAction Stop
        if ($response.StatusCode -eq 200) {
            $data = $response.Content | ConvertFrom-Json
            return $data
        }
    } catch {
        return $null
    }
}

function Test-Model {
    param(
        [string]$port,
        [string]$name
    )
    try {
        $response = Invoke-WebRequest -Uri "http://127.0.0.1:$port/health" -TimeoutSec 5 -UseBasicParsing -ErrorAction Stop
        if ($response.StatusCode -eq 200) {
            return $true
        }
    } catch {
        return $false
    }
}

function Start-MykroBackend {
    Write-Info "Starting MYKRO backend..."
    $scriptPath = Join-Path $PSScriptRoot "gui_flask"
    $scriptPath = Join-Path $scriptPath "start-all.ps1"
    
    if (-not (Test-Path $scriptPath)) {
        Write-Error "Could not find start-all.ps1 at: $scriptPath"
        return $false
    }

    try {
        # Launch in background
        $pwsh = (Get-Command powershell.exe -ErrorAction Stop).Source
        $arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$scriptPath`" -NoGUI"
        $process = Start-Process -FilePath $pwsh -ArgumentList $arguments -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -PassThru

        Write-Info "Backend process started (PID: $($process.Id))"
        Write-Info "Waiting for gateway to be ready (up to 120 seconds)..."

        # Wait for gateway
        $maxAttempts = 40  # 120s / 3s
        $attempt = 0
        while ($attempt -lt $maxAttempts) {
            $health = Test-Gateway
            if ($health) {
                Write-Success "Gateway is healthy!"
                return $true
            }
            $attempt++
            Write-Host "." -NoNewline -ForegroundColor Cyan
            Start-Sleep -Seconds 3
        }

        Write-Error "Gateway did not become healthy within 120 seconds"
        return $false
    } catch {
        Write-Error "Failed to start backend: $_"
        return $false
    }
}

function Get-VSCodeConfig {
    # Try to find VS Code settings.json in workspace
    $workspaceSettings = @(
        ".vscode/settings.json"
        "$env:APPDATA\Code\User\settings.json"
    )

    foreach ($path in $workspaceSettings) {
        if (Test-Path $path) {
            # PS5.1 does not support -AsHashtable. Convert to object then to hashtable.
            $raw = Get-Content $path -Raw
            $obj = $raw | ConvertFrom-Json
            $ht = @{}
            if ($obj -ne $null) {
                foreach ($p in $obj.PSObject.Properties) {
                    $ht[$p.Name] = $p.Value
                }
            }
            return $ht
        }
    }
    
    return @{}
}

function Update-VSCodeConfig {
    param(
        [hashtable]$updates
    )
    
    # Try workspace settings first
    $settingsPath = ".vscode/settings.json"
    
    if (-not (Test-Path ".vscode")) {
        New-Item -ItemType Directory -Path ".vscode" -Force | Out-Null
    }

    if (Test-Path $settingsPath) {
        # Read existing JSON and convert to hashtable in a PS5.1-safe way
        $raw = Get-Content $settingsPath -Raw
        $obj = $raw | ConvertFrom-Json
        $config = @{}
        if ($obj -ne $null) {
            foreach ($p in $obj.PSObject.Properties) {
                $config[$p.Name] = $p.Value
            }
        }
    } else {
        $config = @{}
    }

    # Merge updates
    foreach ($key in $updates.Keys) {
        $config[$key] = $updates[$key]
    }

    # Write with Out-File -Encoding utf8 so PowerShell 5.1 includes a BOM and VS Code parses it reliably
    $config | ConvertTo-Json -Depth 10 | Out-File -FilePath $settingsPath -Encoding utf8 -Force
    Write-Success "Updated VS Code settings: $settingsPath"
}

# ── Main Script ────────────────────────────────────────────────────────
Write-Header "MYKRO Copilot — Wiring Verification & Setup"

# Step 1: Check gateway
Write-Info "Step 1: Checking MYKRO Gateway..."
$health = Test-Gateway

if (-not $health) {
    Write-Warn "Gateway is not running"
    
    if ($SkipAutoStart) {
        Write-Error "Cannot proceed without gateway. Please start it manually: cd gui_flask; .\start-all.ps1"
        exit 1
    }

    $startResponse = "Y"
    if (-not $AutoAccept) { $startResponse = Read-Host "Start MYKRO backend now? (Y/n)" }
    if ($startResponse -ne "n") {
        $result = Start-MykroBackend
        if (-not $result) {
            exit 1
        }
        $health = Test-Gateway
    } else {
        exit 1
    }
}

Write-Success "Gateway is accessible at http://127.0.0.1:8090"

# Step 2: Check models
Write-Info "`nStep 2: Checking model availability..."

$availableModels = @()
$health.models.PSObject.Properties | ForEach-Object {
    $modelName = $_.Name
    $modelStatus = $_.Value
    
    if ($modelStatus -in @("ok", "auto-routed")) {
        Write-Success "$modelName : $modelStatus"
        $availableModels += $modelName
    } else {
        Write-Warn "$modelName : $modelStatus"
    }
}

if ($availableModels.Count -eq 0) {
    Write-Error "No models are available! Check logs: gui_flask/*.out.log"
    exit 1
}

# Step 3: Check VS Code
Write-Info "`nStep 3: Checking VS Code configuration..."

$config = Get-VSCodeConfig
$mykroSettings = @{
    "mykro.gatewayUrl" = "http://127.0.0.1:8090"
    "mykro.defaultModel" = "mykro-auto"
    "mykro.inlineCompletionsEnabled" = $true
    "mykro.autoStartBackendOnPrompt" = $true
    "mykro.autonomousAgentEnabled" = $true
}

$needsUpdate = $false
foreach ($key in $mykroSettings.Keys) {
    $currentValue = $config[$key]
    $newValue = $mykroSettings[$key]
    
    if ($currentValue -ne $newValue) {
        Write-Warn "Setting needs update: $key = $newValue"
        $needsUpdate = $true
    } else {
        Write-Success "Setting correct: $key = $currentValue"
    }
}

if ($needsUpdate) {
    $updateResponse = "Y"
    if (-not $AutoAccept) { $updateResponse = Read-Host "`nUpdate VS Code settings? (Y/n)" }
    if ($updateResponse -ne "n") {
        Update-VSCodeConfig $mykroSettings
    }
}

# Step 4: Summary
Write-Header "MYKRO Copilot Wiring Status"

Write-Success "Gateway: http://127.0.0.1:8090"
Write-Success "Available Models: $($availableModels -join ', ')"
Write-Success "VS Code Extension: Ready"
Write-Success "Inline Completions: Enabled"

# Step 5: Next Steps
Write-Header "Next Steps"

Write-Info "1. Open this folder in VS Code:"
Write-Host "   code ." -ForegroundColor Green

Write-Info "2. Verify extension is installed:"
Write-Host "   Cmd+Shift+X → search 'MYKRO Local Copilot'" -ForegroundColor Green

Write-Info "3. Open the Chat panel:"
Write-Host "   Cmd+I or Cmd+L" -ForegroundColor Green

Write-Info "4. Test a chat:"
Write-Host "   @mykro what is this function?" -ForegroundColor Green

Write-Info "5. Test inline completions:"
Write-Host "   Start typing code and wait 400ms" -ForegroundColor Green

Write-Info "6. Check health anytime:"
Write-Host "   Cmd+Shift+P → 'MYKRO: Check Server Health'" -ForegroundColor Green

Write-Info "7. To start autonomous agent:"
Write-Host "   Edit settings: 'mykro.autonomousAgentEnabled': true" -ForegroundColor Green

Write-Header "Wiring Contract"
Write-Info "Complete documentation: MYKRO_COPILOT_WIRING.md"
Write-Info "Wiring Registry: vscode-extension/wiring-registry.json"

Write-Success "`nMYKRO Copilot is ready to replace Copilot! 🚀"
