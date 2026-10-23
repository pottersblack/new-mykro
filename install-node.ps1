#Requires -RunAsAdministrator
<#
.SYNOPSIS
    MYKRO Node Agent Installer — run this AS ADMIN on the M70q (Mini PC).
    Sets up a FastAPI tool-executor that the P920 MYKRO gateway can call remotely.

.DESCRIPTION
    1. Probes local hardware specs
    2. Creates C:\mykro-node and writes node_agent.py
    3. Creates a Python venv and installs FastAPI + uvicorn + psutil
    4. Generates a secret auth token
    5. Opens firewall port 8095
    6. Installs a Windows Scheduled Task for auto-start on boot
    7. Starts the agent immediately
    8. Prints the registration command to run on the P920

.NOTES
    After running, take the token printed at the end and add it to
    gui_flask/config.json on the P920 under "nodes".
#>

$ErrorActionPreference = "Stop"

# ─── Config ───────────────────────────────────────────────────────────────────
$NodeDir  = "C:\mykro-node"
$Port     = 8095
$TaskName = "MYKRONodeAgent"

# ─── 1. Probe specs ───────────────────────────────────────────────────────────
Write-Host "`n╔══════════════════════════════════════════╗" -ForegroundColor Cyan
Write-Host "║       MYKRO Node Agent Installer         ║" -ForegroundColor Cyan
Write-Host "╚══════════════════════════════════════════╝`n" -ForegroundColor Cyan

$Model = (Get-CimInstance Win32_ComputerSystem).Model
$CPU   = Get-CimInstance Win32_Processor | Select-Object -First 1
$RAM   = [math]::Round((Get-CimInstance Win32_PhysicalMemory | Measure-Object Capacity -Sum).Sum / 1GB, 1)
$GPU   = (Get-CimInstance Win32_VideoController | Select-Object -First 1).Name
$IP    = (Get-NetIPAddress -AddressFamily IPv4 |
          Where-Object { $_.InterfaceAlias -notmatch 'Loopback|vEthernet' -and
                         $_.IPAddress -notmatch '^169\.' } |
          Select-Object -First 1).IPAddress

Write-Host "  Hostname : $env:COMPUTERNAME"
Write-Host "  Model    : $Model"
Write-Host "  CPU      : $($CPU.Name) [$($CPU.NumberOfCores)C / $($CPU.NumberOfLogicalProcessors)T]"
Write-Host "  RAM      : $RAM GB"
Write-Host "  GPU      : $GPU"
Write-Host "  LAN IP   : $IP"
Write-Host "  Port     : $Port`n"

# ─── 2. Create node directory ─────────────────────────────────────────────────
Write-Host "[1/7] Creating $NodeDir ..." -ForegroundColor Yellow
New-Item -ItemType Directory -Force -Path $NodeDir | Out-Null

# ─── 3. Write node_agent.py ───────────────────────────────────────────────────
Write-Host "[2/7] Writing node_agent.py ..." -ForegroundColor Yellow

$AgentPy = @'
"""
MYKRO Node Agent v1.0 — Remote tool executor for the P920 MYKRO gateway.

Endpoints:
    GET  /ping              — Fast liveness probe (no auth)
    GET  /health            — Status + RAM/CPU usage (no auth)
    GET  /info              — Full hardware spec (auth required)
    POST /execute           — Dispatch a tool call (auth required)
        Body: {"tool": "<name>", "args": {<tool args>}}

Available tools:
    run_command     — Run a shell command (shlex-split, no shell=True)
    read_file       — Read a file (with optional start_line/end_line)
    write_file      — Write content to a file
    list_directory  — List a directory
    file_search     — Glob search under a root path
    get_processes   — List running processes (optional name_filter)
    kill_process    — Terminate a process by PID (requires pid)
    get_specs       — Return CPU/RAM snapshot

Auth: every protected endpoint requires header  X-MYKRO-Token: <token>
The token is stored in C:\\mykro-node\\node_token.txt
"""
from __future__ import annotations

import json
import logging
import os
import platform
import secrets
import shlex
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import psutil
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

