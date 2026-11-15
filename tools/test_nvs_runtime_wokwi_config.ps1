$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'nvs_runtime_wokwi_config.ps1')

$legacyConfig = "[wokwi]`r`nversion = 1`r`nfirmware = `"fresh.bin`"`r`nelf = `"fresh.elf`"`rnrfc2217ServerPort = 4000`r`n"
if ($legacyConfig -notmatch 'nrfc2217ServerPort\s*=\s*4000' -or
    $legacyConfig -match '(?m)^rfc2217ServerPort\s*=') {
  throw 'Baseline regression setup failed: the previous concatenated RFC2217 line was not reproduced.'
}

$runtimeDiagram = @'
{"parts":[{"id":"testlogic","attrs":{"channelNames":"BUZZER,GREEN,AMBER,RED","bufferSize":"250000"}}],"connections":[["testlogic:D0","esp:4","purple",[]],["testlogic:D3","esp:7","purple",[]]]}
'@
$triggeredDiagram = (Set-NvsRuntimeLogicAnalyzerTrigger $runtimeDiagram) | ConvertFrom-Json
$analyzer = @($triggeredDiagram.parts | Where-Object { $_.id -eq 'testlogic' })[0]
if ($analyzer.attrs.triggerMode -ne 'edge' -or $analyzer.attrs.triggerPin -ne 'D3' -or $analyzer.attrs.triggerLevel -ne 'high') {
  throw 'Runtime analyzer must begin capture when the red GPIO7 signal rises.'
}

$config = New-NvsRuntimeWokwiConfig `
  -FirmwareRelativePath '../isolated-source/firmware/.pio/build/node1_nvs_runtime/firmware.bin' `
  -ElfRelativePath '../isolated-source/firmware/.pio/build/node1_nvs_runtime/firmware.elf' `
  -Rfc2217ServerPort 41234 `
  -VcdRelativePath 'nvs-runtime-gpio.vcd'

$requiredLines = @(
  '[wokwi]',
  'version = 1',
  'firmware = "../isolated-source/firmware/.pio/build/node1_nvs_runtime/firmware.bin"',
  'elf = "../isolated-source/firmware/.pio/build/node1_nvs_runtime/firmware.elf"',
  'rfc2217ServerPort = 41234',
  'vcdFile = "nvs-runtime-gpio.vcd"'
)
$actualLines = @($config -split "`r?`n" | Where-Object { $_ -ne '' })
if ($actualLines.Count -ne $requiredLines.Count) {
  throw "Wokwi runtime config regression: expected $($requiredLines.Count) lines, got $($actualLines.Count)."
}
for ($index = 0; $index -lt $requiredLines.Count; $index++) {
  if ($actualLines[$index] -cne $requiredLines[$index]) {
    throw "Wokwi runtime config regression: line $($index + 1) expected '$($requiredLines[$index])', got '$($actualLines[$index])'."
  }
}

$runnerPath = Join-Path $PSScriptRoot 'run-all-tests.ps1'
$runnerSource = Get-Content -LiteralPath $runnerPath -Raw
$portHelperSource = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'nvs_runtime_wokwi_config.ps1') -Raw
$testVcd = Join-Path (Join-Path $PSScriptRoot '..\tmp\nvs-runtime-wokwi-config-unit') (('wait-' + [guid]::NewGuid().ToString('N')) + '.vcd')
New-Item -ItemType Directory -Path (Split-Path -Parent $testVcd) -Force | Out-Null
try {
  Set-Content -LiteralPath $testVcd -Value '$enddefinitions $end' -Encoding ascii
  if (-not (Wait-ForNonEmptyFile $testVcd 1)) {
    throw 'VCD shutdown wait regression: a completed non-empty VCD was not detected.'
  }
  Remove-Item -LiteralPath $testVcd -Force
  if (Wait-ForNonEmptyFile $testVcd 0) {
    throw 'VCD shutdown wait regression: a missing artifact was reported as ready.'
  }
} finally {
  if (Test-Path -LiteralPath $testVcd) { Remove-Item -LiteralPath $testVcd -Force }
}
$stopMatch = [regex]::Match($runnerSource, '(?s)function Stop-IsolatedWokwiWindow\([^)]+\)\s*\{(?<body>.*?)\r?\n\}')
if (-not $stopMatch.Success) { throw 'Could not locate Stop-IsolatedWokwiWindow for VCD shutdown regression.' }
$stopBody = $stopMatch.Groups['body'].Value
$stopCommandPosition = $stopBody.IndexOf("Send-IsolatedWokwiCommand `$Marker 'Stop Simulation'", [System.StringComparison]::Ordinal)
$vcdWaitPosition = $stopBody.IndexOf('Wait-ForNonEmptyFile $VcdPath 15', [System.StringComparison]::Ordinal)
$closeWindowPosition = $stopBody.IndexOf("`$shell.SendKeys('^+w')", [System.StringComparison]::Ordinal)
if ($stopCommandPosition -lt 0 -or $vcdWaitPosition -le $stopCommandPosition -or $closeWindowPosition -le $vcdWaitPosition) {
  throw 'VCD shutdown regression: the runner must invoke the registered Stop Simulation command, wait for the VCD, then close the test-owned window.'
}
if ($stopBody.Contains("Send-IsolatedWokwiCommand `$Marker 'Wokwi: Stop Simulation'")) {
  throw 'VCD shutdown regression: the stop command title must not include the nonexistent Wokwi: prefix.'
}
$expectedStopCall = "Stop-IsolatedWokwiWindow `$runtimeWindowMarker (Join-Path `$runtimeSimDir 'nvs-runtime-gpio.vcd')"
if (-not $runnerSource.Contains($expectedStopCall)) {
  throw 'VCD shutdown regression: NVS runtime cleanup must pass its current-run VCD path to the stop helper.'
}
$allocatedPort = Get-FreeNvsRuntimeRfc2217Port
if ($allocatedPort -in @(1883,1884,4000) -or $allocatedPort -lt 1 -or $allocatedPort -gt 65535) {
  throw "Free RFC2217 port allocator returned invalid/reserved port $allocatedPort."
}
$legacyPortProbe = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::IPv6Any,4000)
$legacyPortProbeOwned = $false
try {
  $legacyPortProbe.Server.ExclusiveAddressUse = $true
  $legacyPortProbe.Server.DualMode = $true
  $legacyPortProbe.Start()
  $legacyPortProbeOwned = $true
} catch [System.Net.Sockets.SocketException] {
  # Port 4000 may belong to an existing user's Wokwi session; never stop or connect to it.
}
try {
  $whileLegacyPortOccupied = Get-FreeNvsRuntimeRfc2217Port
  if ($whileLegacyPortOccupied -eq 4000) {
    throw 'RFC2217 allocator selected legacy port 4000 while it was excluded/occupied.'
  }
} finally {
  if ($legacyPortProbeOwned) { $legacyPortProbe.Stop() }
}
$probe6 = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::IPv6Any,$allocatedPort)
$probe6.Server.ExclusiveAddressUse = $true
$probe6.Server.DualMode = $true
$probe6.Start()
$probe4 = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback,$allocatedPort)
$ipv4ConflictDetected = $false
try { $probe4.Start() } catch { $ipv4ConflictDetected = $true } finally { try { $probe4.Stop() } catch {}; $probe6.Stop() }
if (-not $ipv4ConflictDetected) {
  throw 'Dual-stack RFC2217 reservation regression: an IPv4 listener unexpectedly bound while IPv6Any was held.'
}
if ($portHelperSource -notmatch 'IPv6Any' -or $portHelperSource -notmatch 'DualMode\s*=\s*\$true' -or
    $portHelperSource -notmatch 'for\s*\(\$attempt\s*=\s*1;\s*\$attempt\s*-le\s*16') {
  throw 'RFC2217 port allocation must reserve dual-stack space and retry a bounded number of reserved port-0 results.'
}
$startMatch = [regex]::Match($runnerSource, '(?s)function Start-IsolatedWokwiSimulation\([^\r\n]*\)\s*\{(?<body>.*?)\r?\n\}')
if (-not $startMatch.Success) { throw 'Could not locate Start-IsolatedWokwiSimulation for foreground regression.' }
$startBody = $startMatch.Groups['body'].Value
$startCommandPosition = $startBody.IndexOf("Send-IsolatedWokwiCommand `$marker 'Wokwi: Start Simulator'", [System.StringComparison]::Ordinal)
$focusPosition = $startBody.IndexOf('Focus-IsolatedWokwiWindow $marker', [System.StringComparison]::Ordinal)
if ($startCommandPosition -lt 0 -or $focusPosition -le $startCommandPosition) {
  throw 'The isolated Wokwi runtime must activate and foreground its simulator window after issuing Start Simulator.'
}
$focusMatch = [regex]::Match($runnerSource, '(?s)function Focus-IsolatedWokwiWindow\([^\r\n]*\)\s*\{(?<body>.*?)\r?\n\}')
if (-not $focusMatch.Success -or $focusMatch.Groups['body'].Value -notmatch 'AppActivate') {
  throw 'The Wokwi foreground helper must activate only the test-owned VS Code window.'
}
if ($runnerSource -notmatch "firmware_nvs_replay\.py'.*?'--serial-debug'") {
  throw 'The NVS runtime runner must capture PySerial RFC2217 negotiation diagnostics.'
}
$listenerHelper = [regex]::Match($runnerSource, '(?s)function Wait-IsolatedWokwiRfc2217Listener\([^)]+\)\s*\{(?<body>.*?)\r?\n\}')
$ownerHelper = [regex]::Match($portHelperSource, '(?s)function Test-IsolatedWokwiProcessTree\([^)]+\)\s*\{(?<body>.*?)\r?\n\}')
if (-not $listenerHelper.Success -or
    $listenerHelper.Groups['body'].Value -notmatch 'Get-NetTCPConnection' -or
    $listenerHelper.Groups['body'].Value -notmatch 'OwningProcess' -or
    $listenerHelper.Groups['body'].Value -notmatch 'Test-IsolatedWokwiProcessTree' -or
    $listenerHelper.Groups['body'].Value -notmatch 'testRootIds') {
  throw 'The runner must verify that the RFC2217 listener belongs to the isolated test VS Code process tree.'
}
if (-not $ownerHelper.Success -or
    $ownerHelper.Groups['body'].Value -notmatch 'Get-CimInstance' -or
    $ownerHelper.Groups['body'].Value -notmatch 'RootProcessIds\s*-contains') {
  throw 'The runner must walk the RFC2217 listener PID ancestry to a test-owned VS Code window.'
}
$parentMap = @{ 501 = 502; 502 = 700; 601 = 602; 602 = 0 }
$parentLookup = { param([int]$ChildProcessId) return [int]$parentMap[$ChildProcessId] }.GetNewClosure()
if (-not (Test-IsolatedWokwiProcessTree 501 @(700) $parentLookup)) {
  throw 'Listener ownership regression: a child process of the test VS Code window was rejected.'
}
if (Test-IsolatedWokwiProcessTree 601 @(700) $parentLookup) {
  throw 'Listener ownership regression: an unrelated process was accepted as the test Wokwi listener.'
}
$runtimeStartPosition = $runnerSource.IndexOf('$runtimeWindowMarker = Start-IsolatedWokwiSimulation $runtimeSimDir', [System.StringComparison]::Ordinal)
$portAllocationPosition = $runnerSource.IndexOf('$runtimeRfc2217Port = Get-FreeNvsRuntimeRfc2217Port', [System.StringComparison]::Ordinal)
$listenerWaitPosition = $runnerSource.IndexOf('$listenerEvidence = Wait-IsolatedWokwiRfc2217Listener $runtimeRfc2217Port $runtimeWindowMarker 25', [System.StringComparison]::Ordinal)
$driverStartPosition = $runnerSource.IndexOf("Invoke-LoggedProcess 'firmware-nvs-offline-replay-runtime'", [System.StringComparison]::Ordinal)
if ($portAllocationPosition -lt 0 -or $runtimeStartPosition -lt 0 -or $listenerWaitPosition -le $runtimeStartPosition -or $driverStartPosition -le $listenerWaitPosition) {
  throw 'The runtime driver must use an allocated dual-stack port and start only after the test-owned Wokwi listener appears.'
}
$serialUrlArgPosition = $runnerSource.IndexOf("'--serial-url',", [System.StringComparison]::Ordinal)
$dynamicSerialUrlPosition = if ($serialUrlArgPosition -ge 0) {
  $runnerSource.IndexOf('rfc2217://127.0.0.1:$runtimeRfc2217Port', $serialUrlArgPosition, [System.StringComparison]::Ordinal)
} else { -1 }
if ($serialUrlArgPosition -lt 0 -or $dynamicSerialUrlPosition -lt $serialUrlArgPosition) {
  throw 'The Python runtime driver must receive the same dynamically allocated RFC2217 port as wokwi.toml.'
}
Write-Output 'PASS: fresh artifacts, VCD trigger, dual-stack dynamic RFC2217 port reservation/config/URL, legacy-port isolation, test-process listener ownership, foregrounding, and negotiation logging are guarded.'
