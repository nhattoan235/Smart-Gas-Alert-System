param(
  [ValidateSet('quick', 'full')]
  [string]$Mode = 'full'
)

$ErrorActionPreference = 'Stop'
if ($env:OS -ne 'Windows_NT') {
  throw 'Saved test credentials are protected with Windows DPAPI and can only be used on Windows.'
}
if ([string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
  throw 'LOCALAPPDATA is unavailable; cannot load the per-user encrypted test credentials.'
}

$storePath = Join-Path (Join-Path $env:LOCALAPPDATA 'SmartGasAlertSystem') 'test-secrets.clixml'
if (-not (Test-Path -LiteralPath $storePath -PathType Leaf)) {
  throw "Encrypted credentials are not saved. Run .\tools\save-test-secrets.ps1 once first. Store: $storePath"
}

$secretStore = Import-Clixml -LiteralPath $storePath
if ($secretStore.schema -ne 1 -or
    [string]::IsNullOrWhiteSpace([string]$secretStore.WOKWI_CLI_TOKEN) -or
    [string]::IsNullOrWhiteSpace([string]$secretStore.GAS_MQTT_PASSWORD)) {
  throw "Encrypted credential store is invalid. Run .\tools\save-test-secrets.ps1 to replace it. Store: $storePath"
}

$oldToken = [Environment]::GetEnvironmentVariable('WOKWI_CLI_TOKEN', 'Process')
$oldMqtt = [Environment]::GetEnvironmentVariable('GAS_MQTT_PASSWORD', 'Process')
$wokwiSecure = $null
$mqttSecure = $null
$exitCode = 1
try {
  if ([string]::IsNullOrWhiteSpace($env:WOKWI_CLI_TOKEN)) {
    $wokwiSecure = ConvertTo-SecureString -String ([string]$secretStore.WOKWI_CLI_TOKEN)
    $env:WOKWI_CLI_TOKEN = [System.Net.NetworkCredential]::new('', $wokwiSecure).Password
  }
  if ([string]::IsNullOrWhiteSpace($env:GAS_MQTT_PASSWORD)) {
    $mqttSecure = ConvertTo-SecureString -String ([string]$secretStore.GAS_MQTT_PASSWORD)
    $env:GAS_MQTT_PASSWORD = [System.Net.NetworkCredential]::new('', $mqttSecure).Password
  }
  if ([string]::IsNullOrWhiteSpace($env:WOKWI_CLI_TOKEN) -or
      [string]::IsNullOrWhiteSpace($env:GAS_MQTT_PASSWORD)) {
    throw 'A required test credential is empty; refusing to run the full test suite.'
  }

  $engineName = if ($PSVersionTable.PSEdition -eq 'Core') { 'pwsh.exe' } else { 'powershell.exe' }
  $engine = Join-Path $PSHOME $engineName
  if (-not (Test-Path -LiteralPath $engine -PathType Leaf)) {
    throw "Could not locate the current PowerShell executable: $engine"
  }
  $runner = Join-Path $PSScriptRoot 'run-all-tests.ps1'
  & $engine -NoLogo -NoProfile -ExecutionPolicy Bypass -File $runner -Mode $Mode
  $exitCode = $LASTEXITCODE
}
finally {
  if ($null -eq $oldToken) { Remove-Item Env:WOKWI_CLI_TOKEN -ErrorAction SilentlyContinue }
  else { $env:WOKWI_CLI_TOKEN = $oldToken }
  if ($null -eq $oldMqtt) { Remove-Item Env:GAS_MQTT_PASSWORD -ErrorAction SilentlyContinue }
  else { $env:GAS_MQTT_PASSWORD = $oldMqtt }
  if ($wokwiSecure) { $wokwiSecure.Dispose() }
  if ($mqttSecure) { $mqttSecure.Dispose() }
}
exit $exitCode
