# Non-destructive Windows diagnostic. Does not change firewall or Docker state.
[CmdletBinding()]
param([int]$BackendPort=18080)
$ErrorActionPreference = 'Stop'
$private = @(18080,5432,6333,6334,11434)
$unsafe = @(Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | Where-Object {
    $_.LocalPort -in $private -and $_.LocalAddress -notin @('127.0.0.1','::1')
})
$local = @(Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | Where-Object {
    $_.LocalPort -eq $BackendPort -and $_.LocalAddress -in @('127.0.0.1','::1')
})
Write-Host 'DocumentIntelligence local readiness audit'
Write-Host ('Local backend listener: ' + [bool]($local.Count -gt 0))
if ($unsafe.Count) {
    Write-Warning 'Private-service listeners are exposed beyond loopback:'
    $unsafe | Select-Object LocalAddress,LocalPort,OwningProcess | Format-Table -AutoSize
} else {
    Write-Host 'Private service listener exposure: none detected by TCP listener audit.'
}
if ($local.Count) {
    try {
        $configDir=Join-Path $env:LOCALAPPDATA 'DocumentIntelligenceConsole'
        $keyFile=Join-Path $configDir 'local_api_token.txt'
        if (Test-Path -LiteralPath $keyFile) {
            $key=(Get-Content -LiteralPath $keyFile -Raw -Encoding UTF8).Trim()
            $headers=@{Authorization=('Bearer ' + $key)}
            $metrics=Invoke-RestMethod -Uri ('http://127.0.0.1:' + $BackendPort + '/api/v1/runtime/metrics') -Headers $headers -TimeoutSec 5
            $ready=Invoke-WebRequest -Uri ('http://127.0.0.1:' + $BackendPort + '/api/v1/health/ready') -Headers $headers -UseBasicParsing -TimeoutSec 5 -ErrorAction Stop
            Write-Host ('Backend readiness HTTP: ' + $ready.StatusCode)
            Write-Host ('Active chats: ' + $metrics.chat.active + ' / ' + $metrics.chat.max_active)
        } else { Write-Warning 'Local API token file missing; cannot authenticate diagnostic.' }
    } catch { Write-Warning 'Backend readiness is not confirmed; use API logs and dependency health diagnostics.' }
}
Write-Host 'NOTE: This audit does not prove encryption, network ACLs, live SQL/Qdrant availability or end-to-end RAG quality.'
if ($unsafe.Count -gt 0) { exit 2 }
