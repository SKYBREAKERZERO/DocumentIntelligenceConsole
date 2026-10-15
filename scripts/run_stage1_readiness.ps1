# v1.28 Stage 1 offline source-package preflight (no database calls).
[CmdletBinding()]
param([Parameter(Mandatory=$true)][string]$OutputFile)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$py = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $py -PathType Leaf)) {
    throw 'No project .venv found. Install requirements before running the Windows acceptance preflight.'
}
& $py (Join-Path $PSScriptRoot 'stage1_release_readiness.py') --out $OutputFile
if ($LASTEXITCODE -ne 0) { throw 'Stage 1 readiness report could not be generated.' }
Write-Host 'Readiness JSON created. This does NOT certify production acceptance.'
