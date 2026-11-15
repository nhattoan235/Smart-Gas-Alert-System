param(
  [ValidateSet('quick', 'full')]
  [string]$Mode = 'quick'
)

$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$runId = (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssfffZ')
$resultRoot = Join-Path $repo 'test-results'
$runDir = Join-Path $resultRoot $runId
New-Item -ItemType Directory -Path $runDir -Force | Out-Null

$script:tests = [System.Collections.Generic.List[object]]::new()
$script:timeline = [System.Collections.Generic.List[object]]::new()
$script:runnerErrors = [System.Collections.Generic.List[object]]::new()
$script:python = Get-Command python -ErrorAction SilentlyContinue
$script:node = Get-Command node -ErrorAction SilentlyContinue
$script:powerShellVersion = $PSVersionTable.PSVersion.ToString()
$script:pioCoreDir = Join-Path $runDir 'platformio-core'
$script:pioPackagesDir = $null
$script:pioModule = $false
$script:pythonVersion = 'unavailable'
$script:pioVersion = 'unavailable'
$script:wokwiVersion = 'unavailable'
$script:mqttPasswordPresent = -not [string]::IsNullOrWhiteSpace($env:GAS_MQTT_PASSWORD)
$script:mosquittoVersion = 'unavailable'
function Invoke-ScrubbedVersionProbe([string]$Executable, [string[]]$Arguments) {
  $sensitiveEnvironmentNames = @('WOKWI_CLI_TOKEN','GAS_MQTT_PASSWORD','MQTT_PASSWORD','WIFI_PASSWORD','GAS_SYNC_TOKEN','GAS_SYNC_API_TOKEN','MQTT_USER')
  $savedEnvironment = @{}
  foreach ($name in $sensitiveEnvironmentNames) {
    $current = Get-Item -LiteralPath ("Env:" + $name) -ErrorAction SilentlyContinue
    if ($current) { $savedEnvironment[$name] = $current.Value }
    Remove-Item -LiteralPath ("Env:" + $name) -ErrorAction SilentlyContinue
  }
  $output = ''
  $exitCode = $null
  try {
    $output = (& $Executable @Arguments 2>&1 | Out-String).Trim()
    $exitCode = $LASTEXITCODE
  } finally {
    foreach ($name in $sensitiveEnvironmentNames) {
      Remove-Item -LiteralPath ("Env:" + $name) -ErrorAction SilentlyContinue
      if ($savedEnvironment.ContainsKey($name)) { Set-Item -LiteralPath ("Env:" + $name) -Value ([string]$savedEnvironment[$name]) }
    }
  }
  return [pscustomobject]@{ output = $output; exit_code = $exitCode }
}
if ($script:python) {
  $pythonVersionProbe = Invoke-ScrubbedVersionProbe $script:python.Source @('--version')
  $script:pythonVersion = $pythonVersionProbe.output
  $pioVersionProbe = Invoke-ScrubbedVersionProbe $script:python.Source @('-m','platformio','--version')
  $script:pioModule = ($pioVersionProbe.exit_code -eq 0)
  if ($script:pioModule) { $script:pioVersion = $pioVersionProbe.output }
}
$script:wokwiPath = $null
$wokwiCommand = Get-Command wokwi-cli -ErrorAction SilentlyContinue | Where-Object { $_.CommandType -eq 'Application' } | Select-Object -First 1
if (-not $wokwiCommand) { $wokwiCommand = Get-Command wokwi-cli.exe -ErrorAction SilentlyContinue | Where-Object { $_.CommandType -eq 'Application' } | Select-Object -First 1 }
if ($wokwiCommand) {
  $script:wokwiPath = $wokwiCommand.Source
  if (-not $script:wokwiPath) { $script:wokwiPath = $wokwiCommand.Path }
}
if (-not $script:wokwiPath -and -not [string]::IsNullOrWhiteSpace($env:USERPROFILE)) {
  $wokwiCandidate = Join-Path $env:USERPROFILE '.wokwi\bin\wokwi-cli.exe'
  if (Test-Path -LiteralPath $wokwiCandidate -PathType Leaf) { $script:wokwiPath = $wokwiCandidate }
}
$script:wokwiTokenPresent = -not [string]::IsNullOrWhiteSpace($env:WOKWI_CLI_TOKEN)
if ($script:wokwiPath) {
  $wokwiVersionProbe = Invoke-ScrubbedVersionProbe $script:wokwiPath @('--version')
  $script:wokwiVersion = $wokwiVersionProbe.output
}
$script:mosquitto = Get-Command mosquitto -ErrorAction SilentlyContinue
if ($script:mosquitto) {
  $mosquittoVersionProbe = Invoke-ScrubbedVersionProbe $script:mosquitto.Source @('-h')
  $script:mosquittoVersion = ($mosquittoVersionProbe.output -split "`r?`n" | Select-Object -First 1).Trim()
}
$script:mosquittoPub = Get-Command mosquitto_pub -ErrorAction SilentlyContinue
$script:mosquittoSub = Get-Command mosquitto_sub -ErrorAction SilentlyContinue
$script:cpp = Get-Command g++ -ErrorAction SilentlyContinue
$script:cppKind = ''
if ($script:cpp) { $script:cppKind = 'gnu' }
if (-not $script:cpp) {
  $script:cpp = Get-Command clang++ -ErrorAction SilentlyContinue
  if ($script:cpp) { $script:cppKind = 'clang' }
}
if (-not $script:cpp) {
  $script:cpp = Get-Command cl.exe -CommandType Application -ErrorAction SilentlyContinue |
    Select-Object -First 1
  if ($script:cpp) { $script:cppKind = 'msvc' }
}
$script:port1883Listener = $null
$script:port1884Listener = $null
try {
  $netstat = Get-Command netstat -ErrorAction Stop
  $netstatLines = & $netstat.Source -ano -p tcp 2>$null
  $script:port1883Listener = [bool]($netstatLines | Select-String -Pattern '^\s*TCP\s+\S+:1883\s+\S+\s+LISTENING\b')
  $script:port1884Listener = [bool]($netstatLines | Select-String -Pattern '^\s*TCP\s+\S+:1884\s+\S+\s+LISTENING\b')
} catch {}

function Write-EmptyLog([string]$Name, [string]$Reason) {
  $path = Join-Path $runDir $Name
  if (-not (Test-Path $path)) { Set-Content -Path $path -Value $Reason -Encoding utf8 }
}

function Write-Utf8NoBom([string]$Path, [string]$Text) {
  [System.IO.File]::WriteAllText($Path, $Text, [System.Text.UTF8Encoding]::new($false))
}

function Add-Result([string]$Name, [string]$Status, [int]$ExitCode, [string]$Log, [string]$Detail, [datetime]$Start, [datetime]$End, [string]$Command = '', [Nullable[bool]]$ProcessStarted = $null, [string]$ExceptionType = '', [string]$ExceptionMessage = '', [string]$ExceptionScript = '', [Nullable[int]]$ExceptionLine = $null, [string]$ExceptionStack = '') {
  $duration = [math]::Round(($End - $Start).TotalSeconds, 3)
  $script:tests.Add([pscustomobject]@{
    name = $Name; status = $Status; exit_code = $ExitCode; log = $Log
    detail = $Detail; duration_seconds = $duration
    command = (Protect-DiagnosticText $Command); process_started = $ProcessStarted
    exception_type = $ExceptionType; exception_message = (Protect-DiagnosticText $ExceptionMessage)
    exception_script = $ExceptionScript; exception_line = $ExceptionLine
    exception_stack = (Protect-DiagnosticText $ExceptionStack)
    started_utc = $Start.ToUniversalTime().ToString('o'); ended_utc = $End.ToUniversalTime().ToString('o')
  })
  $script:timeline.Add([pscustomobject]@{
    test = $Name; status = $Status; started_utc = $Start.ToUniversalTime().ToString('o')
    ended_utc = $End.ToUniversalTime().ToString('o'); duration_seconds = $duration
  })
}

function Protect-DiagnosticText([AllowNull()][string]$Text) {
  if ($null -eq $Text) { return '' }
  $safe = [regex]::Replace($Text, 'wok_[A-Za-z0-9_-]{20,}', '[REDACTED_WOKWI_TOKEN]')
  $safe = [regex]::Replace($safe, '(?i)((?:WOKWI_CLI_TOKEN|GAS_MQTT_PASSWORD|MQTT_PASSWORD|WIFI_PASSWORD|GAS_SYNC_TOKEN|GAS_SYNC_API_TOKEN)\s*[=:]\s*)\S+', '$1[REDACTED]')
  foreach ($name in @('GAS_MQTT_PASSWORD','MQTT_PASSWORD','WIFI_PASSWORD','GAS_SYNC_TOKEN','GAS_SYNC_API_TOKEN','WOKWI_CLI_TOKEN')) {
    $secret = [Environment]::GetEnvironmentVariable($name, 'Process')
    if (-not [string]::IsNullOrEmpty($secret)) { $safe = $safe.Replace($secret, '[REDACTED]') }
  }
  return $safe
}

function ConvertTo-WindowsCommandLineArgument([AllowNull()][string]$Argument) {
  if ($null -eq $Argument) { $Argument = '' }
  if ($Argument.Length -gt 0 -and $Argument -notmatch '[\s"]') { return $Argument }
  $builder = [System.Text.StringBuilder]::new()
  [void]$builder.Append([char]34)
  $backslashes = 0
  foreach ($character in $Argument.ToCharArray()) {
    if ($character -eq [char]92) { $backslashes++; continue }
    if ($character -eq [char]34) {
      for ($i = 0; $i -lt (2 * $backslashes + 1); $i++) { [void]$builder.Append([char]92) }
      [void]$builder.Append([char]34)
      $backslashes = 0
      continue
    }
    for ($i = 0; $i -lt $backslashes; $i++) { [void]$builder.Append([char]92) }
    $backslashes = 0
    [void]$builder.Append($character)
  }
  for ($i = 0; $i -lt (2 * $backslashes); $i++) { [void]$builder.Append([char]92) }
  [void]$builder.Append([char]34)
  return $builder.ToString()
}

function Format-ProcessCommand([string]$Executable, [string[]]$Arguments) {
  $quoted = @($Arguments | ForEach-Object { ConvertTo-WindowsCommandLineArgument ([string]$_) })
  $line = if ($quoted.Count -gt 0) { $Executable + ' ' + ($quoted -join ' ') } else { $Executable }
  return (Protect-DiagnosticText $line)
}

function Stop-ChildProcessTree([System.Diagnostics.Process]$Process) {
  if (-not $Process) { $script:lastTreeStopDetail = 'No process object was available.'; return $false }
  $observedIds = [System.Collections.Generic.HashSet[int]]::new()
  $treeEnumerated = $false
  try {
    $processSnapshot = @(Get-CimInstance -ClassName Win32_Process -ErrorAction Stop | Select-Object ProcessId,ParentProcessId)
    [void]$observedIds.Add([int]$Process.Id)
    $changed = $true
    while ($changed) {
      $changed = $false
      foreach ($entry in $processSnapshot) {
        if ($observedIds.Contains([int]$entry.ParentProcessId) -and $observedIds.Add([int]$entry.ProcessId)) { $changed = $true }
      }
    }
    $treeEnumerated = $true
  } catch {
    $script:lastTreeStopDetail = 'Could not enumerate the process tree before termination; tree termination cannot be confirmed.'
  }
  $taskkill = Get-Command taskkill.exe -ErrorAction SilentlyContinue
  if (-not $taskkill -and -not [string]::IsNullOrWhiteSpace($env:SystemRoot)) {
    $taskkillPath = Join-Path $env:SystemRoot 'System32\taskkill.exe'
    if (Test-Path -LiteralPath $taskkillPath -PathType Leaf) { $taskkill = [pscustomobject]@{ Source = $taskkillPath } }
  }
  $taskkillExitCode = $null
  if ($taskkill) {
    try { $taskkillOutput = & $taskkill.Source /PID $Process.Id /T /F 2>&1; $taskkillExitCode = $LASTEXITCODE } catch { $taskkillExitCode = -1 }
  }
  $fallbackStopAttempted = $false
  if ($taskkillExitCode -ne 0) {
    $fallbackStopAttempted = $true
    foreach ($processId in @($observedIds | Sort-Object -Descending)) {
      try { Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue } catch {}
    }
    try { if (-not $Process.HasExited) { $Process.Kill() } } catch {}
  }
  try { if (-not $Process.HasExited) { [void]$Process.WaitForExit(5000) } } catch {}
  $deadline = [DateTime]::UtcNow.AddSeconds(5)
  $remainingIds = @()
  do {
    $remainingIds = @($observedIds | Where-Object { Get-Process -Id $_ -ErrorAction SilentlyContinue })
    if ($remainingIds.Count -eq 0) { break }
    Start-Sleep -Milliseconds 100
  } while ([DateTime]::UtcNow -lt $deadline)
  if ($treeEnumerated -and $taskkillExitCode -eq 0 -and $remainingIds.Count -eq 0) {
    $script:lastTreeStopDetail = "taskkill /T exited 0; all $($observedIds.Count) process IDs observed in the tree before termination are confirmed exited."
    return $true
  }
  if ($remainingIds.Count -gt 0) {
    $script:lastTreeStopDetail = "Could not confirm process IDs exited: $($remainingIds -join ', '). taskkill exit code: $taskkillExitCode; fallback stop attempted: $fallbackStopAttempted."
  } elseif (-not $treeEnumerated) {
    $script:lastTreeStopDetail = 'The parent exit was observed, but the process tree could not be enumerated; termination is not marked confirmed.'
  } else {
    $script:lastTreeStopDetail = "All observed process IDs exited, but taskkill /T confirmation was unavailable (exit code: $taskkillExitCode; fallback stop attempted: $fallbackStopAttempted); tree termination is not marked confirmed."
  }
  return $false
}

function Write-RunnerDiagnostic([string]$Name, [string]$Command, [string]$Category, [System.Management.Automation.ErrorRecord]$ErrorRecord) {
  $invocation = $ErrorRecord.InvocationInfo
  $entry = [ordered]@{
    test = $Name
    category = $Category
    command = (Protect-DiagnosticText $Command)
    exception_type = $ErrorRecord.Exception.GetType().FullName
    exception_message = (Protect-DiagnosticText $ErrorRecord.Exception.Message)
    script_stack_trace = (Protect-DiagnosticText $ErrorRecord.ScriptStackTrace)
    script_file = if ($invocation -and $invocation.ScriptName) { $invocation.ScriptName } else { $null }
    script_line = if ($invocation -and $invocation.ScriptLineNumber) { $invocation.ScriptLineNumber } else { $null }
    invocation_position = if ($invocation) { Protect-DiagnosticText $invocation.PositionMessage } else { '' }
    exception_detail = (Protect-DiagnosticText ($ErrorRecord.Exception.ToString()))
  }
  $script:runnerErrors.Add($entry)
}

function Invoke-LoggedProcess([string]$Name, [string]$Executable, [string[]]$Arguments, [string]$WorkingDirectory, [string]$LogName, [int]$TimeoutSeconds, [string]$Detail = '') {
  $start = Get-Date
  $logPath = Join-Path $runDir $LogName
  $safeCommand = Format-ProcessCommand $Executable $Arguments
  if (-not (Test-Path -LiteralPath $Executable -PathType Leaf)) {
    Set-Content -Path $logPath -Value "BLOCKED: executable not found: $Executable`r`nCOMMAND: $safeCommand`r`nPROCESS_STARTED: False`r`nEXIT_CODE: 127" -Encoding utf8
    Add-Result $Name 'BLOCKED' 127 $LogName $(if ($Detail) { $Detail } else { 'Executable unavailable.' }) $start (Get-Date) $safeCommand $false
    return
  }
  $proc = $null
  $processStarted = $false
  try {
    $psi = [System.Diagnostics.ProcessStartInfo]::new()
    $psi.FileName = $Executable
    $psi.WorkingDirectory = $WorkingDirectory
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    # EnvironmentVariables and Arguments are available on both Windows PowerShell 5.1
    # (.NET Framework) and PowerShell 7 (.NET). ProcessStartInfo.ArgumentList is not.
    $psi.EnvironmentVariables['TEMP'] = $runDir
    $psi.EnvironmentVariables['TMP'] = $runDir
    $psi.EnvironmentVariables['TMPDIR'] = $runDir
    # Clear credentials for ordinary tests. A real MQTT password is added only
    # to the isolated PlatformIO child process after the full-mode preflight.
    $psi.EnvironmentVariables['WOKWI_CLI_TOKEN'] = ''
    $psi.EnvironmentVariables['GAS_MQTT_PASSWORD'] = ''
    $psi.EnvironmentVariables['MQTT_PASSWORD'] = ''
    $psi.EnvironmentVariables['WIFI_PASSWORD'] = ''
    $psi.EnvironmentVariables['GAS_SYNC_TOKEN'] = ''
    $psi.EnvironmentVariables['GAS_SYNC_API_TOKEN'] = ''
    $psi.EnvironmentVariables['MQTT_USER'] = ''
    $psi.EnvironmentVariables['GAS_E2E_TMPDIR'] = ''
    if ($Name -eq 'firmware-build-node1-node2-node3') {
      $buildPassword = [string]$env:GAS_MQTT_PASSWORD
      if ([string]::IsNullOrWhiteSpace($buildPassword)) { throw 'GAS_MQTT_PASSWORD became empty before PlatformIO launch; refusing to start the build.' }
      $psi.EnvironmentVariables['GAS_MQTT_PASSWORD'] = $buildPassword
      $pioCache = Join-Path $script:pioCoreDir 'cache'
      $pioGlobalLibs = Join-Path $script:pioCoreDir 'globallib'
      $localPlatforms = Join-Path $script:pioCoreDir 'platforms'
      $existingPioCore = if (-not [string]::IsNullOrWhiteSpace($env:USERPROFILE)) { Join-Path $env:USERPROFILE '.platformio' } else { '' }
      $existingPlatforms = if ($existingPioCore) { Join-Path $existingPioCore 'platforms' } else { '' }
      $existingPackages = if ($existingPioCore) { Join-Path $existingPioCore 'packages' } else { '' }
      New-Item -ItemType Directory -Path $script:pioCoreDir,$pioCache,$pioGlobalLibs,$localPlatforms -Force | Out-Null
      $psi.EnvironmentVariables['PLATFORMIO_CORE_DIR'] = $script:pioCoreDir
      $psi.EnvironmentVariables['PLATFORMIO_CACHE_DIR'] = $pioCache
      $psi.EnvironmentVariables['PLATFORMIO_GLOBALLIB_DIR'] = $pioGlobalLibs
      $psi.EnvironmentVariables['PLATFORMIO_DISABLE_UPGRADE_CHECK'] = 'true'
      if ($existingPlatforms -and (Test-Path -LiteralPath (Join-Path $existingPlatforms 'espressif32') -PathType Container)) {
        $localPlatform = Join-Path $localPlatforms 'espressif32'
        if (-not (Test-Path -LiteralPath $localPlatform -PathType Container)) {
          Copy-Item -LiteralPath (Join-Path $existingPlatforms 'espressif32') -Destination $localPlatform -Recurse
        }
      }
      # Keep mutable PlatformIO core/platform state in the run directory. Reuse
      # the exact installed global packages folder directly; never create a
      # writable junction to package payloads.
      $psi.EnvironmentVariables['PLATFORMIO_PLATFORMS_DIR'] = $localPlatforms
      if (-not $existingPackages -or -not (Test-Path -LiteralPath $existingPackages -PathType Container)) {
        throw 'The existing PlatformIO global packages directory is unavailable; refusing to download or install packages during this run.'
      }
      $psi.EnvironmentVariables['PLATFORMIO_PACKAGES_DIR'] = $existingPackages
      $script:pioPackagesDir = $existingPackages
    }
    if ($Name -eq 'mqtt-sqlite-app-ack-dedup-harness') {
      $psi.EnvironmentVariables['GAS_E2E_TMPDIR'] = $runDir
    }
    if ($Name -like 'wokwi-node*' -and $script:wokwiTokenPresent) {
      $psi.EnvironmentVariables['WOKWI_CLI_TOKEN'] = [string]$env:WOKWI_CLI_TOKEN
    }
    $quotedArguments = @($Arguments | ForEach-Object { ConvertTo-WindowsCommandLineArgument ([string]$_) })
    $psi.Arguments = $quotedArguments -join ' '
    $proc = [System.Diagnostics.Process]::new()
    $proc.StartInfo = $psi
    [void]$proc.Start()
    $processStarted = $true
    $stdoutTask = $proc.StandardOutput.ReadToEndAsync()
    $stderrTask = $proc.StandardError.ReadToEndAsync()
    $finished = $proc.WaitForExit($TimeoutSeconds * 1000)
    if (-not $finished) {
      $treeStopped = Stop-ChildProcessTree $proc
      if ($treeStopped) {
        $code = 124
        $status = 'FAIL'
        $extra = "Timed out after $TimeoutSeconds seconds; $script:lastTreeStopDetail"
      } else {
        $code = 126
        $status = 'BLOCKED'
        $extra = "Timed out after $TimeoutSeconds seconds; $script:lastTreeStopDetail"
      }
    } else {
      $code = $proc.ExitCode
      $status = if ($code -eq 0) { 'PASS' } else { 'FAIL' }
      $extra = if ($Detail) { $Detail } else { 'Process exit code is the test assertion.' }
    }
    $stdoutReady = $stdoutTask.Wait(5000)
    $stderrReady = $stderrTask.Wait(5000)
    if ($stdoutReady) { $stdout = $stdoutTask.GetAwaiter().GetResult() } else { $stdout = '[stdout drain timed out after process exit]' }
    if ($stderrReady) { $stderr = $stderrTask.GetAwaiter().GetResult() } else { $stderr = '[stderr drain timed out after process exit]' }
    $processEnvSummary = ''
    if ($Name -eq 'firmware-build-node1-node2-node3') {
      $processEnvSummary = "PLATFORMIO_CORE_DIR=$($psi.EnvironmentVariables['PLATFORMIO_CORE_DIR']); PLATFORMIO_PLATFORMS_DIR=$($psi.EnvironmentVariables['PLATFORMIO_PLATFORMS_DIR']); PLATFORMIO_PACKAGES_DIR=$($psi.EnvironmentVariables['PLATFORMIO_PACKAGES_DIR'])"
    }
    $processOutput = Protect-DiagnosticText ("$stdout`r`n$stderr")
    $combined = Protect-DiagnosticText ("COMMAND: $safeCommand`r`nPROCESS_STARTED: $processStarted`r`nPROCESS_ENV: $processEnvSummary`r`nEXIT_CODE: $code`r`n`r`n$processOutput")
    $environmentBlockReason = $null
    $permissionPath = $null
    if ($processOutput -match "(?im)Permission denied:\s*'([^']+)'" ) { $permissionPath = $Matches[1] }
    if ($code -ne 0 -and $Name -eq 'firmware-build-node1-node2-node3' -and $permissionPath -match '(?i)(?:platforms|packages)\.lock$') {
      $environmentBlockReason = "PlatformIO could not access its package-manager lock '$permissionPath'; build was not evaluated as a source assertion."
    } elseif ($code -ne 0 -and $processOutput -match '(?i)ModuleNotFoundError: No module named|Cannot find module ["'']|UnknownPackageError|Could not find the package') {
      $environmentBlockReason = 'Required runtime or toolchain package is unavailable in this environment.'
    } elseif ($code -ne 0 -and $Name -like 'wokwi-*' -and $processOutput -match '(?i)401|unauthorized|invalid.*token|token.*expired|authentication failed') {
      $environmentBlockReason = 'Wokwi authentication was rejected; the scenario was not evaluated.'
    } elseif ($code -ne 0 -and $permissionPath -and $permissionPath.StartsWith($runDir,[System.StringComparison]::OrdinalIgnoreCase)) {
      $environmentBlockReason = "Sandbox denied access to run-local path '$permissionPath'."
    }
    if ($environmentBlockReason) {
      $status = 'BLOCKED'
      $code = 77
      $extra = $environmentBlockReason
    } elseif ($code -ne 0 -and $Name -eq 'platformio-toolchain-preflight') {
      $status = 'BLOCKED'
      $code = 77
      $extra = 'The installed Xtensa compiler executable launched but did not complete its version probe; firmware compilation was not evaluated.'
    } elseif ($code -ne 0 -and $Name -eq 'firmware-build-node1-node2-node3' -and $processOutput -match '(?im)^xtensa-esp32s3-elf-g\+\+: error: CreateProcess: No such file or directory\s*$') {
      $status = 'BLOCKED'
      $code = 77
      $extra = 'PlatformIO started, but its Xtensa compiler could not launch an internal child process. This is a toolchain/runtime blocker; no source-level compiler diagnostic was produced.'
    } elseif (-not $stdoutReady -or -not $stderrReady) {
      $status = 'BLOCKED'
      $code = 126
      $extra = 'Runner could not drain the child output within 5 seconds; see process log.'
    }
    Set-Content -Path $logPath -Value $combined -Encoding utf8
    if ($code -ne 0 -and $status -eq 'FAIL') { $extra = "$extra Exit code: $code." }
    Add-Result $Name $status $code $LogName $extra $start (Get-Date) $safeCommand $processStarted
  } catch {
    $runnerError = $_
    $category = if ($processStarted) { 'RUNNER_ERROR_AFTER_LAUNCH' } else { 'RUNNER_ERROR_BEFORE_LAUNCH' }
    Write-RunnerDiagnostic $Name $safeCommand $category $runnerError
    $message = if ($processStarted) { 'Runner could not reliably collect the child result; see runner-errors.log.' } else { 'Runner could not start the child process; see runner-errors.log.' }
    Set-Content -Path $logPath -Value ("BLOCKED: $message`r`nCOMMAND: $safeCommand`r`nEXCEPTION: $(Protect-DiagnosticText $runnerError.Exception.Message)`r`nSCRIPT_STACK_TRACE: $(Protect-DiagnosticText $runnerError.ScriptStackTrace)`r`nPOSITION: $(Protect-DiagnosticText $runnerError.InvocationInfo.PositionMessage)") -Encoding utf8
    $exceptionType = $runnerError.Exception.GetType().FullName
    $exceptionScript = if ($runnerError.InvocationInfo -and $runnerError.InvocationInfo.ScriptName) { $runnerError.InvocationInfo.ScriptName } else { '' }
    $exceptionLine = if ($runnerError.InvocationInfo -and $runnerError.InvocationInfo.ScriptLineNumber) { [Nullable[int]]$runnerError.InvocationInfo.ScriptLineNumber } else { $null }
    Add-Result $Name 'BLOCKED' 126 $LogName $message $start (Get-Date) $safeCommand $processStarted $exceptionType $runnerError.Exception.Message $exceptionScript $exceptionLine $runnerError.ScriptStackTrace
  } finally {
    if ($proc) {
      try { if ($processStarted -and -not $proc.HasExited) { [void](Stop-ChildProcessTree $proc) } } catch {}
      $proc.Dispose()
    }
  }
}

function Add-Blocked([string]$Name, [string]$LogName, [string]$Reason) {
  $start = Get-Date
  Set-Content -Path (Join-Path $runDir $LogName) -Value "BLOCKED: $Reason`r`nPROCESS_STARTED: False`r`nEXIT_CODE: 127" -Encoding utf8
  Add-Result $Name 'BLOCKED' 127 $LogName $Reason $start (Get-Date) '' $false
}

function Add-NotTested([string]$Name, [string]$LogName, [string]$Reason) {
  $start = Get-Date
  Set-Content -Path (Join-Path $runDir $LogName) -Value "NOT TESTED: $Reason" -Encoding utf8
  Add-Result $Name 'NOT TESTED' 0 $LogName $Reason $start (Get-Date)
}

function Remove-IsolatedFirmwareBuildArtifacts {
  $cleanupLog = Join-Path $runDir 'firmware-artifact-cleanup.log'
  $start = Get-Date
  $target = Join-Path (Join-Path $runDir 'isolated-source/firmware') '.pio'
  try {
    $runRoot = [System.IO.Path]::GetFullPath($runDir).TrimEnd([char[]]@('\','/')) + [System.IO.Path]::DirectorySeparatorChar
    $targetFull = [System.IO.Path]::GetFullPath($target)
    if (-not $targetFull.StartsWith($runRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
      throw 'Refusing cleanup because the resolved artifact path is outside this test run directory.'
    }
    if (Test-Path -LiteralPath $targetFull) {
      $item = Get-Item -LiteralPath $targetFull -Force
      if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw 'Refusing cleanup because the isolated PlatformIO artifact path is a reparse point.'
      }
      Remove-Item -LiteralPath $targetFull -Recurse -Force -ErrorAction Stop
      if (Test-Path -LiteralPath $targetFull) { throw 'The isolated PlatformIO artifact directory still exists after cleanup.' }
      $message = 'PASS: removed this run''s isolated PlatformIO build artifacts after Wokwi execution; project source and prior runs were untouched.'
    } else {
      $message = 'PASS: no isolated PlatformIO build artifacts were created; nothing needed removal.'
    }
    Set-Content -LiteralPath $cleanupLog -Value $message -Encoding utf8
    Add-Result 'firmware-build-artifact-cleanup' 'PASS' 0 'firmware-artifact-cleanup.log' $message $start (Get-Date) '' $false
  } catch {
    $safeMessage = Protect-DiagnosticText $_.Exception.Message
    Set-Content -LiteralPath $cleanupLog -Value ("BLOCKED: isolated build artifact cleanup did not complete.`r`nDETAIL: $safeMessage") -Encoding utf8
    Add-Result 'firmware-build-artifact-cleanup' 'BLOCKED' 126 'firmware-artifact-cleanup.log' 'Could not confirm removal of this run''s isolated PlatformIO build artifacts; see firmware-artifact-cleanup.log.' $start (Get-Date) '' $false $_.Exception.GetType().FullName $safeMessage $_.InvocationInfo.ScriptName $_.InvocationInfo.ScriptLineNumber $_.ScriptStackTrace
  }
}

function Copy-ProjectWithoutBuildCache([string]$Source, [string]$Destination) {
  New-Item -ItemType Directory -Path $Destination -Force | Out-Null
  Get-ChildItem -LiteralPath $Source -Force | ForEach-Object {
    if ($_.PSIsContainer -and $_.Name -in @('.pio', '.git', '__pycache__')) { return }
    $target = Join-Path $Destination $_.Name
    if ($_.PSIsContainer) { Copy-ProjectWithoutBuildCache $_.FullName $target }
    else { Copy-Item -LiteralPath $_.FullName -Destination $target }
  }
}

function Add-IsolatedLogicAnalyzer([string]$FirmwareRoot) {
  foreach ($node in @('node1','node2','node3')) {
    $diagramPath = Join-Path $FirmwareRoot ('sim/' + $node + '/diagram.json')
    $diagram = Get-Content -LiteralPath $diagramPath -Raw | ConvertFrom-Json
    if (@($diagram.parts | Where-Object id -eq 'testlogic').Count -gt 0) { continue }
    $diagram.parts += [pscustomobject]@{
      type = 'wokwi-logic-analyzer'; id = 'testlogic'; top = -300; left = 500
      attrs = [pscustomobject]@{ channelNames = 'BUZZER,GREEN,AMBER,RED'; bufferSize = '250000' }
    }
    foreach ($pair in @(@('D0','4'),@('D1','5'),@('D2','6'),@('D3','7'))) {
      $diagram.connections += ,@("testlogic:$($pair[0])","esp:$($pair[1])",'purple',@())
    }
    $diagram.connections += ,@('testlogic:GND','esp:GND.1','black',@())
    $diagramJson = $diagram | ConvertTo-Json -Depth 50
    Write-Utf8NoBom $diagramPath $diagramJson
  }
}

function Test-IsolatedLogicAnalyzer([string]$FirmwareRoot) {
  $details = [System.Collections.Generic.List[string]]::new()
  foreach ($node in @('node1','node2','node3')) {
    $diagramPath = Join-Path $FirmwareRoot ('sim/' + $node + '/diagram.json')
    $diagram = Get-Content -LiteralPath $diagramPath -Raw | ConvertFrom-Json
    if (@($diagram.parts | Where-Object id -eq 'testlogic').Count -ne 1) { throw "$($node): logic analyzer part missing or duplicated" }
    foreach ($pin in @('D0','D1','D2','D3','GND')) {
      if (@($diagram.connections | Where-Object { $_[0] -eq "testlogic:$pin" }).Count -ne 1) { throw "$($node): analyzer pin $pin is not wired exactly once" }
    }
    foreach ($gpio in @('4','5','6','7')) {
      $channel = switch ($gpio) { '4' { 'D0' } '5' { 'D1' } '6' { 'D2' } '7' { 'D3' } }
      if (@($diagram.connections | Where-Object { $_[0] -eq "testlogic:$channel" -and $_[1] -eq "esp:$gpio" }).Count -ne 1) { throw "$($node): GPIO$gpio analyzer wiring mismatch" }
    }
    $details.Add("$node analyzer channels and GPIO4-7 wiring verified")
  }
  return ($details -join "`r`n")
}

function Invoke-QuickTests([switch]$ForFull) {
  if ($script:python) {
    Invoke-LoggedProcess 'backend-regression' $script:python.Source @('-m','unittest','discover','-s','dashboard','-p','test_server.py','-v') $repo 'backend-tests.log' 120 'Python unittest; dashboard/backend assertions.'
    if ($script:node) {
      Invoke-LoggedProcess 'frontend-regression' $script:node.Source @('test_frontend.js') (Join-Path $repo 'dashboard') 'frontend-tests.log' 60 'Node assertions for dashboard rendering and assets.'
    } else { Add-Blocked 'frontend-regression' 'frontend-tests.log' 'Node.js is not available in PATH.' }
    Invoke-LoggedProcess 'trace-evaluation-regression' $script:python.Source @('-m','unittest','discover','-s','tools/evaluation','-p','test_*.py','-v') $repo 'evaluation-tests.log' 120 'Python unittest over labeled trace evaluation.'
    Invoke-LoggedProcess 'wokwi-config-static' $script:python.Source @('tools/validate_wokwi_scenarios.py') $repo 'wokwi-static-validation.log' 30 'Static scenario/diagram assertions only; not a simulator run.'
    Invoke-LoggedProcess 'wokwi-artifact-validation-regression' $script:python.Source @('-m','unittest','tools.test_wokwi_validation','-v') $repo 'wokwi-artifact-validation-tests.log' 60 'Regression tests for static validation with/without binaries and separate post-build artifact validation.'
    $mqttHarness = Join-Path $repo 'tools/e2e/mqtt_ack_harness.py'
    if (Test-Path $mqttHarness) {
      Invoke-LoggedProcess 'mqtt-sqlite-app-ack-dedup-harness' $script:python.Source @($mqttHarness) $repo 'mqtt-integration.log' 60 'Protocol/backend integration harness with isolated loopback broker and temporary SQLite; does not use firmware/NVS.'
    } else { Add-Blocked 'mqtt-sqlite-app-ack-dedup-harness' 'mqtt-integration.log' 'Isolated MQTT/backend harness is not available in this checkout.' }
  } else {
    Add-Blocked 'backend-regression' 'backend-tests.log' 'Python is not available in PATH.'
    Add-Blocked 'frontend-regression' 'frontend-tests.log' 'Python launcher unavailable; frontend test was not scheduled.'
    Add-Blocked 'trace-evaluation-regression' 'evaluation-tests.log' 'Python is not available in PATH.'
    Add-Blocked 'wokwi-config-static' 'wokwi-static-validation.log' 'Python is not available in PATH.'
    Add-Blocked 'wokwi-artifact-validation-regression' 'wokwi-artifact-validation-tests.log' 'Python is not available in PATH.'
    Add-Blocked 'mqtt-sqlite-app-ack-dedup-harness' 'mqtt-integration.log' 'Python is not available in PATH.'
  }

  if ($script:cpp) {
    $fw = Join-Path $repo 'firmware'
    $harnesses = @(
      @{ name='detector-state-machine'; file='detector_recovery_test.cpp'; sources=@('src/Detector.cpp') },
      @{ name='nvs-queue-policy'; file='event_queue_policy_test.cpp'; sources=@() },
      @{ name='nvs-failure-injection'; file='event_queue_nvs_failure_test.cpp'; sources=@() },
      @{ name='ack-remove-source-regression'; file='event_ack_remove_source_test.cpp'; sources=@() },
      @{ name='sensor-drivers-unit'; file='sensor_drivers_test.cpp'; sources=@('src/SensorDrivers.cpp','src/WireI2cBus.cpp') },
      @{ name='sensor-health-unit'; file='sensor_health_test.cpp'; sources=@() },
      @{ name='mq7b-heater-sequencer-unit'; file='mq7b_heater_sequencer_test.cpp'; sources=@() }
    )
    foreach ($h in $harnesses) {
      $source = Join-Path $fw ('tests/' + $h.file)
      if (-not (Test-Path $source)) { Add-Blocked $h.name ($h.name + '.log') 'Harness has not been added to this checkout.'; continue }
      $binary = Join-Path $runDir ($h.name + '.exe')
      if ($script:cppKind -eq 'msvc') {
        $objectDir = Join-Path $runDir ($h.name + '-objects')
        New-Item -ItemType Directory -Path $objectDir -Force | Out-Null
        $compileArgs = @('/nologo','/std:c++14','/W4',('/I' + (Join-Path $fw 'include')),$source)
        foreach ($s in $h.sources) { $compileArgs += (Join-Path $fw $s) }
        $compileArgs += @('/Fo' + ($objectDir.TrimEnd('\') + '\'),('/Fe' + $binary))
      } else {
        $compileArgs = @('-std=c++11','-Wall','-Wextra','-I', (Join-Path $fw 'include'), $source)
        foreach ($s in $h.sources) { $compileArgs += (Join-Path $fw $s) }
        $compileArgs += @('-o',$binary)
      }
      $compileLog = $h.name + '-build.log'
      Invoke-LoggedProcess ($h.name + '-compile') $script:cpp.Source $compileArgs $repo $compileLog 120 ("Host C++ harness compilation using $script:cppKind.")
      $compileResult = $script:tests | Where-Object { $_.name -eq ($h.name + '-compile') } | Select-Object -Last 1
      if (-not $compileResult -or $compileResult.status -ne 'PASS') { continue }
      $harnessCwd = if ($h.name -eq 'ack-remove-source-regression') { $fw } else { $repo }
      Invoke-LoggedProcess $h.name $binary @() $harnessCwd ($h.name + '.log') 30 'Host C++ harness assertion result.'
    }
  } else {
    foreach ($name in @('detector-state-machine','nvs-queue-policy','nvs-failure-injection','ack-remove-source-regression','sensor-drivers-unit','sensor-health-unit','mq7b-heater-sequencer-unit')) {
      Add-Blocked $name ($name + '.log') 'No host C++ compiler was found (checked g++, clang++, and cl.exe). Xtensa firmware compiler is not a substitute. Run from an x64 Native Tools PowerShell prompt with cl.exe, or from a shell where g++/clang++ is already available; the runner does not install tools.'
    }
  }
  if (-not $ForFull) {
    Add-NotTested 'firmware-nvs-offline-replay-runtime' 'nvs-replay.log' 'Quick mode deliberately excludes firmware runtime, reboot persistence, and broker outage scenarios.'
  }
}

function Invoke-FullTests {
  $passwordCheckStart = Get-Date
  if (-not [string]::IsNullOrWhiteSpace($env:GAS_MQTT_PASSWORD)) {
    $passwordPreflightDetail = 'Required build environment variable is present; its value was not logged.'
    Set-Content -LiteralPath (Join-Path $runDir 'mqtt-password-preflight.log') -Value 'PASS: GAS_MQTT_PASSWORD is present and non-empty. Its value was not recorded.' -Encoding utf8
    Add-Result 'mqtt-build-password-preflight' 'PASS' 0 'mqtt-password-preflight.log' $passwordPreflightDetail $passwordCheckStart (Get-Date) '' $false
  } else {
    Add-Blocked 'mqtt-build-password-preflight' 'mqtt-password-preflight.log' 'GAS_MQTT_PASSWORD is missing or empty in this process. Firmware build will be skipped; set it in the same PowerShell session before running the full test runner. The runner never requests or records the password value.'
  }
  $sourceFirmware = Join-Path $repo 'firmware'
  $isolatedFirmware = Join-Path $runDir 'isolated-source/firmware'
  try {
    Copy-ProjectWithoutBuildCache $sourceFirmware $isolatedFirmware
    $overlayStart = Get-Date
    try {
      Add-IsolatedLogicAnalyzer $isolatedFirmware
      $overlayDetails = Test-IsolatedLogicAnalyzer $isolatedFirmware
      Set-Content -Path (Join-Path $runDir 'wokwi-overlay-static.log') -Value "PASS: isolated overlay created and validated.`r`n$overlayDetails" -Encoding utf8
      Add-Result 'wokwi-gpio-overlay-static' 'PASS' 0 'wokwi-overlay-static.log' 'Temporary analyzer overlay is valid; source diagrams remain untouched.' $overlayStart (Get-Date)
    } catch {
      $runnerError = $_
      Write-RunnerDiagnostic 'wokwi-gpio-overlay-static' '<prepare-isolated-logic-analyzer-overlay>' 'RUNNER_ERROR_DURING_PREPARATION' $runnerError
      Set-Content -Path (Join-Path $runDir 'wokwi-overlay-static.log') -Value "BLOCKED: runner failed while preparing temporary GPIO overlay.`r`nEXCEPTION: $(Protect-DiagnosticText $runnerError.Exception.Message)`r`nSCRIPT_STACK_TRACE: $(Protect-DiagnosticText $runnerError.ScriptStackTrace)`r`nPOSITION: $(Protect-DiagnosticText $runnerError.InvocationInfo.PositionMessage)" -Encoding utf8
      Add-Result 'wokwi-gpio-overlay-static' 'BLOCKED' 126 'wokwi-overlay-static.log' 'Runner failed while preparing temporary GPIO overlay; see runner-errors.log.' $overlayStart (Get-Date)
    }
    $ini = Join-Path $isolatedFirmware 'platformio.ini'
    $text = Get-Content $ini -Raw
    $testHome = 'autotest-' + $runId.ToLowerInvariant()
    $text = $text.Replace('demo-home', $testHome).Replace('MQTT_HOST="host.wokwi.internal"','MQTT_HOST="autotest.invalid"').Replace('MQTT_PORT=1883','MQTT_PORT=1884').Replace('MQTT_USER="iotlab"','MQTT_USER="autotest"')
    Write-Utf8NoBom $ini $text
    $isolatedPioDir = Join-Path $isolatedFirmware '.pio'
    $isolatedArtifactTreeIsFresh = -not (Test-Path -LiteralPath $isolatedPioDir)
    if ($isolatedArtifactTreeIsFresh) {
      $freshnessDetail = 'PASS: isolated firmware copy contains no .pio directory before this run''s build; prior binaries cannot be reused.'
      Set-Content -LiteralPath (Join-Path $runDir 'firmware-artifact-isolation-preflight.log') -Value $freshnessDetail -Encoding utf8
      Add-Result 'firmware-artifact-isolation-preflight' 'PASS' 0 'firmware-artifact-isolation-preflight.log' $freshnessDetail (Get-Date) (Get-Date) '' $false
    } else {
      Add-Blocked 'firmware-artifact-isolation-preflight' 'firmware-artifact-isolation-preflight.log' 'The isolated firmware copy already contains .pio before build; refusing to risk using stale firmware artifacts.'
    }
    if ($script:python -and $script:pioModule) {
      $packageRoot = if (-not [string]::IsNullOrWhiteSpace($env:USERPROFILE)) { Join-Path $env:USERPROFILE '.platformio\packages' } else { '' }
      $compilerPath = if ($packageRoot) { Join-Path $packageRoot 'toolchain-xtensa-esp32s3\bin\xtensa-esp32s3-elf-g++.exe' } else { '' }
      $packageRequirements = [ordered]@{
        'toolchain-xtensa-esp32s3' = '8.4.0+2021r2-patch5'
        'toolchain-riscv32-esp' = '8.4.0+2021r2-patch5'
        'framework-arduinoespressif32' = '4.20017.260907+sha.dcc1105b'
        'tool-esptoolpy' = '2.41100.260830'
      }
      $missingPackages = [System.Collections.Generic.List[string]]::new()
      foreach ($packageName in $packageRequirements.Keys) {
        $manifestPath = Join-Path (Join-Path $packageRoot $packageName) 'package.json'
        if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
          $missingPackages.Add("$packageName (manifest missing)")
          continue
        }
        try {
          $packageManifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
          if ([string]$packageManifest.version -ne [string]$packageRequirements[$packageName]) {
            $missingPackages.Add("$packageName (version $($packageManifest.version), expected $($packageRequirements[$packageName]))")
          }
        } catch { $missingPackages.Add("$packageName (manifest unreadable)") }
      }
      if ([string]::IsNullOrWhiteSpace($env:GAS_MQTT_PASSWORD)) {
        Add-Blocked 'firmware-build-node1-node2-node3' 'firmware-build.log' 'Build skipped before PlatformIO launch because GAS_MQTT_PASSWORD is missing or empty in this process. Set the variable in the same PowerShell session and rerun; no credential was logged.'
      } elseif (-not $isolatedArtifactTreeIsFresh) {
        Add-Blocked 'firmware-build-node1-node2-node3' 'firmware-build.log' 'Build skipped because the isolated .pio output tree was not empty before build; refusing possible stale-artifact reuse.'
      } elseif ($missingPackages.Count -gt 0) {
        Add-Blocked 'platformio-toolchain-preflight' 'platformio-toolchain-preflight.log' ('Refusing implicit PlatformIO package install/upgrade; required installed package set is incomplete or mismatched: ' + ($missingPackages -join ', '))
        Add-Blocked 'firmware-build-node1-node2-node3' 'firmware-build.log' 'Build skipped because the existing package cache did not match the versions already used by the last recorded build.'
      } elseif (-not $compilerPath -or -not (Test-Path -LiteralPath $compilerPath -PathType Leaf)) {
        Add-Blocked 'platformio-toolchain-preflight' 'platformio-toolchain-preflight.log' "Xtensa compiler executable is missing at the PlatformIO package path: $compilerPath"
        Add-Blocked 'firmware-build-node1-node2-node3' 'firmware-build.log' 'Build skipped because the Xtensa compiler executable was missing during preflight.'
      } else {
        $compilerItem = Get-Item -LiteralPath $compilerPath
        $linkDescription = if ($compilerItem.LinkType) { "$($compilerItem.LinkType): $($compilerItem.Target -join ',')" } else { 'regular file' }
        $preflightMetadata = "RESOLVED_COMPILER: $compilerPath`r`nFILE_EXISTS: True`r`nFILE_LENGTH: $($compilerItem.Length)`r`nFILE_LINK: $linkDescription`r`nEXPECTED_PACKAGE_VERSION: $($packageRequirements['toolchain-xtensa-esp32s3'])"
        Invoke-LoggedProcess 'platformio-toolchain-preflight' $compilerPath @('--version') $repo 'platformio-toolchain-preflight.log' 20 'Direct launch of the exact Xtensa compiler executable required by PlatformIO.'
        Add-Content -LiteralPath (Join-Path $runDir 'platformio-toolchain-preflight.log') -Value ("`r`n" + $preflightMetadata) -Encoding utf8
        $preflightResult = $script:tests | Where-Object { $_.name -eq 'platformio-toolchain-preflight' } | Select-Object -Last 1
        if ($preflightResult.status -eq 'PASS') {
          Invoke-LoggedProcess 'firmware-build-node1-node2-node3' $script:python.Source @('-m','platformio','run','-d',$isolatedFirmware,'-e','node1','-e','node2','-e','node3') $repo 'firmware-build.log' 600 'Fresh source build in isolated run folder; exact preinstalled PlatformIO packages are reused and no package download/upgrade is allowed. GAS_MQTT_PASSWORD is inherited through the child environment only and is not included in arguments or logs.'
        } else {
          Add-Blocked 'firmware-build-node1-node2-node3' 'firmware-build.log' 'Build skipped because the exact Xtensa compiler executable failed its direct launch preflight; see platformio-toolchain-preflight.log and runner-errors.log.'
        }
      }
    } elseif ($script:python) {
      Add-Blocked 'firmware-build-node1-node2-node3' 'firmware-build.log' 'PlatformIO module is unavailable in this process environment.'
    } else {
      Add-Blocked 'firmware-build-node1-node2-node3' 'firmware-build.log' 'Python is not available in PATH.'
    }

    $buildResult = $script:tests | Where-Object { $_.name -eq 'firmware-build-node1-node2-node3' } | Select-Object -Last 1
    $artifactResult = $null
    if ($buildResult -and $buildResult.status -eq 'PASS') {
      Invoke-LoggedProcess 'wokwi-artifact-validation' $script:python.Source @('tools/validate_wokwi_artifacts.py','--firmware-root',$isolatedFirmware) $repo 'wokwi-artifact-validation.log' 30 'Requires non-empty node1/node2/node3 firmware BIN and ELF outputs under this run''s isolated .pio tree and verifies Wokwi TOML resolves to them.'
      $artifactResult = $script:tests | Where-Object { $_.name -eq 'wokwi-artifact-validation' } | Select-Object -Last 1
    } else {
      Add-Blocked 'wokwi-artifact-validation' 'wokwi-artifact-validation.log' 'Post-build artifact validation was not run because the fresh three-node build did not PASS.'
    }
    if ($buildResult -and $buildResult.status -eq 'PASS' -and $artifactResult -and $artifactResult.status -eq 'PASS' -and $script:wokwiPath -and $script:wokwiTokenPresent) {
      foreach ($node in @('node1','node2','node3')) {
        $simDir = Join-Path $isolatedFirmware ('sim/' + $node)
        $scenario = 'scenarios/' + $node + '.yaml'
        $vcd = Join-Path $runDir ($node + '-gpio.vcd')
        if (Test-Path (Join-Path $simDir $scenario)) {
          Invoke-LoggedProcess ('wokwi-' + $node) $script:wokwiPath @($simDir,'--scenario',$scenario,'--timeout','60000','--timeout-exit-code','124','--serial-log-file',(Join-Path $runDir ($node + '-serial.log')),'--vcd-file',$vcd) $repo ('wokwi-' + $node + '.log') 180 'Wokwi simulation in an isolated source copy. GPIO VCD is collected when supported.'
          $simResult = $script:tests | Where-Object { $_.name -eq ('wokwi-' + $node) } | Select-Object -Last 1
          if ($simResult -and $simResult.status -eq 'PASS') {
            Invoke-LoggedProcess ('wokwi-serial-observations-' + $node) $script:python.Source @('tools/verify_wokwi_serial.py',$node,(Join-Path $runDir ($node + '-serial.log'))) $repo ('wokwi-' + $node + '-serial-check.log') 30 'Ordered state transitions and emitted sample timing rows; Wokwi evidence only.'
            Invoke-LoggedProcess ('wokwi-gpio-vcd-' + $node) $script:python.Source @('tools/verify_wokwi_vcd.py',$vcd) $repo ('wokwi-' + $node + '-gpio-vcd.log') 30 'Independent VCD assertion for LED state transitions and buzzer PWM activity.'
          } else { Add-Blocked ('wokwi-gpio-vcd-' + $node) ('wokwi-' + $node + '-gpio-vcd.log') 'Scenario did not pass, so GPIO VCD was not accepted.' }
        } else { Add-Blocked ('wokwi-' + $node) ('wokwi-' + $node + '.log') "Scenario missing in isolated project: $scenario" }
      }
    } elseif ($buildResult -and $buildResult.status -ne 'PASS') {
      foreach ($node in @('node1','node2','node3')) { Add-Blocked ('wokwi-' + $node) ('wokwi-' + $node + '.log') ("Wokwi scenario skipped because fresh firmware build status is $($buildResult.status); see firmware-build.log.") }
    } elseif (-not $artifactResult -or $artifactResult.status -ne 'PASS') {
      foreach ($node in @('node1','node2','node3')) { Add-Blocked ('wokwi-' + $node) ('wokwi-' + $node + '.log') 'Wokwi scenario skipped because post-build firmware artifact validation did not PASS; see wokwi-artifact-validation.log.' }
    } elseif (-not $script:wokwiPath) {
      foreach ($node in @('node1','node2','node3')) { Add-Blocked ('wokwi-' + $node) ('wokwi-' + $node + '.log') 'wokwi-cli is not available in PATH.' }
    } elseif (-not $script:wokwiTokenPresent) {
      foreach ($node in @('node1','node2','node3')) { Add-Blocked ('wokwi-' + $node) ('wokwi-' + $node + '.log') 'WOKWI_CLI_TOKEN is not present in the current Codex process environment.' }
    } else {
      foreach ($node in @('node1','node2','node3')) { Add-Blocked ('wokwi-' + $node) ('wokwi-' + $node + '.log') 'Firmware build result is unavailable; scenario was not run.' }
    }
  } catch {
    $runnerError = $_
    Write-RunnerDiagnostic 'firmware-build-node1-node2-node3' '<prepare-isolated-firmware-copy>' 'RUNNER_ERROR_DURING_PREPARATION' $runnerError
    Add-Blocked 'firmware-build-node1-node2-node3' 'firmware-build.log' ("Runner could not prepare isolated build source: $(Protect-DiagnosticText $runnerError.Exception.Message). See runner-errors.log.")
    foreach ($node in @('node1','node2','node3')) { Add-Blocked ('wokwi-' + $node) ('wokwi-' + $node + '.log') 'Isolated build source could not be prepared.' }
  }

  $runtimeBuild = $script:tests | Where-Object { $_.name -eq 'firmware-build-node1-node2-node3' } | Select-Object -Last 1
  $runtimeReasons = [System.Collections.Generic.List[string]]::new()
  if (-not $runtimeBuild -or $runtimeBuild.status -ne 'PASS') { $runtimeReasons.Add('fresh firmware build did not PASS') }
  if (-not $script:wokwiPath) { $runtimeReasons.Add('Wokwi CLI is unavailable') }
  if (-not $script:wokwiTokenPresent) { $runtimeReasons.Add('WOKWI_CLI_TOKEN is absent from this process environment') }
  $runtimeReasons.Add('no firmware-backed NVS offline/reconnect/replay/application-ACK driver is implemented; the MQTT/SQLite harness uses synthetic clients')
  $runtimeReasonText = if ($runtimeReasons.Count) { ' Blocking conditions: ' + ($runtimeReasons -join '; ') + '.' } else { '' }
  Add-Blocked 'firmware-nvs-offline-replay-runtime' 'nvs-replay.log' ("Firmware/NVS replay was not executed.$runtimeReasonText")

}

$startedUtc = (Get-Date).ToUniversalTime()
if ($Mode -eq 'quick' -or $Mode -eq 'full') { Invoke-QuickTests -ForFull:($Mode -eq 'full') }
if ($Mode -eq 'full') {
  Invoke-FullTests
  Remove-IsolatedFirmwareBuildArtifacts
} else {
  Write-EmptyLog 'mqtt-password-preflight.log' 'NOT TESTED: quick mode does not build firmware.'
  Write-EmptyLog 'firmware-artifact-cleanup.log' 'NOT TESTED: quick mode does not create PlatformIO build artifacts.'
}

Write-EmptyLog 'firmware-build.log' 'NOT TESTED: quick mode does not build firmware.'
Write-EmptyLog 'platformio-toolchain-preflight.log' 'NOT TESTED: quick mode does not launch the Xtensa compiler.'
Write-EmptyLog 'firmware-artifact-isolation-preflight.log' 'NOT TESTED: quick mode does not prepare an isolated firmware build tree.'
Write-EmptyLog 'wokwi-artifact-validation.log' 'NOT TESTED: quick mode does not validate post-build firmware artifacts.'
Write-EmptyLog 'wokwi-artifact-validation-tests.log' 'NOT TESTED: quick mode does not build firmware.'
Write-EmptyLog 'wokwi-node1.log' 'NOT TESTED: quick mode does not run Wokwi.'
Write-EmptyLog 'wokwi-node2.log' 'NOT TESTED: quick mode does not run Wokwi.'
Write-EmptyLog 'wokwi-node3.log' 'NOT TESTED: quick mode does not run Wokwi.'
Write-EmptyLog 'wokwi-overlay-static.log' 'NOT TESTED: full mode does not prepare the isolated analyzer overlay.'
Write-EmptyLog 'wokwi-node1-serial.log' 'NOT CAPTURED: Wokwi CLI did not produce a serial log in this run.'
Write-EmptyLog 'wokwi-node2-serial.log' 'NOT CAPTURED: Wokwi CLI did not produce a serial log in this run.'
Write-EmptyLog 'wokwi-node3-serial.log' 'NOT CAPTURED: Wokwi CLI did not produce a serial log in this run.'
Write-EmptyLog 'wokwi-node1-serial-check.log' 'NOT TESTED: Wokwi serial runtime was blocked or not requested.'
Write-EmptyLog 'wokwi-node2-serial-check.log' 'NOT TESTED: Wokwi serial runtime was blocked or not requested.'
Write-EmptyLog 'wokwi-node3-serial-check.log' 'NOT TESTED: Wokwi serial runtime was blocked or not requested.'
Write-EmptyLog 'mqtt-integration.log' 'NOT TESTED: quick mode does not run MQTT integration.'
Write-EmptyLog 'nvs-replay.log' 'NOT TESTED: no firmware NVS runtime replay was executed.'

$endedUtc = (Get-Date).ToUniversalTime()
$script:integrationPort = $null
$script:port1884Used = $false
$integrationLogPath = Join-Path $runDir 'mqtt-integration.log'
$integrationText = ''
if (Test-Path $integrationLogPath) {
  $integrationText = Get-Content -LiteralPath $integrationLogPath -Raw
}
foreach ($brokerLog in Get-ChildItem -LiteralPath $runDir -Directory -Filter 'mqtt-ack-*' -ErrorAction SilentlyContinue | ForEach-Object { Join-Path $_.FullName 'broker.log' }) {
  if (Test-Path $brokerLog) { $integrationText += "`r`n" + (Get-Content -LiteralPath $brokerLog -Raw) }
}
if ($integrationText -match '(?:harness completed on loopback port=|test broker listening at 127\.0\.0\.1:)(\d+)') {
  $script:integrationPort = [int]$Matches[1]
  $script:port1884Used = ($script:integrationPort -eq 1884)
}
$script:wokwiMqttTargetPort = $null
if (@($script:tests | Where-Object { $_.name -like 'wokwi-node*' -and $_.status -ne 'BLOCKED' }).Count -gt 0) {
  $script:wokwiMqttTargetPort = 1884
}
$summary = [ordered]@{
  run_id = $runId; mode = $Mode; started_utc = $startedUtc.ToString('o'); ended_utc = $endedUtc.ToString('o')
  environment = [ordered]@{
    powershell = $script:powerShellVersion; python = $script:pythonVersion; platformio_module = $script:pioVersion
    platformio_core_dir = $script:pioCoreDir; platformio_packages_dir = $script:pioPackagesDir
    platformio_packages_policy = 'Reuses existing global package payloads directly after exact-version preflight; no writable junction, package copy, install, or upgrade.'
    wokwi_cli = $script:wokwiVersion; wokwi_cli_path = $script:wokwiPath; wokwi_token_present = $script:wokwiTokenPresent
    mqtt_build_password_present = $script:mqttPasswordPresent
    token_active_status = 'not checked without a Wokwi CLI run'
    mosquitto_cli = $script:mosquittoVersion; host_cpp_compiler = [bool]$script:cpp
    host_cpp_compiler_kind = $script:cppKind; host_cpp_compiler_path = if ($script:cpp) { $script:cpp.Source } else { $null }
    mosquitto_pub = [bool]$script:mosquittoPub; mosquitto_sub = [bool]$script:mosquittoSub
    port_1883_touched = $null
    port_1883_touch_observation = 'Not instrumented; the MQTT test harness uses loopback port 1884 or an ephemeral port and has no port 1883 connection path.'
    isolated_mqtt_test_port = $script:integrationPort
    port_1884_used_by_runner = $script:port1884Used
    wokwi_mqtt_target_host = 'autotest.invalid'
    wokwi_mqtt_target_port = $script:wokwiMqttTargetPort
    port_1883_listener_observed = $script:port1883Listener; port_1884_listener_observed = $script:port1884Listener
  }
  tests = @($script:tests); runner_errors = @($script:runnerErrors)
  counts = [ordered]@{
    PASS = @($script:tests | Where-Object status -eq 'PASS').Count
    FAIL = @($script:tests | Where-Object status -eq 'FAIL').Count
    BLOCKED = @($script:tests | Where-Object status -eq 'BLOCKED').Count
    NOT_TESTED = @($script:tests | Where-Object status -eq 'NOT TESTED').Count
  }
}
$jsonPath = Join-Path $runDir 'results.json'
$summary | ConvertTo-Json -Depth 8 | Set-Content -Path $jsonPath -Encoding utf8
$script:timeline | Export-Csv -Path (Join-Path $runDir 'timeline.csv') -NoTypeInformation -Encoding utf8
if ($script:runnerErrors.Count -gt 0) {
  $script:runnerErrors | ConvertTo-Json -Depth 8 | Set-Content -Path (Join-Path $runDir 'runner-errors.json') -Encoding utf8
  $diagnosticLines = [System.Collections.Generic.List[string]]::new()
  foreach ($entry in $script:runnerErrors) {
    $diagnosticLines.Add("[$($entry.category)] $($entry.test)")
    $diagnosticLines.Add("COMMAND: $($entry.command)")
    $diagnosticLines.Add("EXCEPTION: $($entry.exception_type): $($entry.exception_message)")
    $diagnosticLines.Add("SCRIPT_FILE: $($entry.script_file):$($entry.script_line)")
    $diagnosticLines.Add("SCRIPT_STACK_TRACE: $($entry.script_stack_trace)")
    $diagnosticLines.Add("POSITION: $($entry.invocation_position)")
    $diagnosticLines.Add("DETAIL: $($entry.exception_detail)")
    $diagnosticLines.Add('')
  }
  $diagnosticLines | Set-Content -Path (Join-Path $runDir 'runner-errors.log') -Encoding utf8
} else {
  Write-EmptyLog 'runner-errors.log' 'No PowerShell runner exceptions were captured.'
  Write-EmptyLog 'runner-errors.json' '[]'
}

$lines = [System.Collections.Generic.List[string]]::new()
$lines.Add("# Automated test run $runId")
$lines.Add('')
$lines.Add("Mode: **$Mode**. Started: $($startedUtc.ToString('o')); ended: $($endedUtc.ToString('o')).")
$lines.Add('')
$lines.Add('| Test | Status | Exit | Seconds | Evidence |')
$lines.Add('|---|---:|---:|---:|---|')
foreach ($test in $script:tests) { $lines.Add("| $($test.name) | $($test.status) | $($test.exit_code) | $($test.duration_seconds) | $($test.log) |") }
$lines.Add('')
$lines.Add("Totals: PASS $($summary.counts.PASS), FAIL $($summary.counts.FAIL), BLOCKED $($summary.counts.BLOCKED), NOT TESTED $($summary.counts.NOT_TESTED).")
$lines.Add('')
$lines.Add("PowerShell runner exceptions captured: $($script:runnerErrors.Count). See runner-errors.log and runner-errors.json.")
$lines.Add('')
$lines.Add('A PASS is limited to the test named and its evidence. Static checks and unit tests are not Wokwi or physical hardware runtime evidence.')
$lines.Add('')
$lines.Add('Scope: MQTT integration uses a synthetic MQTT client and temporary SQLite against an isolated loopback broker; it does not execute firmware or NVS. Physical hardware was not tested.')
$lines | Set-Content -Path (Join-Path $runDir 'summary.md') -Encoding utf8

$failed = [System.Collections.Generic.List[string]]::new()
$failed.Add('# Failed and blocked tests')
$failed.Add('')
foreach ($test in $script:tests | Where-Object { $_.status -in @('FAIL','BLOCKED') }) { $failed.Add('- **' + $test.status + ': ' + $test.name + '** - ' + $test.detail + ' Evidence: `' + $test.log + '`') }
if ($failed.Count -eq 2) { $failed.Add('No failed or blocked test entries.') }
$failed | Set-Content -Path (Join-Path $runDir 'failed-tests.md') -Encoding utf8

Write-Output "Test results: $runDir"
Write-Output "PASS=$($summary.counts.PASS) FAIL=$($summary.counts.FAIL) BLOCKED=$($summary.counts.BLOCKED) NOT_TESTED=$($summary.counts.NOT_TESTED)"
if ($summary.counts.FAIL -gt 0) { exit 1 }
if ($summary.counts.BLOCKED -gt 0) { exit 2 }
