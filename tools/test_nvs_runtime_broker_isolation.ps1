$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'nvs_runtime_wokwi_config.ps1')

$sample = @"
default_envs = node1, node2, node3
[env]
  -D HOME_ID="demo-home"
[env:node1]
  -D DEVICE_ID="node1"
; Isolated local Wokwi runtime target
[env:node1_nvs_runtime]
  -D HOME_ID="nvs-runtime-test"
  -D MQTT_HOST="host.wokwi.internal"
  -D MQTT_PORT=1884
  -D MQTT_USER="nvs-runtime-test"
"@
$actual = Set-IsolatedSimulationNodeBroker -PlatformIoText $sample -TestHomeId 'autotest-case'
$runtimeMarker = '; Isolated local Wokwi runtime target'
$runtimeIndex = $actual.IndexOf($runtimeMarker, [System.StringComparison]::Ordinal)
if ($runtimeIndex -lt 0) {
  throw 'Isolated NVS runtime profile marker was lost.'
}
$base = $actual.Substring(0, $runtimeIndex)
$runtime = $actual.Substring($runtimeIndex)
$brokerEnvironment = Get-IsolatedSimulationNodeBrokerEnvironment

if (-not $base.Contains('autotest-case') -or $base -match 'MQTT_HOST|MQTT_PORT|MQTT_USER') {
  throw 'Normal node config must isolate HOME_ID while taking broker settings from the environment.'
}
if ($brokerEnvironment.MQTT_HOST -ne 'autotest.invalid' -or
    $brokerEnvironment.MQTT_PORT -ne '1884' -or
    $brokerEnvironment.MQTT_USER -ne 'autotest') {
  throw 'Normal node builds were not assigned the isolated test broker environment.'
}
if (-not $runtime.Contains('HOME_ID="nvs-runtime-test"') -or
    -not $runtime.Contains('MQTT_HOST="host.wokwi.internal"') -or
    -not $runtime.Contains('MQTT_PORT=1884') -or
    -not $runtime.Contains('MQTT_USER="nvs-runtime-test"')) {
  throw 'Regression: isolated NVS runtime target lost its private-gateway endpoint or test credentials.'
}
Write-Output 'PASS: node builds receive an isolated broker environment and the NVS runtime profile keeps its separate endpoint.'
