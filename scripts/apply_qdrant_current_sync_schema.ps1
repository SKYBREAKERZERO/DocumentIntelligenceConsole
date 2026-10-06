$ErrorActionPreference = "Stop"
$env:PGCLIENTENCODING = "UTF8"

$HostName = if ($env:POSTGRES_HOST) { $env:POSTGRES_HOST } else { "127.0.0.1" }
$Port = if ($env:POSTGRES_PORT) { $env:POSTGRES_PORT } else { "5432" }
$User = if ($env:POSTGRES_USER) { $env:POSTGRES_USER } else { "postgres" }

foreach ($db in @("rag_21mm", "rag_24mm", "rag")) {
    Write-Host "Applying Qdrant current-sync schema to $db..."
    psql -h $HostName -p $Port -U $User -d $db -v ON_ERROR_STOP=1 -f ".\database\upgrade_qdrant_current_sync_v6_6.sql"
    psql -h $HostName -p $Port -U $User -d $db -v ON_ERROR_STOP=1 -f ".\database\verify_qdrant_current_sync_v6_6.sql"
}