# ── Config ────────────────────────────────────────────────────────────────────
PORT      = int(os.environ.get("MYKRO_NODE_PORT", "8095"))
NODE_DIR  = Path(__file__).parent.resolve()
TOKEN_FILE = NODE_DIR / "node_token.txt"
LOG_FILE  = NODE_DIR / "node_agent.log"

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("mykro-node")

# ── Auth token ────────────────────────────────────────────────────────────────
def _load_or_create_token() -> str:
    if TOKEN_FILE.exists():
        return TOKEN_FILE.read_text(encoding="utf-8").strip()
    token = secrets.token_hex(32)
    TOKEN_FILE.write_text(token, encoding="utf-8")
    log.info("Generated new auth token → %s", TOKEN_FILE)
    return token

_NODE_TOKEN = _load_or_create_token()
_START_TIME = time.time()

# ── Safety: blocked paths & commands ─────────────────────────────────────────
_BLOCKED_WRITE_DIRS = {
    "c:\\windows",
    "c:\\program files",
    "c:\\program files (x86)",
}
_BLOCKED_COMMANDS = {
    "format", "deltree", "mkfs", "shutdown", "reboot",
    "del", "rmdir", "rd", "attrib",
}

# ── FastAPI app ───────────────────────────────────────────────────────────────
app = FastAPI(title="MYKRO Node Agent", version="1.0.0")


def _require_auth(request: Request) -> None:
    token = request.headers.get("X-MYKRO-Token", "")
    # constant-time compare to avoid timing side-channel
    if not secrets.compare_digest(token, _NODE_TOKEN):
        raise HTTPException(status_code=401, detail="Unauthorized")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── GET /ping — fast liveness (no auth) ──────────────────────────────────────
@app.get("/ping")
async def ping():
    return {"ok": True, "node": platform.node()}


# ── GET /health — runtime stats (no auth) ────────────────────────────────────
@app.get("/health")
async def health():
    mem = psutil.virtual_memory()
    return {
        "status": "ok",
        "hostname": platform.node(),
        "uptime_s": round(time.time() - _START_TIME, 1),
        "ram_total_gb": round(mem.total / 1e9, 1),
        "ram_free_gb": round(mem.available / 1e9, 1),
        "cpu_percent": psutil.cpu_percent(interval=0.1),
        "ts": _now(),
    }


# ── GET /info — full hardware spec (auth required) ───────────────────────────
@app.get("/info")
async def info(request: Request):
    _require_auth(request)
    mem = psutil.virtual_memory()
    disks = []
    for part in psutil.disk_partitions(all=False):
        if not part.fstype:
            continue
        try:
            usage = psutil.disk_usage(part.mountpoint)
            disks.append({
                "mount": part.mountpoint,
                "total_gb": round(usage.total / 1e9, 1),
                "free_gb": round(usage.free / 1e9, 1),
            })
        except (PermissionError, OSError):
            pass
    return {
        "hostname": platform.node(),
        "os": platform.platform(),
        "cpu_count_logical": psutil.cpu_count(logical=True),
        "cpu_count_physical": psutil.cpu_count(logical=False),
        "ram_total_gb": round(mem.total / 1e9, 1),
        "ram_free_gb": round(mem.available / 1e9, 1),
        "disks": disks,
        "python": platform.python_version(),
    }


# ── POST /execute — tool dispatcher (auth required) ──────────────────────────
class ToolRequest(BaseModel):
    tool: str
    args: dict[str, Any] = {}


@app.post("/execute")
async def execute(req: ToolRequest, request: Request):
    _require_auth(request)
    log.info("execute  tool=%s  args=%s", req.tool, json.dumps(req.args)[:200])
    try:
        result = _dispatch(req.tool, req.args)
        return {"ok": True, "result": result}
    except (ValueError, PermissionError, FileNotFoundError, NotADirectoryError) as exc:
        log.warning("execute client error  tool=%s: %s", req.tool, exc)
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        log.error("execute server error  tool=%s: %s", req.tool, exc)
        return JSONResponse(
            status_code=500,
            content={"ok": False, "error": str(exc)},
        )


