# Developer diagnostic: foreground Backend. The GUI also starts it detached.
# ASCII-only script for Windows PowerShell 5.1 without a UTF-8 BOM.
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) { $python = 'python' }
$env:HF_HUB_OFFLINE = '1'
$env:TRANSFORMERS_OFFLINE = '1'
$env:HF_DATASETS_OFFLINE = '1'
& $python -c "import fastapi, uvicorn, yaml; print('Local API dependencies: OK')"
if ($LASTEXITCODE -ne 0) { throw 'Missing local API dependencies; install requirements-api.txt.' }
Write-Host 'Starting localhost-only DocumentIntelligence Backend (18080 by default)' -ForegroundColor Cyan
& $python -m app.server
if ($LASTEXITCODE -ne 0) { throw 'Backend failed; inspect config, credentials and local logs.' }
