$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'wokwi_failure_classification.ps1')

$transportError = @'
API Error: Connection to transport closed unexpectedly: code 1006
'@
$reason = Get-WokwiTransportBlockReason -TestName 'wokwi-node2' -ExitCode 1 -ProcessOutput $transportError
if ([string]::IsNullOrWhiteSpace($reason)) {
  throw 'Wokwi transport-close regression: expected infrastructure BLOCKED classification.'
}

$scenarioFailure = 'Scenario failed: expected serial text was not observed.'
$reason = Get-WokwiTransportBlockReason -TestName 'wokwi-node2' -ExitCode 1 -ProcessOutput $scenarioFailure
if (-not [string]::IsNullOrWhiteSpace($reason)) {
  throw 'Wokwi transport-close regression: scenario assertion failures must remain FAIL.'
}

$reason = Get-WokwiTransportBlockReason -TestName 'backend-regression' -ExitCode 1 -ProcessOutput $transportError
if (-not [string]::IsNullOrWhiteSpace($reason)) {
  throw 'Wokwi transport-close regression: transport classification must be limited to Wokwi runs.'
}

$quotaError = 'API Error: You have used up your Free plan monthly CI minute quota.'
$reason = Get-WokwiTransportBlockReason -TestName 'wokwi-node1' -ExitCode 1 -ProcessOutput $quotaError
if ($reason -notmatch 'monthly CI minute quota is exhausted') {
  throw 'Wokwi quota regression: exhausted CI minutes must be BLOCKED, not reported as a product failure.'
}
$reason = Get-WokwiTransportBlockReason -TestName 'backend-regression' -ExitCode 1 -ProcessOutput $quotaError
if (-not [string]::IsNullOrWhiteSpace($reason)) {
  throw 'Wokwi quota regression: quota classification must apply only to Wokwi tests.'
}

if (-not (Test-WokwiTransportRetryEligible -TestName 'wokwi-node3' -Attempt 1 -Status 'BLOCKED' -ExitCode 77 -ProcessOutput $transportError)) {
  throw 'Wokwi retry regression: the first exact API 1006 transport closure should allow one bounded retry.'
}
if (Test-WokwiTransportRetryEligible -TestName 'wokwi-node3' -Attempt 2 -Status 'BLOCKED' -ExitCode 77 -ProcessOutput $transportError) {
  throw 'Wokwi retry regression: retries must stop after one retry.'
}
if (Test-WokwiTransportRetryEligible -TestName 'wokwi-node3' -Attempt 1 -Status 'FAIL' -ExitCode 1 -ProcessOutput $scenarioFailure) {
  throw 'Wokwi retry regression: scenario assertion failures must not be retried or hidden.'
}
if (Test-WokwiTransportRetryEligible -TestName 'backend-regression' -Attempt 1 -Status 'BLOCKED' -ExitCode 77 -ProcessOutput $transportError) {
  throw 'Wokwi retry regression: retries must apply only to node simulation runs.'
}

$runnerSource = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'run-all-tests.ps1') -Raw
$retryHelper = [regex]::Match($runnerSource, '(?s)function Invoke-WokwiNodeScenarioWithRetry\([^)]+\)\s*\{(?<body>.*?)\r?\n\}')
if (-not $retryHelper.Success -or
    $retryHelper.Groups['body'].Value -notmatch 'Test-WokwiTransportRetryEligible' -or
    $retryHelper.Groups['body'].Value -notmatch 'attempt1\.log' -or
    $retryHelper.Groups['body'].Value -notmatch 'RemoveAt' -or
    $retryHelper.Groups['body'].Value -notmatch 'Invoke-LoggedProcess') {
  throw 'Wokwi retry regression: bounded retry must preserve first-attempt evidence and emit one final node test result.'
}
if ($runnerSource -notmatch 'Invoke-WokwiNodeScenarioWithRetry \(''wokwi-'' \+ \$node\)') {
  throw 'Wokwi retry regression: full-mode node scenarios must use the bounded-transport-retry wrapper.'
}
if ($runnerSource -notmatch '\$script:wokwiQuotaExhausted\s*=\s*\$true' -or
    $retryHelper.Groups['body'].Value -notmatch 'if \(\$script:wokwiQuotaExhausted\)' -or
    $retryHelper.Groups['body'].Value -notmatch 'Add-Blocked') {
  throw 'Wokwi quota regression: after quota exhaustion, later cloud scenarios must be BLOCKED without launching more API requests.'
}

Write-Output 'PASS: Wokwi API transport/quota blockers are classified, quota stops later cloud launches, and scenario assertion failures remain FAIL.'
