# Windows PowerShell 5.1 compatible. ASCII-only to avoid legacy codepage parsing.
# Exposes ONLY Open WebUI to the selected active private-address Wi-Fi interface.
# Never exposes DocumentIntelligence Backend, Qdrant, Ollama, or PostgreSQL.
[CmdletBinding()]
param(
    [ValidateSet('Enable','Disable','Status')][string]$Action = 'Status',
    [ValidateRange(1,65535)][int]$Port = 3000,
    [switch]$ConfirmInternalNetwork,
    [string]$InterfaceAlias = ''
)
$ErrorActionPreference = 'Stop'
$ruleName = 'DIC-OpenWebUI-LAN-Only'
$stateDirectory = Join-Path $env:ProgramData 'DocumentIntelligenceConsole'
$statePath = Join-Path $stateDirectory 'lan_share.json'
$lastResultPath = Join-Path $stateDirectory 'lan_last_result.json'
# Capture failures so the developer Settings panel can report them after UAC closes.
trap {
    $detail = $_.Exception.Message
    New-Item -ItemType Directory -Path $stateDirectory -Force -ErrorAction SilentlyContinue | Out-Null
    @{ ok = $false; message = $detail; at = [DateTimeOffset]::Now.ToString('o') } |
        ConvertTo-Json | Set-Content -LiteralPath $lastResultPath -Encoding UTF8 -ErrorAction SilentlyContinue
    Write-Host ('LAN configuration failed: ' + $detail) -ForegroundColor Red
    exit 1
}
function Save-ActionSuccess([string]$message) {
    New-Item -ItemType Directory -Path $stateDirectory -Force | Out-Null
    @{ ok = $true; message = $message; at = [DateTimeOffset]::Now.ToString('o') } |
        ConvertTo-Json | Set-Content -LiteralPath $lastResultPath -Encoding UTF8
}

function Get-ManagedState {
    if (-not (Test-Path -LiteralPath $statePath)) { return $null }
    $raw = Get-Content -LiteralPath $statePath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($raw.managed_by -ne $ruleName) { throw 'Unrecognized LAN management state; refusing to modify it.' }
    return $raw
}

function Assert-Administrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'Run PowerShell as Administrator. GUI Enable/Disable requests UAC.'
    }
}

function Remove-ManagedRule($state) {
    if ($null -eq $state) { return }
    $addr = [string]$state.ip
    $oldPort = [int]$state.port
    if ($addr -notmatch '^\d{1,3}(\.\d{1,3}){3}$' -or $oldPort -lt 1 -or $oldPort -gt 65535) {
        throw 'Stored LAN state is malformed. Manual review required.'
    }
    & netsh interface portproxy delete v4tov4 "listenaddress=$addr" "listenport=$oldPort" | Out-Null
    $oldRule = Get-NetFirewallRule -Name $ruleName -ErrorAction SilentlyContinue
    if ($oldRule) { $oldRule | Remove-NetFirewallRule -ErrorAction Stop }
}

function Get-ActiveWifi {
    $wifiAdapters = @(
        Get-NetAdapter -ErrorAction Stop | Where-Object {
            $_.Status -eq 'Up' -and (
                $_.Name -match 'Wi-Fi|WiFi|WLAN|Wireless' -or
                $_.InterfaceDescription -match '802\.11|Wi-Fi|Wireless|WLAN'
            ) -and ($InterfaceAlias -eq '' -or $_.Name -eq $InterfaceAlias)
        }
    )
    if ($wifiAdapters.Count -ne 1) {
        throw "Expected exactly one active Wi-Fi interface; found $($wifiAdapters.Count). Choose a trusted Wi-Fi and retry."
    }
    $wifi = $wifiAdapters[0]
    $ips = @(Get-NetIPAddress -AddressFamily IPv4 -InterfaceIndex $wifi.ifIndex -ErrorAction Stop |
        Where-Object { $_.AddressState -eq 'Preferred' -and $_.IPAddress -notlike '169.254.*' })
    if ($ips.Count -ne 1) { throw "Expected one preferred Wi-Fi IPv4 address; found $($ips.Count)" }
    $ip = [string]$ips[0].IPAddress
    $octets = [Net.IPAddress]::Parse($ip).GetAddressBytes()
    $isPrivate = ($octets[0] -eq 10) -or
        ($octets[0] -eq 172 -and $octets[1] -ge 16 -and $octets[1] -le 31) -or
        ($octets[0] -eq 192 -and $octets[1] -eq 168)
    if (-not $isPrivate) { throw 'Wi-Fi IPv4 is not an RFC1918 private address; refusing LAN exposure.' }
    $profile = Get-NetConnectionProfile -InterfaceIndex $wifi.ifIndex -ErrorAction Stop | Select-Object -First 1
    if ($null -eq $profile -or $profile.NetworkCategory -notin @('Public','Private')) {
        throw 'Wi-Fi network profile cannot be safely scoped.'
    }
    return @{ interface = [string]$wifi.Name; ip = $ip; profile = [string]$profile.NetworkCategory }
}

