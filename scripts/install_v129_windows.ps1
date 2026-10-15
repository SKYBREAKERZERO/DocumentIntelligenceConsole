# Explicit reproducible Windows Python 3.12 install. No DB migrations or password handling.
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
& py -3.12 -c 'import sys; assert sys.version_info[:2] == (3, 12)'
if ($LASTEXITCODE -ne 0) { throw 'Python 3.12 is required.' }
& py -3.12 -m venv .venv
if ($LASTEXITCODE -ne 0) { throw 'Virtual environment creation failed.' }
$python = Join-Path $root '.venv\Scripts\python.exe'
& $python -m pip install -r (Join-Path $root 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed. Review pip output; no partial success claimed.' }
& $python -m pip check
if ($LASTEXITCODE -ne 0) { throw 'Dependency conflict detected by pip check.' }
& $python (Join-Path $root 'scripts\verify_runtime_package.py')
if ($LASTEXITCODE -ne 0) { throw 'Release source integrity failed.' }
Write-Host 'Dependencies and source check completed. Run manual GUI + database acceptance separately.'