# ── Tool implementations ──────────────────────────────────────────────────────
def _dispatch(tool: str, args: dict[str, Any]) -> Any:
    match tool:
        case "run_command":   return _run_command(**args)
        case "read_file":     return _read_file(**args)
        case "write_file":    return _write_file(**args)
        case "list_directory":return _list_directory(**args)
        case "file_search":   return _file_search(**args)
        case "get_processes": return _get_processes(**args)
        case "kill_process":  return _kill_process(**args)
        case "get_specs":     return _get_specs()
        case _:               raise ValueError(f"Unknown tool: {tool!r}")


def _run_command(
    command: str,
    cwd: Optional[str] = None,
    timeout: int = 60,
) -> str:
    parts = shlex.split(command, posix=False)
    if not parts:
        raise ValueError("Empty command")
    first = parts[0].lower().rstrip(".exe").rstrip(".cmd")
    if first in _BLOCKED_COMMANDS:
        raise PermissionError(f"Command blocked: {first!r}")
    result = subprocess.run(
        parts,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=cwd,
        shell=False,
    )
    out = (result.stdout + result.stderr).strip()
    return out[:10000]


def _read_file(
    path: str,
    start_line: Optional[int] = None,
    end_line: Optional[int] = None,
) -> str:
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"File not found: {p}")
    text = p.read_text(encoding="utf-8", errors="replace")
    if start_line is not None or end_line is not None:
        lines = text.splitlines(keepends=True)
        s = (start_line or 1) - 1
        e = end_line or len(lines)
        text = "".join(lines[s:e])
    return text[:60000]


def _write_file(path: str, content: str) -> str:
    p = Path(path).resolve()
    for blocked in _BLOCKED_WRITE_DIRS:
        if str(p).lower().startswith(blocked):
            raise PermissionError(f"Write blocked: inside {blocked}")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return f"Written {len(content)} chars to {p}"


def _list_directory(path: str) -> list[dict]:
    p = Path(path)
    if not p.is_dir():
        raise NotADirectoryError(f"Not a directory: {p}")
    items = []
    for child in sorted(p.iterdir()):
        try:
            stat = child.stat()
            items.append({
                "name": child.name,
                "type": "dir" if child.is_dir() else "file",
                "size_bytes": stat.st_size if child.is_file() else None,
            })
        except OSError:
            pass
    return items


def _file_search(
    root: str,
    pattern: str,
    max_results: int = 100,
) -> list[str]:
    results: list[str] = []
    root_path = Path(root)
    if not root_path.exists():
        raise FileNotFoundError(f"Root path not found: {root_path}")
    for p in root_path.rglob(pattern):
        results.append(str(p))
        if len(results) >= max_results:
            break
    return results


