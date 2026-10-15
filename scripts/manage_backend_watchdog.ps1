# Windows PowerShell 5.1 compatible, ASCII only. Current-user watchdog registration.
# Run -Action Enable after extracting to its permanent path and installing .venv.
[CmdletBinding()]
param([ValidateSet('Enable','Disable','Status')][string]$Action='Status')
$ErrorActionPreference='Stop'
$root=(Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$registry='HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
$name='DocumentIntelligenceBackendWatchdog'
$configDir=Join-Path $env:LOCALAPPDATA 'DocumentIntelligenceConsole'
$stopFile=Join-Path $configDir 'backend_watchdog.stop'
$pythonw=Join-Path $root '.venv\Scripts\pythonw.exe'
$launcher=Join-Path $root 'scripts\backend_watchdog.pyw'
if ($Action -eq 'Status') {
    $value=(Get-ItemProperty -Path $registry -Name $name -ErrorAction SilentlyContinue).$name
    if ($value) { Write-Host ('REGISTERED ' + $value) } else { Write-Host 'DISABLED' }
    $statusPath=Join-Path $configDir 'backend_watchdog_status.json'
    if (Test-Path -LiteralPath $statusPath) { Get-Content -LiteralPath $statusPath -Raw }
    exit 0
}
if ($Action -eq 'Disable') {
    Remove-ItemProperty -Path $registry -Name $name -ErrorAction SilentlyContinue
    New-Item -ItemType Directory -Path $configDir -Force | Out-Null
    Set-Content -LiteralPath $stopFile -Value 'stop' -Encoding ASCII
    Write-Host 'Watchdog disabled at next login; running watchdog will exit within 15 seconds.'
    Write-Host 'Existing RAG Backend is not terminated.'
    exit 0
}
if (-not (Test-Path -LiteralPath $pythonw)) { throw 'Install the Python 3.12 virtual environment first.' }
if (-not (Test-Path -LiteralPath $launcher)) { throw 'Watchdog entrypoint not found.' }
New-Item -ItemType Directory -Path $configDir -Force | Out-Null
Remove-Item -LiteralPath $stopFile -Force -ErrorAction SilentlyContinue
$command='"' + $pythonw + '" "' + $launcher + '"'
New-Item -Path $registry -Force | Out-Null
Set-ItemProperty -Path $registry -Name $name -Value $command
Start-Process -FilePath $pythonw -ArgumentList ('"' + $launcher + '"') -WorkingDirectory $root -WindowStyle Hidden
Write-Host 'Backend watchdog enabled: checks local RAG every 15 seconds after Windows user login.'
Write-Host 'Uses current-user HKCU Run; does not expose ports, alter Docker, or require admin.'
