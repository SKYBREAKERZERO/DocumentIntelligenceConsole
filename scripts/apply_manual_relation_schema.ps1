param(
    [string]$HostName = "127.0.0.1",
    [int]$Port = 5432,
    [string]$User = "postgres",
    [string[]]$Databases = @("rag_21mm", "rag_24mm", "rag")
)

$ErrorActionPreference = "Stop"
$env:PGCLIENTENCODING = "UTF8"
if ($env:POSTGRES_PASSWORD -and -not $env:PGPASSWORD) {
    $env:PGPASSWORD = $env:POSTGRES_PASSWORD
}

$repoRoot = Split-Path -Parent $PSScriptRoot
$upgrade = Join-Path $repoRoot "database\upgrade_manual_spec_relations_v6_5.sql"
$verify = Join-Path $repoRoot "database\verify_manual_spec_relations_v6_5.sql"

if (-not (Test-Path $upgrade)) {
    throw "Upgrade SQL not found: $upgrade"
}

foreach ($db in $Databases) {
    Write-Host "==> Applying manual relation schema to $db" -ForegroundColor Cyan
    & psql -h $HostName -p $Port -U $User -d $db -v ON_ERROR_STOP=1 -f $upgrade
    if ($LASTEXITCODE -ne 0) {
        throw "Schema upgrade failed for $db"
    }
}

Write-Host "==> Verification on $($Databases[0])" -ForegroundColor Cyan
& psql -h $HostName -p $Port -U $User -d $Databases[0] -v ON_ERROR_STOP=1 -f $verify
if ($LASTEXITCODE -ne 0) {
    throw "Verification failed for $($Databases[0])"
}

Write-Host "Manual relation schema ready." -ForegroundColor Green
