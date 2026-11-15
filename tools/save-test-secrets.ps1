param(
  [ValidateSet('Both', 'WokwiToken', 'MqttPassword')]
  [string]$Update = 'Both'
)

$ErrorActionPreference = 'Stop'

if ($env:OS -ne 'Windows_NT') {
  throw 'This secret store requires Windows DPAPI and the current Windows user profile.'
}
if ([string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
  throw 'LOCALAPPDATA is unavailable; cannot create the per-user encrypted secret store.'
}

$storeDirectory = Join-Path $env:LOCALAPPDATA 'SmartGasAlertSystem'
$storePath = Join-Path $storeDirectory 'test-secrets.clixml'
$wokwiSecure = $null
$mqttSecure = $null
try {
  $existing = $null
  if ($Update -ne 'Both') {
    if (-not (Test-Path -LiteralPath $storePath -PathType Leaf)) {
      throw "Cannot update one value because no encrypted store exists yet. Run .\tools\save-test-secrets.ps1 once without -Update first."
    }
    $existing = Import-Clixml -LiteralPath $storePath
    if ($existing.schema -ne 1 -or
        [string]::IsNullOrWhiteSpace([string]$existing.WOKWI_CLI_TOKEN) -or
        [string]::IsNullOrWhiteSpace([string]$existing.GAS_MQTT_PASSWORD)) {
      throw 'The existing encrypted store is invalid; no value was changed.'
    }
  }

  $encryptedWokwi = if ($existing) { [string]$existing.WOKWI_CLI_TOKEN } else { '' }
  $encryptedMqtt = if ($existing) { [string]$existing.GAS_MQTT_PASSWORD } else { '' }
  if ($Update -in @('Both', 'WokwiToken')) {
    $wokwiSecure = Read-Host 'Wokwi CLI token (saved encrypted for this Windows user)' -AsSecureString
    if ($wokwiSecure.Length -eq 0) { throw 'Wokwi token is empty. Nothing was saved.' }
    $encryptedWokwi = ConvertFrom-SecureString -SecureString $wokwiSecure
  }
  if ($Update -in @('Both', 'MqttPassword')) {
    $mqttSecure = Read-Host 'MQTT build password (saved encrypted for this Windows user)' -AsSecureString
    if ($mqttSecure.Length -eq 0) { throw 'MQTT password is empty. Nothing was saved.' }
    $encryptedMqtt = ConvertFrom-SecureString -SecureString $mqttSecure
  }

  New-Item -ItemType Directory -Path $storeDirectory -Force | Out-Null
  $encryptedStore = [pscustomobject]@{
    schema = 1
    WOKWI_CLI_TOKEN = $encryptedWokwi
    GAS_MQTT_PASSWORD = $encryptedMqtt
  }
  $encryptedStore | Export-Clixml -LiteralPath $storePath -Force
  Write-Host "Saved encrypted test credentials for this Windows user: $storePath (updated: $Update)"
  Write-Host 'The credential values were not written to the project or printed.'
}
finally {
  if ($wokwiSecure) { $wokwiSecure.Dispose() }
  if ($mqttSecure) { $mqttSecure.Dispose() }
}
