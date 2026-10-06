$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

Write-Host "Document Intelligence Console v1.03 - AI Startup" -ForegroundColor Cyan

$ollamaReady = $false
try {
    $null = Invoke-RestMethod -Uri 'http://127.0.0.1:11434/api/tags' -Method Get -TimeoutSec 3
    $ollamaReady = $true
    Write-Host "[OK] Ollama is reachable at http://127.0.0.1:11434" -ForegroundColor Green
}
catch {
    Write-Host "[WARN] Ollama is not reachable. Start it with: ollama serve" -ForegroundColor Yellow
}

$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) {
    $python = 'python'
}

& $python -m app.gui.application
