param([string]$BaseUrl = 'http://127.0.0.1:18080', [string]$Scope = '21MM')
$ErrorActionPreference = 'Stop'
$tokenPath = Join-Path $env:LOCALAPPDATA 'DocumentIntelligenceConsole\local_api_token.txt'
if (-not (Test-Path $tokenPath)) { throw 'Backend token file not found' }
$token = (Get-Content $tokenPath -Raw).Trim()
$headers = @{ Authorization = "Bearer $token" }
$cases = @(
    @{ question='Bluetooth Audio function spec v2.52有吗'; expected='SPEC_LOOKUP' },
    @{ question='查一下21MM 的 550 BT式样书的内容 按目录总结'; expected='SPEC_OUTLINE' },
    @{ question='你现在能查到哪些21MM式样书'; expected='SPEC_CATALOG' },
    @{ question='查询21MM Bluetooth Audio规格书已登记的相邻两个本体版本之间的变更；如果没有两个可信本体版本，请说明，不使用Cover Sheet'; expected='SPEC_VERSION_FACTS' }
)
foreach ($case in $cases) {
    $body = @{ model='document-intelligence-rag'; scope=$Scope; stream=$false;
               messages=@(@{role='user';content=$case.question}) } | ConvertTo-Json -Depth 8 -Compress
    $watch = [Diagnostics.Stopwatch]::StartNew()
    $response = Invoke-RestMethod -Uri "$BaseUrl/v1/chat/completions" -Method Post -Headers $headers -ContentType 'application/json; charset=utf-8' -Body ([Text.Encoding]::UTF8.GetBytes($body)) -TimeoutSec 45
    $watch.Stop()
    $answer = [string]$response.choices[0].message.content
    $actual = [string]$response.rag.route
    [PSCustomObject]@{ Expected=$case.expected; Actual=$actual; RoutePass=($actual -eq $case.expected);
      DurationMs=$watch.ElapsedMilliseconds; ModelInvocationExpected=$false; Excerpt=$answer.Substring(0,[Math]::Min(220,$answer.Length)) }
    if ($actual -ne $case.expected -or $answer.Contains('本次模型回答未通过企业引用完整性校验')) {
        throw "FAIL: incorrect factual route/gate for question: $($case.question)"
    }
}
# No commands above write to SQL/Qdrant. Do not print token values.