if ($Action -eq 'Status') {
    $state = Get-ManagedState
    if ($null -eq $state) {
        Write-Host 'LAN_DISABLED'
    } else {
        Write-Host ("LAN_CONFIGURED http://{0}:{1} interface={2}" -f $state.ip, $state.port, $state.interface)
    }
    exit 0
}

Assert-Administrator
$previous = Get-ManagedState
if ($Action -eq 'Disable') {
    if ($null -ne $previous) {
        Remove-ManagedRule $previous
        Remove-Item -LiteralPath $statePath -Force -ErrorAction Stop
    } else {
        Write-Host 'LAN_ALREADY_DISABLED'
    }
    Save-ActionSuccess 'LAN sharing disabled'
    Write-Host 'LAN_DISABLED; backend and Docker volumes remain untouched.'
    exit 0
}

if (-not $ConfirmInternalNetwork) {
    throw 'Explicit approval required: pass -ConfirmInternalNetwork only for trusted company Wi-Fi.'
}

# Fail closed if private data/model ports are bound to any non-loopback interface.
# A Wi-Fi login is not authorization to publish PostgreSQL, Qdrant, or Ollama.
$privatePorts = @(5432, 6333, 6334, 11434, 18080)
$unsafeListeners = @(Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
    Where-Object {
        $_.LocalPort -in $privatePorts -and
        $_.LocalAddress -notin @('127.0.0.1','::1')
    })
if ($unsafeListeners.Count -gt 0) {
    $details = ($unsafeListeners | ForEach-Object { "{0}:{1}" -f $_.LocalAddress,$_.LocalPort }) -join ', '
    throw "Private backend/database/vector/model ports have non-loopback listeners: $details. Close these exposures before enabling LAN."
}

$target = Get-ActiveWifi
$lanIp = $target.ip
# Only serve an existing localhost WebUI. Do NOT change Docker port mappings.
$tcp = New-Object Net.Sockets.TcpClient
try {
    $connect = $tcp.BeginConnect('127.0.0.1', $Port, $null, $null)
    if (-not $connect.AsyncWaitHandle.WaitOne(1500)) { throw 'Open WebUI is not listening on localhost.' }
    $tcp.EndConnect($connect)
} finally { $tcp.Close() }

# Do not overwrite a different LAN listener or a proxy that we do not own.
if ($null -eq $previous) {
    $existingRule = Get-NetFirewallRule -Name $ruleName -ErrorAction SilentlyContinue
    if ($existingRule) { throw 'Managed firewall rule already exists without state; manual review required.' }
    $listener = @(Get-NetTCPConnection -LocalAddress $lanIp -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
    if ($listener.Count) { throw 'LAN address/port already has a listener. Refusing to overwrite it.' }
}

Start-Service -Name iphlpsvc -ErrorAction SilentlyContinue
if ((Get-Service iphlpsvc).Status -ne 'Running') { throw 'Windows IP Helper service is required for portproxy.' }
if ($null -ne $previous) { Remove-ManagedRule $previous }
$createdProxy = $false
try {
    & netsh interface portproxy add v4tov4 "listenaddress=$lanIp" "listenport=$Port" 'connectaddress=127.0.0.1' "connectport=$Port" | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'netsh portproxy failed' }
    $createdProxy = $true
    New-NetFirewallRule -Name $ruleName -DisplayName 'DocumentIntelligence Open WebUI Trusted Wi-Fi' `
        -Direction Inbound -Action Allow -Protocol TCP -LocalAddress $lanIp `
        -LocalPort $Port -RemoteAddress LocalSubnet -InterfaceAlias $target.interface `
        -Profile $target.profile -Enabled True -ErrorAction Stop | Out-Null
    New-Item -ItemType Directory -Path $stateDirectory -Force | Out-Null
    $data = @{
        managed_by = $ruleName
        ip = $lanIp
        port = $Port
        interface = $target.interface
        profile = $target.profile
        updated_at = [DateTimeOffset]::Now.ToString('o')
    }
    $tmp = $statePath + '.tmp'
    $data | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $tmp -Encoding UTF8
    Move-Item -LiteralPath $tmp -Destination $statePath -Force
    Save-ActionSuccess ("LAN sharing enabled: http://{0}:{1}" -f $lanIp, $Port)
    Write-Host ("LAN_CONFIGURED http://{0}:{1}" -f $lanIp, $Port)
    Write-Warning 'HTTP is not encrypted. This is only a connectivity test until internal HTTPS is configured.'
    Write-Warning 'Client isolation, corporate WLAN policy, or VLAN ACLs may still block colleagues.'
} catch {
    Get-NetFirewallRule -Name $ruleName -ErrorAction SilentlyContinue | Remove-NetFirewallRule -ErrorAction SilentlyContinue
    if ($createdProxy) {
        & netsh interface portproxy delete v4tov4 "listenaddress=$lanIp" "listenport=$Port" | Out-Null
    }
    Remove-Item -LiteralPath ($statePath + '.tmp') -Force -ErrorAction SilentlyContinue
    throw
}
