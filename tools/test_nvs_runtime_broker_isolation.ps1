$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'nvs_runtime_wokwi_config.ps1')

$sample = @'
default_envs = node1, node2, node3
[env]
  -D HOME_ID="demo-home"
  -D MQTT_HOST="host.wokwi.internal"
  -D MQTT_PORT=1883
  -D MQTT_USER="iotlab"
[env:node1]
  -D DEVICE_ID="node1"
; Isolated local Wokwi runtime target
[env:node1_nvs_runtime]
  -D HOME_ID="nvs-runtime-test"
  -D MQTT_HOST="host.wokwi.internal"
  -D MQTT_PORT=1884
  -D MQTT_USER="nvs-runtime-test"
'@
$legacyRewrite = $sample.Replace('MQTT_HOST="host.wokwi.internal"','MQTT_HOST="autotest.invalid"').Replace('MQTT_PORT=1883','MQTT_PORT=1884')
$legacyRuntime = $legacyRewrite.Substring($legacyRewrite.IndexOf('; Isolated local Wokwi runtime target', [System.StringComparison]::Ordinal))
if ($legacyRuntime -notmatch 'MQTT_HOST="autotest.invalid"') {
  throw 'Baseline regression setup failed: the previous global replacement did not reproduce the runtime host corruption.'
}
$actual = Set-IsolatedSimulationNodeBroker -PlatformIoText $sample -TestHomeId 'autotest-case'
$runtime = $actual.Substring($actual.IndexOf('; Isolated local Wokwi runtime target', [System.StringComparison]::Ordinal))
if ($actual -notmatch 'HOME_ID="autotest-case"' -or
    $actual -notmatch 'MQTT_HOST="autotest.invalid"' -or
    $actual -notmatch 'MQTT_USER="autotest"') {
  throw 'Base simulation nodes were not redirected to isolated test broker settings.'
}
if ($runtime -notmatch 'HOME_ID="nvs-runtime-test"' -or
    $runtime -notmatch 'MQTT_HOST="host.wokwi.internal"' -or
    $runtime -notmatch 'MQTT_PORT=1884' -or
    $runtime -notmatch 'MQTT_USER="nvs-runtime-test"') {
  throw 'Regression: isolated NVS runtime target lost its Private Gateway host or port 1884 configuration.'
}
Write-Output 'PASS: reproduced the previous global replacement corruption, then verified cloud isolation preserves the NVS runtime endpoint.'
