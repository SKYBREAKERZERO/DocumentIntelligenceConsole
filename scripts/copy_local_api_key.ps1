# Windows PowerShell 5.1 compatible (ASCII-only source).
$ErrorActionPreference = 'Stop'
$keyPath = Join-Path $env:LOCALAPPDATA 'DocumentIntelligenceConsole\local_api_token.txt'
if (-not (Test-Path -LiteralPath $keyPath)) { throw "API key file not found: $keyPath" }
$key = (Get-Content -LiteralPath $keyPath -Raw -Encoding UTF8).Trim()
if ($key.Length -lt 32) { throw 'Invalid API key length.' }
Set-Clipboard -Value $key
Write-Host 'SUCCESS: API key copied to clipboard (not printed).'
Remove-Variable key -ErrorAction SilentlyContinue
