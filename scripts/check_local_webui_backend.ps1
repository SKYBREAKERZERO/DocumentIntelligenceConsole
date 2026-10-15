$ErrorActionPreference = 'Stop'
Write-Host 'Host API LISTENING (should show 127.0.0.1:18080):'
Get-NetTCPConnection -LocalPort 18080 -State Listen -ErrorAction SilentlyContinue |
  Select-Object LocalAddress,LocalPort,OwningProcess
Write-Host 'Local WebUI / Qdrant containers:'
docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
$keyFile = Join-Path $env:LOCALAPPDATA 'DocumentIntelligenceConsole\local_api_token.txt'
if (-not (Test-Path $keyFile)) { Write-Warning 'Start backend first'; exit 0 }
$key = (Get-Content $keyFile -Raw -Encoding UTF8).Trim()
$headers = @{ Authorization = "Bearer $key" }
try {
  Invoke-RestMethod -Uri 'http://127.0.0.1:18080/api/v1/health' -Headers $headers -TimeoutSec 5 |
    Format-List
  Invoke-RestMethod -Uri 'http://127.0.0.1:18080/v1/models' -Headers $headers -TimeoutSec 5 |
    ConvertTo-Json -Depth 5
} catch {
  Write-Warning 'API health failed; check the backend PowerShell window.'
  throw
}
