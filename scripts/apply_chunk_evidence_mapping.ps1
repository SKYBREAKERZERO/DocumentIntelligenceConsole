param(
    [Parameter(Mandatory=$false)]
    [string]$ProjectRoot = ".",

    [Parameter(Mandatory=$false)]
    [string]$Database = "rag_24mm",

    [Parameter(Mandatory=$false)]
    [switch]$SkipDatabase
)

$ErrorActionPreference = "Stop"
$PatchRoot = Split-Path -Parent $PSScriptRoot
$ProjectRoot = (Resolve-Path $ProjectRoot).Path

Write-Host "Target project: $ProjectRoot"

$required = @(
    "app",
    "database"
)
foreach ($item in $required) {
    if (-not (Test-Path (Join-Path $ProjectRoot $item))) {
        throw "Expected project path missing: $item"
    }
}

$serviceTarget = Join-Path $ProjectRoot "app\services\chunk_evidence_mapping.py"
$databaseUpgradeTarget = Join-Path $ProjectRoot "database\upgrade_chunk_evidence_mapping_v6_1.sql"
$databaseVerifyTarget = Join-Path $ProjectRoot "database\verify_chunk_evidence_mapping_v6_1.sql"
$testTargetDir = Join-Path $ProjectRoot "tests"
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $serviceTarget) | Out-Null
New-Item -ItemType Directory -Force -Path $testTargetDir | Out-Null

Copy-Item (Join-Path $PatchRoot "app\services\chunk_evidence_mapping.py") $serviceTarget -Force
Copy-Item (Join-Path $PatchRoot "database\upgrade_chunk_evidence_mapping_v6_1.sql") $databaseUpgradeTarget -Force
Copy-Item (Join-Path $PatchRoot "database\verify_chunk_evidence_mapping_v6_1.sql") $databaseVerifyTarget -Force
Copy-Item (Join-Path $PatchRoot "tests\test_chunk_evidence_mapping.py") (Join-Path $testTargetDir "test_chunk_evidence_mapping.py") -Force

Write-Host "Overlay files copied."

if (-not $SkipDatabase) {
    if (-not $env:POSTGRES_PASSWORD) {
        throw "POSTGRES_PASSWORD is not set. Set it or run with -SkipDatabase."
    }
    $psql = Get-Command psql -ErrorAction SilentlyContinue
    if (-not $psql) {
        throw "psql was not found in PATH. Run with -SkipDatabase or install PostgreSQL client tools."
    }
    $env:PGPASSWORD = $env:POSTGRES_PASSWORD
    & $psql.Source -h 127.0.0.1 -p 5432 -U postgres -d $Database -v ON_ERROR_STOP=1 -f $databaseUpgradeTarget
    if ($LASTEXITCODE -ne 0) { throw "Database upgrade failed." }
    & $psql.Source -h 127.0.0.1 -p 5432 -U postgres -d $Database -v ON_ERROR_STOP=1 -f $databaseVerifyTarget
    if ($LASTEXITCODE -ne 0) { throw "Database verification failed." }
}

Write-Host "Chunk/Evidence exact mapping overlay applied."
