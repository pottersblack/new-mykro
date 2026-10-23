$settingsPath = ".vscode\settings.json"
if (-not (Test-Path ".vscode")) { New-Item -ItemType Directory -Path ".vscode" -Force | Out-Null }

if (Test-Path $settingsPath) {
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

$updates = @{
 'mykro.gatewayUrl' = 'http://127.0.0.1:8090'
 'mykro.defaultModel' = 'mykro-auto'
 'mykro.inlineCompletionsEnabled' = $true
 'mykro.autoStartBackendOnPrompt' = $true
}

foreach ($k in $updates.Keys) { $config[$k] = $updates[$k] }

$config | ConvertTo-Json -Depth 10 | Out-File -FilePath $settingsPath -Encoding utf8 -Force
Write-Host "WROTE"
Get-Content $settingsPath -Raw
