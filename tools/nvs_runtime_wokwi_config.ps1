function Get-FreeNvsRuntimeRfc2217Port {
  $excludedPorts = @(1883,1884,4000)
  for ($attempt = 1; $attempt -le 16; $attempt++) {
    $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::IPv6Any, 0)
    try {
      $listener.Server.ExclusiveAddressUse = $true
      $listener.Server.DualMode = $true
      $listener.Start()
      $port = ([System.Net.IPEndPoint]$listener.LocalEndpoint).Port
      if ($port -notin $excludedPorts) {
        return [int]$port
      }
    } catch {
      throw "Could not allocate a free dual-stack RFC2217 port without touching an existing listener ($($_.Exception.GetType().Name)): $($_.Exception.Message)"
    } finally {
      try { $listener.Stop() } catch {}
    }
  }
  throw "Could not obtain a non-reserved dual-stack RFC2217 port after 16 OS allocations; excluded ports: $($excludedPorts -join ', ')."
}

function Test-IsolatedWokwiProcessTree([int]$OwnerProcessId, [int[]]$RootProcessIds,
                                      [scriptblock]$ParentLookup) {
  if (-not $ParentLookup) {
    $ParentLookup = {
      param([int]$ChildProcessId)
      $processInfo = Get-CimInstance -ClassName Win32_Process -Filter "ProcessId = $ChildProcessId" -ErrorAction Stop
      if ($processInfo) { return [int]$processInfo.ParentProcessId }
      return 0
    }
  }
  $cursorPid = $OwnerProcessId
  for ($depth = 0; $depth -lt 12 -and $cursorPid -gt 0; $depth++) {
    if ($RootProcessIds -contains $cursorPid) { return $true }
    try {
      $parentPid = [int](& $ParentLookup $cursorPid)
    } catch {
      throw "Could not inspect parent PID for RFC2217 listener process $cursorPid ($($_.Exception.GetType().Name): $($_.Exception.Message))."
    }
    if ($parentPid -eq 0 -or $parentPid -eq $cursorPid) { return $false }
    $cursorPid = $parentPid
  }
  return ($RootProcessIds -contains $cursorPid)
}

function New-NvsRuntimeWokwiConfig {
  param(
    [Parameter(Mandatory=$true)][string]$FirmwareRelativePath,
    [Parameter(Mandatory=$true)][string]$ElfRelativePath,
    [Parameter(Mandatory=$true)][ValidateRange(1,65535)][int]$Rfc2217ServerPort,
    [Parameter(Mandatory=$true)][string]$VcdRelativePath
  )

  if ($FirmwareRelativePath -match '["\r\n]' -or $ElfRelativePath -match '["\r\n]' -or $VcdRelativePath -match '["\r\n]') {
    throw 'Wokwi artifact and VCD paths must be single-line relative paths without quotes.'
  }
  return @"
[wokwi]
version = 1
firmware = "$FirmwareRelativePath"
elf = "$ElfRelativePath"
rfc2217ServerPort = $Rfc2217ServerPort
vcdFile = "$VcdRelativePath"
"@
}

function Wait-ForNonEmptyFile([string]$Path, [int]$TimeoutSeconds = 15) {
  if ($TimeoutSeconds -lt 0) { throw 'TimeoutSeconds must be non-negative.' }
  $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
  do {
    if (Test-Path -LiteralPath $Path -PathType Leaf) {
      try {
        if ((Get-Item -LiteralPath $Path -ErrorAction Stop).Length -gt 0) { return $true }
      } catch {}
    }
    if ((Get-Date) -ge $deadline) { break }
    Start-Sleep -Milliseconds 100
  } while ($true)
  return $false
}

function Set-NvsRuntimeLogicAnalyzerTrigger {
  param([Parameter(Mandatory=$true)][string]$DiagramJson)

  $diagram = $DiagramJson | ConvertFrom-Json
  $analyzers = @($diagram.parts | Where-Object { $_.id -eq 'testlogic' })
  if ($analyzers.Count -ne 1) {
    throw "NVS runtime diagram must contain exactly one 'testlogic' analyzer; found $($analyzers.Count)."
  }
  if (@($diagram.connections | Where-Object { $_[0] -eq 'testlogic:D3' -and $_[1] -eq 'esp:7' }).Count -ne 1) {
    throw 'NVS runtime analyzer D3 must be connected to ESP GPIO7 (red LED).'
  }
  if (@($diagram.connections | Where-Object { $_[0] -eq 'testlogic:D0' -and $_[1] -eq 'esp:4' }).Count -ne 1) {
    throw 'NVS runtime analyzer D0 must be connected to ESP GPIO4 (buzzer).'
  }
  $analyzers[0].attrs | Add-Member -NotePropertyName triggerMode -NotePropertyValue 'edge' -Force
  $analyzers[0].attrs | Add-Member -NotePropertyName triggerPin -NotePropertyValue 'D3' -Force
  $analyzers[0].attrs | Add-Member -NotePropertyName triggerLevel -NotePropertyValue 'high' -Force
  return (ConvertTo-Json -InputObject $diagram -Depth 50)
}

function Set-IsolatedSimulationNodeBroker {
  param(
    [Parameter(Mandatory=$true)][string]$PlatformIoText,
    [Parameter(Mandatory=$true)][string]$TestHomeId
  )

  $runtimeMarker = '; Isolated local Wokwi runtime target'
  $runtimeIndex = $PlatformIoText.IndexOf($runtimeMarker, [System.StringComparison]::Ordinal)
  if ($runtimeIndex -lt 0) {
    throw "PlatformIO config is missing protected runtime target marker '$runtimeMarker'."
  }
  $baseConfig = $PlatformIoText.Substring(0, $runtimeIndex)
  $protectedConfig = $PlatformIoText.Substring($runtimeIndex)
  $baseConfig = $baseConfig.Replace('demo-home', $TestHomeId)
  return $baseConfig + $protectedConfig
}

function Get-IsolatedSimulationNodeBrokerEnvironment {
  return @{
    MQTT_HOST = 'autotest.invalid'
    MQTT_PORT = '1884'
    MQTT_USER = 'autotest'
  }
}