def _get_processes(name_filter: Optional[str] = None) -> list[dict]:
    out: list[dict] = []
    for proc in psutil.process_iter(["pid", "name", "status", "cpu_percent", "memory_info"]):
        try:
            info = proc.info
            if name_filter and name_filter.lower() not in (info.get("name") or "").lower():
                continue
            out.append({
                "pid": info["pid"],
                "name": info.get("name"),
                "status": info.get("status"),
                "cpu_pct": info.get("cpu_percent"),
                "ram_mb": round(info["memory_info"].rss / 1e6, 1)
                          if info.get("memory_info") else None,
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return out[:200]


def _kill_process(pid: int) -> str:
    try:
        proc = psutil.Process(pid)
        name = proc.name()
        proc.terminate()
        return f"Terminated PID {pid} ({name})"
    except psutil.NoSuchProcess:
        raise ValueError(f"PID {pid} not found")


def _get_specs() -> dict:
    mem = psutil.virtual_memory()
    return {
        "hostname": platform.node(),
        "os": platform.platform(),
        "cpu_logical": psutil.cpu_count(logical=True),
        "cpu_physical": psutil.cpu_count(logical=False),
        "ram_total_gb": round(mem.total / 1e9, 1),
        "ram_free_gb": round(mem.available / 1e9, 1),
    }


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    log.info("═" * 60)
    log.info("MYKRO Node Agent v1.0  |  port=%d", PORT)
    log.info("Hostname : %s", platform.node())
    log.info("Token    : %s... (full token in %s)", _NODE_TOKEN[:8], TOKEN_FILE)
    log.info("═" * 60)
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
'@

$AgentPy | Set-Content -Path "$NodeDir\node_agent.py" -Encoding UTF8
Write-Host "    -> $NodeDir\node_agent.py" -ForegroundColor Gray

# ─── 4. Write requirements.txt ────────────────────────────────────────────────
@"
fastapi>=0.110.0
uvicorn[standard]>=0.29.0
psutil>=5.9.0
pydantic>=2.0.0
"@ | Set-Content -Path "$NodeDir\requirements.txt" -Encoding UTF8

# ─── 5. Create venv and install deps ─────────────────────────────────────────
Write-Host "[3/7] Setting up Python venv ..." -ForegroundColor Yellow

$VenvDir  = "$NodeDir\.venv"
$PythonExe = "$VenvDir\Scripts\python.exe"
$PipExe    = "$VenvDir\Scripts\pip.exe"

# Find Python (prefer py launcher, then direct)
$PyCmd = $null
foreach ($candidate in @("py", "python3", "python")) {
    try {
        $ver = & $candidate --version 2>&1
        if ($ver -match "Python 3\.(1[0-9]|[2-9]\d)") {
            $PyCmd = $candidate
            Write-Host "    Using: $candidate ($ver)" -ForegroundColor Gray
            break
        }
    } catch {}
}

if (-not $PyCmd) {
    Write-Host "  ERROR: Python 3.10+ not found. Install from https://python.org" -ForegroundColor Red
    exit 1
}

if (-not (Test-Path $PythonExe)) {
    & $PyCmd -m venv $VenvDir
    Write-Host "    -> venv created at $VenvDir" -ForegroundColor Gray
} else {
    Write-Host "    -> venv already exists, skipping" -ForegroundColor Gray
}

Write-Host "[4/7] Installing Python dependencies ..." -ForegroundColor Yellow
& $PipExe install -q -r "$NodeDir\requirements.txt"
Write-Host "    -> fastapi, uvicorn, psutil installed" -ForegroundColor Gray

# ─── 6. Generate auth token (if not already created by first run) ─────────────
Write-Host "[5/7] Setting up auth token ..." -ForegroundColor Yellow
$TokenFile = "$NodeDir\node_token.txt"
if (-not (Test-Path $TokenFile)) {
    $Token = -join ((97..122) + (48..57) | Get-Random -Count 64 | ForEach-Object {[char]$_})
    $Token | Set-Content -Path $TokenFile -Encoding ASCII -NoNewline
    Write-Host "    -> Token generated" -ForegroundColor Gray
} else {
    Write-Host "    -> Token already exists" -ForegroundColor Gray
}
$Token = Get-Content -Path $TokenFile -Raw

# ─── 7. Open firewall ─────────────────────────────────────────────────────────
Write-Host "[6/7] Opening firewall port $Port ..." -ForegroundColor Yellow
$RuleName = "MYKRO Node Agent (TCP $Port)"
$existing = Get-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue
if (-not $existing) {
    New-NetFirewallRule -DisplayName $RuleName `
        -Direction Inbound `
        -Protocol TCP `
        -LocalPort $Port `
        -Action Allow `
        -Profile Domain,Private | Out-Null
    Write-Host "    -> Firewall rule created" -ForegroundColor Gray
} else {
    Write-Host "    -> Firewall rule already exists" -ForegroundColor Gray
}

# ─── 8. Create helper batch launcher ─────────────────────────────────────────
$StartBat = "$NodeDir\start-node.bat"
@"
@echo off
cd /d C:\mykro-node
.venv\Scripts\python.exe node_agent.py
pause
"@ | Set-Content -Path $StartBat -Encoding ASCII

# ─── 9. Install Scheduled Task (auto-start on boot) ──────────────────────────
Write-Host "[7/7] Installing Scheduled Task '$TaskName' ..." -ForegroundColor Yellow

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

$Action  = New-ScheduledTaskAction `
    -Execute "$PythonExe" `
    -Argument "$NodeDir\node_agent.py" `
    -WorkingDirectory $NodeDir

$Trigger = New-ScheduledTaskTrigger -AtStartup
$Settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0) `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1)

$Principal = New-ScheduledTaskPrincipal `
    -UserId "SYSTEM" `
    -LogonType ServiceAccount `
    -RunLevel Highest

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Principal $Principal `
    -Description "MYKRO Node Agent — remote tool executor for P920 gateway" | Out-Null

Write-Host "    -> Scheduled Task registered (runs as SYSTEM at boot)" -ForegroundColor Gray

# ─── 9b. Enable WinRM for future remote management ───────────────────────────
Write-Host "`n[BONUS] Enabling WinRM for future remote access ..." -ForegroundColor Yellow
try {
    Enable-PSRemoting -Force -SkipNetworkProfileCheck | Out-Null
    Set-Item WSMan:\localhost\Client\TrustedHosts -Value "*" -Force | Out-Null
    Set-ItemProperty -Path "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System" `
        -Name LocalAccountTokenFilterPolicy -Value 1 -Type DWord -Force | Out-Null
    Restart-Service WinRM | Out-Null
    New-NetFirewallRule -DisplayName "WinRM HTTP (5985)" -Direction Inbound -Protocol TCP `
        -LocalPort 5985 -Action Allow -Profile Domain,Private -ErrorAction SilentlyContinue | Out-Null
    Write-Host "    -> WinRM enabled. P920 can now manage this machine directly." -ForegroundColor Green
} catch {
    Write-Host "    -> WinRM setup skipped: $($_.Exception.Message)" -ForegroundColor Yellow
}

# ─── 10. Start agent now ─────────────────────────────────────────────────────
Write-Host "`nStarting MYKRO Node Agent (background) ..." -ForegroundColor Green
Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 3

# Quick health check
try {
    $resp = Invoke-RestMethod -Uri "http://localhost:$Port/ping" -TimeoutSec 5
    Write-Host "  Health check: $($resp | ConvertTo-Json -Compress)" -ForegroundColor Green
} catch {
    Write-Host "  (Agent starting up — check http://localhost:$Port/ping in a moment)" -ForegroundColor Yellow
}

# ─── Done — print registration info ──────────────────────────────────────────
Write-Host "`n╔══════════════════════════════════════════════════════════════╗" -ForegroundColor Cyan
Write-Host "║                  NODE AGENT IS READY                        ║" -ForegroundColor Cyan
Write-Host "╚══════════════════════════════════════════════════════════════╝" -ForegroundColor Cyan
Write-Host ""
Write-Host "  Node URL  : http://${IP}:$Port" -ForegroundColor White
Write-Host "  Token     : $Token" -ForegroundColor Yellow
Write-Host "  Token file: $TokenFile" -ForegroundColor Gray
Write-Host ""
Write-Host "─── Add this to gui_flask/config.json on the P920 ─────────────" -ForegroundColor Cyan
Write-Host @"
  "nodes": [
    {
      "name": "$env:COMPUTERNAME",
      "url": "http://${IP}:$Port",
      "token": "$Token",
      "enabled": true
    }
  ]
"@
Write-Host "────────────────────────────────────────────────────────────────" -ForegroundColor Cyan
Write-Host ""
Write-Host "  Test from P920:" -ForegroundColor White
Write-Host "  curl http://${IP}:${Port}/ping" -ForegroundColor Gray
Write-Host "  curl -H `"X-MYKRO-Token: $Token`" http://${IP}:${Port}/health" -ForegroundColor Gray
Write-Host ""
Write-Host "  Logs: $NodeDir\node_agent.log" -ForegroundColor Gray
Write-Host "  Stop: Stop-ScheduledTask -TaskName '$TaskName'" -ForegroundColor Gray
Write-Host ""
