# PowerShell 5.1 safe: ASCII only, no secret echo, no destructive DDL.
param(
  [Parameter(Mandatory=$true)][ValidateSet('21MM','24MM')][string]$Project,
  [Parameter(Mandatory=$true)][string]$Database,
  [string]$User = 'postgres',
  [string]$HostName = '127.0.0.1',
  [int]$Port = 5432,
  [string]$PostgresBin = 'C:\Program Files\PostgreSQL\17\bin',
  [switch]$Apply
)
$ErrorActionPreference = 'Stop'
if ($Database -notmatch '^[A-Za-z0-9_]{1,64}$' -or $Database -in @('rag','rag_qabug')) {
  throw 'Use an explicit specification database, e.g. rag_21mm or rag_24mm.'
}
$psql = Join-Path $PostgresBin 'psql.exe'
$dump = Join-Path $PostgresBin 'pg_dump.exe'
if (-not (Test-Path $psql) -or -not (Test-Path $dump)) { throw 'PostgreSQL tools not found.' }
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$migration = Join-Path $root 'database\upgrade_engineering_intelligence_v7.sql'
if (-not (Test-Path $migration)) { throw 'Missing migration.' }
Write-Host "Target database: $Database; project: $Project"
$actual = (& $psql -h $HostName -p $Port -U $User -d $Database -X -A -t -v ON_ERROR_STOP=1 -c 'SELECT project_code FROM public.project_info LIMIT 1')
if ($LASTEXITCODE -ne 0 -or ($actual | Out-String).Trim().ToUpperInvariant() -ne $Project) {
  throw 'Database identity check failed. No migration applied.'
}
if (-not $Apply) {
  Write-Host 'DRY RUN. Database identity verified. Add -Apply to take a local backup and apply the additive migration.'
  return
}
$backupDir = Join-Path $root 'local_backups'
New-Item -ItemType Directory -Path $backupDir -Force | Out-Null
$stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$backup = Join-Path $backupDir ("$($Database)_before_ei_v7_$stamp.dump")
& $dump -h $HostName -p $Port -U $User -d $Database -Fc -f $backup
if ($LASTEXITCODE -ne 0 -or -not (Test-Path $backup) -or (Get-Item $backup).Length -lt 1024) {
  throw 'Database backup failed. Migration refused.'
}
Write-Host "Backup stored locally: $backup"
& $psql -h $HostName -p $Port -U $User -d $Database -X -v ON_ERROR_STOP=1 -f $migration
if ($LASTEXITCODE -ne 0) { throw 'Migration failed. Database backup retained.' }
Write-Host 'Additive EI v7 schema successfully applied.'
