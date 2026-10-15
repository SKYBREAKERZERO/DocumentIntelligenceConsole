# Windows PowerShell 5.1. Backs up authoritative SQL only. Does not touch Qdrant volumes.
[CmdletBinding()]
param(
    [string]$Destination = '',
    [string]$PgBin = 'C:\Program Files\PostgreSQL\17\bin',
    [string]$PgUser = 'postgres',
    [string]$PgHost = '127.0.0.1',
    [ValidateRange(1,65535)][int]$PgPort = 5432,
    [string[]]$Databases = @('rag_21mm','rag_24mm','rag','rag_qabug'),
    [switch]$Apply
)
$ErrorActionPreference = 'Stop'
if (-not $Destination) {
    $Destination = Join-Path $env:LOCALAPPDATA ('DocumentIntelligenceConsole\local_backups\' + (Get-Date -Format 'yyyyMMdd_HHmmss'))
}
$pgDump = Join-Path $PgBin 'pg_dump.exe'
$pgRestore = Join-Path $PgBin 'pg_restore.exe'
foreach ($tool in @($pgDump,$pgRestore)) {
    if (-not (Test-Path -LiteralPath $tool)) { throw ('Missing PostgreSQL backup tool: ' + $tool) }
}
if ($PgHost -notin @('127.0.0.1','localhost','::1')) {
    throw 'Only local PostgreSQL is supported.'
}
if ($Databases.Count -lt 1 -or $Databases.Count -gt 4) { throw 'Select 1..4 authorized databases.' }
$invalidNames = @($Databases | Where-Object { $_ -notmatch '^[a-z][a-z0-9_]{0,62}$' })
if ($invalidNames.Count -ne 0) {
    throw 'Invalid database name.'
}
$all = @($Databases | Select-Object -Unique)
$destinationPath = [IO.Path]::GetFullPath($Destination)
if ($destinationPath -match '[\\/]input[\\/]|[\\/]output[\\/]' -or $destinationPath -match '[\\/]open-webui[\\/]') {
    throw 'Backup destination cannot be an import/output/WebUI-served directory.'
}
Write-Host ('Backup databases: ' + ($all -join ', '))
Write-Host ('Destination: ' + $destinationPath)
Write-Host 'pg_dump -Fc uses PostgreSQL MVCC snapshot per database; this is NOT an atomic snapshot across all databases.'
Write-Host 'Backups may contain company-confidential material; store in an encrypted, access-controlled folder.'
if (-not $Apply) { Write-Host 'DRY RUN: no backup created. Re-run with -Apply after review.'; exit 0 }
New-Item -ItemType Directory -Path $destinationPath -Force | Out-Null
$results = @()
foreach ($db in $all) {
    $archive = Join-Path $destinationPath ($db + '.dump')
    $partial = $archive + '.partial'
    if ((Test-Path -LiteralPath $archive) -or (Test-Path -LiteralPath $partial)) {
        throw ('Refusing to overwrite backup file: ' + $archive)
    }
    Write-Host ('Backing up ' + $db + ' ...')
    & $pgDump -h $PgHost -p $PgPort -U $PgUser -d $db -F c -f $partial
    if ($LASTEXITCODE -ne 0) { throw ('pg_dump failed for ' + $db + '; partial backup retained for inspection') }
    & $pgRestore --list $partial | Out-Null
    if ($LASTEXITCODE -ne 0) { throw ('pg_restore verification failed for ' + $db) }
    Move-Item -LiteralPath $partial -Destination $archive -ErrorAction Stop
    $hash = Get-FileHash -LiteralPath $archive -Algorithm SHA256
    $results += [PSCustomObject]@{
        database=$db; file=($db + '.dump'); size_bytes=(Get-Item -LiteralPath $archive).Length; sha256=$hash.Hash
    }
}
$manifest = [PSCustomObject]@{
    format='pg_dump-custom'; created_at=[DateTimeOffset]::Now.ToString('o')
    postgres_host=$PgHost; postgres_port=$PgPort; dumps=$results
    note='Restore is a deliberate operator action; no cross-database atomicity or Qdrant snapshot included.'
}
$manifest | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath (Join-Path $destinationPath 'manifest.json') -Encoding UTF8
Write-Host 'SUCCESS: all requested SQL backups verified and SHA256 manifest written.'
