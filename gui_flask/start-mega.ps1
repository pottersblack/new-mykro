# start-mega.ps1 — Launches Mixtral-8x22B-Instruct on port 8082
# Uses all 40 threads with NUMA-distributed memory to maximise 128 GB RAM throughput.
param(
    [switch]$Debug
)

$here = Split-Path -Parent $MyInvocation.MyCommand.Definition
$projectRoot = Split-Path -Parent $here

$LlamaServer = $env:MYKRO_LLAMA_SERVER
if (-not $LlamaServer) {
    try {
        $LlamaServer = (Get-Command llama-server.exe -ErrorAction Stop).Source
    } catch {
        $LlamaServer = ""
    }
}
$ModelPath = $env:MYKRO_MEGA_MODEL_PATH
if (-not $ModelPath) {
    $ModelPath = Join-Path $projectRoot "model\Mixtral-8x22B-Instruct-v0.1.Q4_K_M-00001-of-00002.gguf"
}
$Port         = 8082
$CtxSize      = 32768
$Threads      = 20   # physical cores per CPU socket
$BatchThreads = 40   # hyper-threads for batch processing

if (-not (Test-Path $LlamaServer)) {
    Write-Error "llama-server not found at $LlamaServer"
    exit 1
}

if (-not (Test-Path $ModelPath)) {
    Write-Warning "Model not found at $ModelPath"
    Write-Warning "Download it first with: python tools/download_model.py"
    Write-Warning "Starting server anyway — it will fail to load until the model exists."
}

$args_list = @(
    "--model", $ModelPath,
    "--port", $Port,
    "--threads", $Threads,
    "--threads-batch", $BatchThreads,
    "--ctx-size", $CtxSize,
    "--n-gpu-layers", 0,          # GTX 1050 only has 2 GB VRAM — CPU-only
    "--device", "none",           # disable Vulkan/CUDA backend entirely (CPU-only)
    "--numa", "distribute",       # spread memory across both Xeon sockets
    "--host", "127.0.0.1",
    "--log-prefix",
    "--flash-attn", "on"
)

if ($Debug) {
    $args_list += "--verbose"
}

Write-Host "Starting MYKRO Mega (Mixtral-8x22B) on port $Port ..."
Write-Host "Model : $ModelPath"
Write-Host "Args  : $args_list"

& $LlamaServer @args_list
