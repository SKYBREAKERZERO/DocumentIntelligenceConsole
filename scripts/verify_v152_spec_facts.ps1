param([string]$BaseUrl='http://127.0.0.1:18080', [string]$Scope='21MM')
$ErrorActionPreference = 'Stop'
$tokenPath = Join-Path $env:LOCALAPPDATA 'DocumentIntelligenceConsole\local_api_token.txt'
if (-not (Test-Path $tokenPath)) { throw 'Local API token file not found' }
$token = (Get-Content $tokenPath -Raw).Trim()
$headers = @{ Authorization = "Bearer $token" }
$cases = @(
    @{ Question='Bluetooth Audio function spec v2.52有吗'; Route='SPEC_LOOKUP' },
    @{ Question='查一下21MM 的 550 BT式样书的内容 按目录总结'; Route='SPEC_OUTLINE' },
    @{ Question='查询21MM 550 BT相邻两个本体版本之间的变更'; Route='SPEC_VERSION_FACTS' }
)
foreach ($case in $cases) {
    $payload = @{ operation='spec_lookup'; scope=$Scope; question=$case.Question }
    # Use the chat endpoint for correct operation dispatch instead of forcing
    # spec_lookup on every question.
    $payload = @{ model='document-intelligence-rag'; scope=$Scope; stream=$false;
                  messages=@(@{role='user'; content=$case.Question}) }
    $json = $payload | ConvertTo-Json -Depth 8 -Compress
    $stopwatch = [Diagnostics.Stopwatch]::StartNew()
    $response = Invoke-RestMethod -Uri "$BaseUrl/v1/chat/completions" -Method Post `
        -Headers $headers -ContentType 'application/json; charset=utf-8' `
        -Body ([Text.Encoding]::UTF8.GetBytes($json)) -TimeoutSec 20
    $stopwatch.Stop()
    $actual = [string]$response.rag.route
    $answer = [string]$response.choices[0].message.content
    $warnings = @($response.rag.warnings)
    $pass = ($actual -eq $case.Route) -and `
            (-not ($warnings -match 'IndeterminateDatatype')) -and `
            (-not $answer.Contains('模型回答未通过企业引用完整性校验'))
    [PSCustomObject]@{
        RouteExpected=$case.Route
        RouteActual=$actual
        RouteAndSQLPass=$pass
        DurationMs=$stopwatch.ElapsedMilliseconds
        Warnings=($warnings -join '; ')
        Answer=($answer.Substring(0, [Math]::Min(350, $answer.Length)))
    }
    if (-not $pass) { throw 'Factual SQL acceptance failed; inspect warnings above' }
}
# No PostgreSQL/Qdrant writes; no token values printed.
