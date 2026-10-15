# Safe opt-in network-only migration. Does not delete the qdrant_storage Docker volume.
param([switch]$Apply)
$ErrorActionPreference = 'Stop'
$name = 'qdrant'
$inspectRaw = docker inspect $name
if ($LASTEXITCODE -ne 0) { throw 'qdrant container missing; stop.' }
$state = @($inspectRaw | ConvertFrom-Json)[0]
$vols = @($state.Mounts | Where-Object { $_.Destination -eq '/qdrant/storage' -and $_.Name -eq 'qdrant_storage' -and $_.Type -eq 'volume' })
if ($vols.Count -ne 1) { throw 'Required qdrant_storage volume missing; abort.' }
$image = [string]$state.Config.Image
if ($image -notlike 'qdrant/qdrant:*') { throw "Unexpected Qdrant image: $image" }
$otherMounts = @($state.Mounts | Where-Object { $_.Destination -ne '/qdrant/storage' })
if ($otherMounts.Count -gt 0) { throw 'Custom mounts found. Review manually.' }
if ($state.Config.Cmd -and @($state.Config.Cmd).Count -gt 0) { Write-Warning 'Custom CMD detected. Review docker inspect before Apply.' }
$backup = Join-Path $PSScriptRoot ('qdrant-inspect-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.json')
$inspectRaw | Set-Content -Encoding UTF8 -Path $backup
Write-Host "Inspect configuration backup: $backup"
Write-Host "Data volume: qdrant_storage; image: $image"
Write-Host 'Plan: rebind only from 0.0.0.0 to 127.0.0.1; preserve volume.'
if (-not $Apply) { Write-Host 'Dry run complete. Review custom environment/arguments before Apply.'; return }
$running = [bool]$state.State.Running
if ($running) {
  docker stop $name | Out-Null
  if ($LASTEXITCODE -ne 0) { throw 'Failed to stop existing container.' }
}
docker rm $name | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Failed to remove container; volume unchanged.' }
docker run -d --name $name --restart unless-stopped `
  -p 127.0.0.1:6333:6333 -p 127.0.0.1:6334:6334 `
  -v qdrant_storage:/qdrant/storage $image
if ($LASTEXITCODE -ne 0) { throw 'Rebuild failed. Preserve qdrant_storage; do NOT remove Docker volume.' }
docker port qdrant
Write-Host 'Verify ports are 127.0.0.1 and verify Qdrant collections.' -ForegroundColor Green
