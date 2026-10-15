$ErrorActionPreference = 'Stop'
Write-Host 'LOCAL_ONLY listener audit (0.0.0.0 / :: means exposed on all interfaces):' -ForegroundColor Cyan
$ports = @(3000, 18080, 5432, 6333, 6334, 11434)
Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
  Where-Object { $ports -contains $_.LocalPort } |
  Sort-Object LocalPort, LocalAddress |
  Select-Object LocalAddress, LocalPort, OwningProcess |
  Format-Table -AutoSize
Write-Host 'Docker published ports:'
docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
Write-Host 'ALERT: any 0.0.0.0 or [::] listening on these ports needs firewall review.' -ForegroundColor Yellow
