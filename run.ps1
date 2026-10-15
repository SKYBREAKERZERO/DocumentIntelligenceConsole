$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) {
    $python = 'python'
}

& $python (Join-Path $PSScriptRoot 'scripts\verify_runtime_package.py')
if ($LASTEXITCODE -ne 0) {
    throw 'Application package integrity check failed. Use a complete release ZIP.'
}

& $python -m app.gui.application
if ($LASTEXITCODE -ne 0) {
    throw "Application failed to launch (exit code: $LASTEXITCODE). See the Python traceback above."
}
