function Get-WokwiTransportBlockReason {
  param(
    [Parameter(Mandatory = $true)][string]$TestName,
    [Parameter(Mandatory = $true)][int]$ExitCode,
    [AllowEmptyString()][string]$ProcessOutput
  )

  if ($TestName -like 'wokwi-*' -and $ExitCode -ne 0 -and
      $ProcessOutput -match '(?im)^\s*API Error:\s*Connection to transport closed unexpectedly:\s*code 1006\s*$') {
    return 'Wokwi Simulation API transport closed before scenario evidence was produced; the scenario was not evaluated.'
  }
  if ($TestName -like 'wokwi-*' -and $ExitCode -ne 0 -and
      $ProcessOutput -match '(?i)You have used up your Free plan monthly CI minute quota') {
    return 'Wokwi Free plan monthly CI minute quota is exhausted; the scenario was not evaluated. No further cloud scenarios should be launched in this run.'
  }
  return $null
}

function Test-WokwiTransportRetryEligible {
  param(
    [Parameter(Mandatory = $true)][string]$TestName,
    [Parameter(Mandatory = $true)][int]$Attempt,
    [Parameter(Mandatory = $true)][string]$Status,
    [Parameter(Mandatory = $true)][int]$ExitCode,
    [AllowEmptyString()][string]$ProcessOutput
  )

  return ($Attempt -eq 1 -and
    $TestName -match '^wokwi-node[123]$' -and
    $Status -eq 'BLOCKED' -and
    $ExitCode -eq 77 -and
    $ProcessOutput -match '(?im)^\s*API Error:\s*Connection to transport closed unexpectedly:\s*code 1006\s*$')
}
