# v1.30 read-only RAG gateway inspection. Requires PowerShell on Windows.
# Never modifies Docker settings, secrets, firewall or user Open WebUI config.
param(
    [string]$ContainerName = 'open-webui',
    [string]$Backend = 'http://127.0.0.1:18080'
)
$ErrorActionPreference = 'Stop'
if ($Backend -ne 'http://127.0.0.1:18080') {
    throw 'Backend must remain local-only http://127.0.0.1:18080'
}
$tokenFile = Join-Path $env:LOCALAPPDATA 'DocumentIntelligenceConsole\local_api_token.txt'
if (-not (Test-Path -LiteralPath $tokenFile)) {
    throw 'Local backend token missing. Start the DocumentIntelligenceConsole backend first.'
}
$token = (Get-Content -LiteralPath $tokenFile -Raw).Trim()
if ($token.Length -lt 32) { throw 'Backend token is invalid.' }
try {
    $models = Invoke-RestMethod -Uri "$Backend/v1/models" -Method Get `
        -Headers @{ Authorization = "Bearer $token" } -TimeoutSec 5
} catch {
    throw 'Local RAG Backend authentication/connection failed. No external endpoint was contacted.'
}
$ids = @($models.data | ForEach-Object { $_.id })
if ('document-intelligence-rag' -notin $ids) {
    throw 'RAG model missing from the local Backend. Select/fix Backend first.'
}
Write-Host 'Internal gateway: REACHABLE; model document-intelligence-rag: AVAILABLE'
$raw = @(docker inspect $ContainerName 2>$null)
if ($LASTEXITCODE -ne 0 -or $raw.Count -eq 0) {
    Write-Warning 'Open WebUI container unavailable; direct Ollama/web-search bypass cannot be checked.'
    exit 2
}
$container = @((($raw -join "`n") | ConvertFrom-Json))[0]
$flags = @{}
foreach ($line in @($container.Config.Env)) {
    $pair = $line -split '=', 2
    if ($pair.Count -eq 2 -and $pair[0] -in @('ENABLE_WEB_SEARCH','ENABLE_OLLAMA_API','ENABLE_OPENAI_API')) {
        $flags[$pair[0]] = $pair[1]
    }
}
foreach ($name in @('ENABLE_WEB_SEARCH','ENABLE_OLLAMA_API','ENABLE_OPENAI_API')) {
    $state = if ($flags.ContainsKey($name)) { $flags[$name] } else { 'NOT_EXPLICITLY_SET' }
    Write-Host "$name=$state"
}
Write-Warning 'Container environment is NOT the final Open WebUI configuration. Persistent admin UI settings may override it.'
Write-Host 'Required in Open WebUI admin settings:'
Write-Host '  - OpenAI-compatible backend: http://host.docker.internal:18080/v1'
Write-Host '  - Model: document-intelligence-rag (choose this instead of qwen3/other direct Ollama models)'
Write-Host '  - Disable Web Search, external tools/functions and direct Ollama model access for internal-only users.'
Write-Host '  - Apply host/container outbound policy for strong network-level enforcement.'
