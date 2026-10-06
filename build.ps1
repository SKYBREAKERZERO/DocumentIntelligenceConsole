param(
    [switch]$SkipVerify
)

$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) {
    $python = 'python'
}

if (-not $SkipVerify) {
    Write-Host '[preflight] Running v1.03 source syntax verification...'
    & $python -m compileall -q app scripts
    if ($LASTEXITCODE -ne 0) {
        throw 'v1.03 preflight failed: Python source compilation error.'
    }
}

Write-Host '[build] Creating DocumentIntelligenceConsole.exe...'
& $python -m PyInstaller --clean document_intelligence_console.spec
Write-Host 'Build output: dist\DocumentIntelligenceConsole.exe'
