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
. (Join-Path $PSScriptRoot 'msvc_compile_arguments.ps1')
. (Join-Path $PSScriptRoot 'wokwi_failure_classification.ps1')
. (Join-Path $PSScriptRoot 'nvs_runtime_wokwi_config.ps1')

$script:tests = [System.Collections.Generic.List[object]]::new()
$script:timeline = [System.Collections.Generic.List[object]]::new()
$script:runnerErrors = [System.Collections.Generic.List[object]]::new()
$script:wokwiQuotaExhausted = $false
$script:python = Get-Command python -ErrorAction SilentlyContinue
$script:node = Get-Command node -ErrorAction SilentlyContinue
$script:powerShellVersion = $PSVersionTable.PSVersion.ToString()
$script:powerShellExecutable = Join-Path $PSHOME $(if ($PSVersionTable.PSEdition -eq 'Core') { 'pwsh.exe' } else { 'powershell.exe' })
$script:pioCoreDir = Join-Path $runDir 'platformio-core'
$script:pioPackagesDir = $null
$script:pioModule = $false
$script:mqttCaCertPath = $null
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
$script:vsCodePath = $null
$script:wokwiVscodeExtensionPath = $null
$wokwiExtensionRoot = Join-Path $env:USERPROFILE '.vscode\extensions'
if (Test-Path -LiteralPath $wokwiExtensionRoot -PathType Container) {
  $script:wokwiVscodeExtensionPath = Get-ChildItem -LiteralPath $wokwiExtensionRoot -Directory -Filter 'wokwi.wokwi-vscode-*' -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1 -ExpandProperty FullName
}
$vsCodeCommand = Get-Command code -ErrorAction SilentlyContinue | Select-Object -First 1
if ($vsCodeCommand -and $vsCodeCommand.CommandType -in @('Application','ExternalScript')) {
  $script:vsCodePath = $vsCodeCommand.Source
  if (-not $script:vsCodePath) { $script:vsCodePath = $vsCodeCommand.Path }
}
if (-not $script:vsCodePath) {
  $vsCodeCommand = Get-Command code.cmd -ErrorAction SilentlyContinue | Select-Object -First 1
  if ($vsCodeCommand) { $script:vsCodePath = $vsCodeCommand.Source }
}
if ($script:wokwiPath) {
  $wokwiVersionProbe = Invoke-ScrubbedVersionProbe $script:wokwiPath @('--version')
  $script:wokwiVersion = $wokwiVersionProbe.output
}
$script:mosquitto = Get-Command mosquitto -ErrorAction SilentlyContinue
if (-not $script:mosquitto) { $script:mosquitto = Get-Command mosquitto.exe -ErrorAction SilentlyContinue | Where-Object { $_.CommandType -eq 'Application' } | Select-Object -First 1 }
if (-not $script:mosquitto -and -not [string]::IsNullOrWhiteSpace($env:ProgramFiles)) {
  $candidate = Join-Path $env:ProgramFiles 'mosquitto\mosquitto.exe'
  if (Test-Path -LiteralPath $candidate -PathType Leaf) { $script:mosquitto = [pscustomobject]@{ Source = $candidate } }
}
if (-not $script:mosquitto -and -not [string]::IsNullOrWhiteSpace(${env:ProgramFiles(x86)})) {
  $candidate = Join-Path ${env:ProgramFiles(x86)} 'mosquitto\mosquitto.exe'
  if (Test-Path -LiteralPath $candidate -PathType Leaf) { $script:mosquitto = [pscustomobject]@{ Source = $candidate } }
}
if (-not $script:mosquitto -and -not [string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
  $candidate = Join-Path $env:LOCALAPPDATA 'Programs\mosquitto\mosquitto.exe'
  if (Test-Path -LiteralPath $candidate -PathType Leaf) { $script:mosquitto = [pscustomobject]@{ Source = $candidate } }
}
if (-not $script:mosquitto) {
  $candidatePaths = @()
  if (-not [string]::IsNullOrWhiteSpace($env:MOSQUITTO_HOME)) { $candidatePaths += Join-Path $env:MOSQUITTO_HOME 'mosquitto.exe' }
  $candidatePaths += 'G:\Mosquitto\mosquitto.exe'
  foreach ($candidate in $candidatePaths) {
    if (Test-Path -LiteralPath $candidate -PathType Leaf) {
      $script:mosquitto = [pscustomobject]@{ Source = $candidate }
      break
    }
  }
}
if ($script:mosquitto) {
  $mosquittoVersionProbe = Invoke-ScrubbedVersionProbe $script:mosquitto.Source @('-h')
  $script:mosquittoVersion = ($mosquittoVersionProbe.output -split "`r?`n" | Select-Object -First 1).Trim()
}
$script:mosquittoPasswd = Get-Command mosquitto_passwd -ErrorAction SilentlyContinue
if (-not $script:mosquittoPasswd) { $script:mosquittoPasswd = Get-Command mosquitto_passwd.exe -ErrorAction SilentlyContinue | Where-Object { $_.CommandType -eq 'Application' } | Select-Object -First 1 }
if (-not $script:mosquittoPasswd -and $script:mosquitto -and $script:mosquitto.Source) {
  $candidate = Join-Path (Split-Path -Parent $script:mosquitto.Source) 'mosquitto_passwd.exe'
  if (Test-Path -LiteralPath $candidate -PathType Leaf) { $script:mosquittoPasswd = [pscustomobject]@{ Source = $candidate } }
}
$script:mosquittoPub = Get-Command mosquitto_pub -ErrorAction SilentlyContinue
$script:mosquittoSub = Get-Command mosquitto_sub -ErrorAction SilentlyContinue
if ($script:mosquitto -and $script:mosquitto.Source) {
  $mosquittoDirectory = Split-Path -Parent $script:mosquitto.Source
  if (-not $script:mosquittoPub) {
    $candidate = Join-Path $mosquittoDirectory 'mosquitto_pub.exe'
    if (Test-Path -LiteralPath $candidate -PathType Leaf) { $script:mosquittoPub = [pscustomobject]@{ Source = $candidate } }
  }
  if (-not $script:mosquittoSub) {
    $candidate = Join-Path $mosquittoDirectory 'mosquitto_sub.exe'
    if (Test-Path -LiteralPath $candidate -PathType Leaf) { $script:mosquittoSub = [pscustomobject]@{ Source = $candidate } }
  }
}
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

function Invoke-LoggedProcess([string]$Name, [string]$Executable, [string[]]$Arguments, [string]$WorkingDirectory, [string]$LogName, [int]$TimeoutSeconds, [string]$Detail = '', [hashtable]$EnvironmentOverrides = @{}) {
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
    $psi.EnvironmentVariables['GAS_MQTT_CA_CERT_FILE'] = ''
    $psi.EnvironmentVariables['MQTT_PASSWORD'] = ''
    $psi.EnvironmentVariables['WIFI_PASSWORD'] = ''
    $psi.EnvironmentVariables['GAS_SYNC_TOKEN'] = ''
    $psi.EnvironmentVariables['GAS_SYNC_API_TOKEN'] = ''
    $psi.EnvironmentVariables['MQTT_USER'] = ''
    $psi.EnvironmentVariables['GAS_E2E_TMPDIR'] = ''
    if ($Name -in @('firmware-build-node1-node2-node3','firmware-build-nvs-runtime','firmware-build-mqtt-tls-compile','firmware-build-mqtt-tls-runtime')) {
      $buildPassword = [string]$env:GAS_MQTT_PASSWORD
      if ($Name -eq 'firmware-build-node1-node2-node3') {
        if ([string]::IsNullOrWhiteSpace($buildPassword)) { throw 'GAS_MQTT_PASSWORD became empty before production node build; refusing to start the build.' }
        $psi.EnvironmentVariables['GAS_MQTT_PASSWORD'] = $buildPassword
      } else {
        $psi.EnvironmentVariables['GAS_MQTT_PASSWORD'] = ''
      }
      if ($Name -eq 'firmware-build-mqtt-tls-runtime') {
        if ([string]::IsNullOrWhiteSpace($script:tlsRuntimePassword)) { throw 'TLS runtime build requires a random per-run password.' }
        $psi.EnvironmentVariables['GAS_MQTT_PASSWORD'] = $script:tlsRuntimePassword
      }
      if ($Name -in @('firmware-build-mqtt-tls-compile','firmware-build-mqtt-tls-runtime')) {
        if ([string]::IsNullOrWhiteSpace($script:mqttCaCertPath) -or -not (Test-Path -LiteralPath $script:mqttCaCertPath -PathType Leaf)) {
          throw 'MQTT TLS target requires the ephemeral CA PEM generated for this run.'
        }
        $psi.EnvironmentVariables['GAS_MQTT_CA_CERT_FILE'] = $script:mqttCaCertPath
      }
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
      } else {
        throw 'The existing PlatformIO espressif32 platform package is unavailable; refusing an implicit download during this test run.'
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
  if ($Name -in @('mqtt-sqlite-app-ack-dedup-harness','mqtt-security-policy-harness')) {
      $psi.EnvironmentVariables['GAS_E2E_TMPDIR'] = $runDir
    }
    if ($Name -like 'wokwi-node*' -and $script:wokwiTokenPresent) {
      $psi.EnvironmentVariables['WOKWI_CLI_TOKEN'] = [string]$env:WOKWI_CLI_TOKEN
    }
    foreach ($environmentName in $EnvironmentOverrides.Keys) {
      $psi.EnvironmentVariables[[string]$environmentName] = [string]$EnvironmentOverrides[$environmentName]
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
    if ($Name -in @('firmware-build-node1-node2-node3','firmware-build-nvs-runtime')) {
      $processEnvSummary = "PLATFORMIO_CORE_DIR=$($psi.EnvironmentVariables['PLATFORMIO_CORE_DIR']); PLATFORMIO_PLATFORMS_DIR=$($psi.EnvironmentVariables['PLATFORMIO_PLATFORMS_DIR']); PLATFORMIO_PACKAGES_DIR=$($psi.EnvironmentVariables['PLATFORMIO_PACKAGES_DIR'])"
    }
    $processOutput = Protect-DiagnosticText ("$stdout`r`n$stderr")
    $combined = Protect-DiagnosticText ("COMMAND: $safeCommand`r`nPROCESS_STARTED: $processStarted`r`nPROCESS_ENV: $processEnvSummary`r`nEXIT_CODE: $code`r`n`r`n$processOutput")
    $environmentBlockReason = $null
    $permissionPath = $null
    if ($processOutput -match "(?im)Permission denied:\s*'([^']+)'" ) { $permissionPath = $Matches[1] }
    $wokwiTransportBlockReason = Get-WokwiTransportBlockReason -TestName $Name -ExitCode $code -ProcessOutput $processOutput
    if ($wokwiTransportBlockReason) {
      $environmentBlockReason = $wokwiTransportBlockReason
      if ($Name -like 'wokwi-*' -and $wokwiTransportBlockReason -match '(?i)monthly CI minute quota') {
        $script:wokwiQuotaExhausted = $true
      }
    } elseif ($code -ne 0 -and $Name -in @('firmware-build-node1-node2-node3','firmware-build-nvs-runtime') -and $permissionPath -match '(?i)(?:platforms|packages)\.lock$') {
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
    } elseif ($code -eq 77 -and $Name -in @('firmware-nvs-offline-replay-runtime','firmware-mqtt-tls-runtime','mqtt-security-mosquitto-runtime')) {
      $status = 'BLOCKED'
      $extra = 'Runtime test driver reported a missing local runtime prerequisite; see its evidence log.'
    } elseif ($code -ne 0 -and $Name -in @('firmware-build-node1-node2-node3','firmware-build-nvs-runtime') -and $processOutput -match '(?im)^xtensa-esp32s3-elf-g\+\+: error: CreateProcess: No such file or directory\s*$') {
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

function Invoke-WokwiNodeScenarioWithRetry([string]$Name, [string]$Executable, [string[]]$Arguments,
                                           [string]$WorkingDirectory, [string]$LogName,
                                           [int]$TimeoutSeconds, [string]$Detail) {
  if ($script:wokwiQuotaExhausted) {
    Add-Blocked $Name $LogName 'Wokwi monthly CI quota was exhausted by an earlier scenario; this cloud call was skipped.'
    return
  }
  Invoke-LoggedProcess $Name $Executable $Arguments $WorkingDirectory $LogName $TimeoutSeconds $Detail
  $firstResult = $script:tests | Where-Object { $_.name -eq $Name } | Select-Object -Last 1
  $firstLogPath = Join-Path $runDir $LogName
  if (-not $firstResult -or -not (Test-Path -LiteralPath $firstLogPath -PathType Leaf)) { return }
  $firstOutput = Get-Content -LiteralPath $firstLogPath -Raw
  if (-not (Test-WokwiTransportRetryEligible -TestName $Name -Attempt 1 -Status $firstResult.status `
      -ExitCode $firstResult.exit_code -ProcessOutput $firstOutput)) { return }

  $attemptLogName = [System.IO.Path]::GetFileNameWithoutExtension($LogName) + '-attempt1.log'
  Copy-Item -LiteralPath $firstLogPath -Destination (Join-Path $runDir $attemptLogName) -Force
  foreach ($artifactFlag in @('--serial-log-file','--vcd-file')) {
    $argumentIndex = -1
    for ($index = 0; $index -lt $Arguments.Count; $index++) {
      if ($Arguments[$index] -ceq $artifactFlag) { $argumentIndex = $index; break }
    }
    if ($argumentIndex -ge 0 -and ($argumentIndex + 1) -lt $Arguments.Count) {
      $artifactPath = [string]$Arguments[$argumentIndex + 1]
      if (Test-Path -LiteralPath $artifactPath -PathType Leaf) {
        $artifactDirectory = Split-Path -Parent $artifactPath
        $artifactBase = [System.IO.Path]::GetFileNameWithoutExtension($artifactPath)
        $artifactExtension = [System.IO.Path]::GetExtension($artifactPath)
        $attemptArtifact = Join-Path $artifactDirectory ($artifactBase + '-attempt1' + $artifactExtension)
        Move-Item -LiteralPath $artifactPath -Destination $attemptArtifact -Force
      }
    }
  }
  for ($index = $script:tests.Count - 1; $index -ge 0; $index--) {
    if ($script:tests[$index].name -eq $Name) { $script:tests.RemoveAt($index); break }
  }
  for ($index = $script:timeline.Count - 1; $index -ge 0; $index--) {
    if ($script:timeline[$index].test -eq $Name) { $script:timeline.RemoveAt($index); break }
  }

  $retryDetail = "$Detail One bounded retry was started after the Wokwi API transport closed with code 1006; first attempt evidence is $attemptLogName."
  Invoke-LoggedProcess $Name $Executable $Arguments $WorkingDirectory $LogName $TimeoutSeconds $retryDetail
  $finalResult = $script:tests | Where-Object { $_.name -eq $Name } | Select-Object -Last 1
  if ($finalResult) {
    if ($finalResult.status -eq 'PASS') {
      $finalResult.detail = "$retryDetail Retry scenario completed successfully."
    } else {
      $finalResult.detail = "$($finalResult.detail) The single retry did not pass; first attempt evidence is $attemptLogName."
      Add-Content -LiteralPath (Join-Path $runDir $LogName) -Value "`r`nRETRY_NOTE: one bounded retry was attempted after API transport code 1006; first attempt evidence: $attemptLogName." -Encoding utf8
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

function Set-ExistingResultStatus([string]$Name, [string]$Status, [int]$ExitCode, [string]$Detail) {
  for ($index = $script:tests.Count - 1; $index -ge 0; $index--) {
    if ($script:tests[$index].name -eq $Name) {
      $script:tests[$index].status = $Status
      $script:tests[$index].exit_code = $ExitCode
      $script:tests[$index].detail = $Detail
      break
    }
  }
  for ($index = $script:timeline.Count - 1; $index -ge 0; $index--) {
    if ($script:timeline[$index].test -eq $Name) {
      $script:timeline[$index].status = $Status
      break
    }
  }
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
  $nvsBrokerIsolationRegression = Join-Path $PSScriptRoot 'test_nvs_runtime_broker_isolation.ps1'
  if ((Test-Path -LiteralPath $script:powerShellExecutable -PathType Leaf) -and (Test-Path -LiteralPath $nvsBrokerIsolationRegression -PathType Leaf)) {
    Invoke-LoggedProcess 'nvs-runtime-broker-isolation-regression' $script:powerShellExecutable @('-NoProfile','-File',$nvsBrokerIsolationRegression) $repo 'nvs-runtime-broker-isolation-regression.log' 30 'Regression assertion: NVS firmware keeps host.wokwi.internal:1884 while cloud simulation nodes are redirected away from the demo broker.'
  } else {
    Add-Blocked 'nvs-runtime-broker-isolation-regression' 'nvs-runtime-broker-isolation-regression.log' 'PowerShell executable or NVS runtime broker isolation regression script is unavailable.'
  }
  $nvsWokwiConfigRegression = Join-Path $PSScriptRoot 'test_nvs_runtime_wokwi_config.ps1'
  if ((Test-Path -LiteralPath $script:powerShellExecutable -PathType Leaf) -and (Test-Path -LiteralPath $nvsWokwiConfigRegression -PathType Leaf)) {
    Invoke-LoggedProcess 'nvs-runtime-wokwi-config-regression' $script:powerShellExecutable @('-NoProfile','-File',$nvsWokwiConfigRegression) $repo 'nvs-runtime-wokwi-config-regression.log' 30 'Regression assertion: RFC2217 setting is a separate TOML line and paths point at the fresh test build.'
  } else {
    Add-Blocked 'nvs-runtime-wokwi-config-regression' 'nvs-runtime-wokwi-config-regression.log' 'PowerShell executable or NVS runtime Wokwi config regression script is unavailable.'
  }
  $argumentRegression = Join-Path $PSScriptRoot 'test_msvc_compile_arguments.ps1'
  if ((Test-Path -LiteralPath $script:powerShellExecutable -PathType Leaf) -and (Test-Path -LiteralPath $argumentRegression -PathType Leaf)) {
    Invoke-LoggedProcess 'msvc-compile-argument-regression' $script:powerShellExecutable @('-NoProfile','-File',$argumentRegression) $repo 'msvc-compile-argument-regression.log' 30 'Regression assertion: MSVC /Fo and /Fe are separate arguments, including paths with spaces.'
  } else {
    Add-Blocked 'msvc-compile-argument-regression' 'msvc-compile-argument-regression.log' 'PowerShell executable or MSVC argument regression test script is unavailable.'
  }
  $wokwiFailureRegression = Join-Path $PSScriptRoot 'test_wokwi_failure_classification.ps1'
  if ((Test-Path -LiteralPath $script:powerShellExecutable -PathType Leaf) -and (Test-Path -LiteralPath $wokwiFailureRegression -PathType Leaf)) {
    Invoke-LoggedProcess 'wokwi-transport-classification-regression' $script:powerShellExecutable @('-NoProfile','-File',$wokwiFailureRegression) $repo 'wokwi-transport-classification-regression.log' 30 'Regression assertion: API transport closure is BLOCKED; scenario assertion failures remain FAIL.'
  } else {
    Add-Blocked 'wokwi-transport-classification-regression' 'wokwi-transport-classification-regression.log' 'PowerShell executable or Wokwi transport classification regression script is unavailable.'
  }
  if ($script:python) {
    Invoke-LoggedProcess 'backend-regression' $script:python.Source @('-m','unittest','discover','-s','dashboard','-p','test_*.py','-v') $repo 'backend-tests.log' 120 'Python unittest across dashboard/backend test files, including MQTT gateway TLS fail-closed checks.'
    if ($script:node) {
      Invoke-LoggedProcess 'frontend-regression' $script:node.Source @('test_frontend.js') (Join-Path $repo 'dashboard') 'frontend-tests.log' 60 'Node assertions for dashboard rendering and assets.'
    } else { Add-Blocked 'frontend-regression' 'frontend-tests.log' 'Node.js is not available in PATH.' }
    Invoke-LoggedProcess 'trace-evaluation-regression' $script:python.Source @('-m','unittest','discover','-s','tools/evaluation','-p','test_*.py','-v') $repo 'evaluation-tests.log' 120 'Python unittest over labeled trace evaluation.'
    Invoke-LoggedProcess 'wokwi-config-static' $script:python.Source @('tools/validate_wokwi_scenarios.py') $repo 'wokwi-static-validation.log' 30 'Static scenario/diagram assertions only; not a simulator run.'
    Invoke-LoggedProcess 'wokwi-artifact-validation-regression' $script:python.Source @('-m','unittest','tools.test_wokwi_validation','-v') $repo 'wokwi-artifact-validation-tests.log' 60 'Regression tests for static validation with/without binaries and separate post-build artifact validation.'
    Invoke-LoggedProcess 'firmware-nvs-runtime-driver-unit' $script:python.Source @('-m','unittest','discover','-s','tools/e2e','-p','test_firmware_nvs_replay_driver.py','-v') $repo 'nvs-runtime-driver-unit.log' 30 'Unit tests for runtime driver serial parsing and identity assertions; not firmware runtime evidence.'
    Invoke-LoggedProcess 'firmware-nvs-runtime-vcd-unit' $script:python.Source @('-m','unittest','discover','-s','tools/e2e','-p','test_verify_nvs_runtime_vcd.py','-v') $repo 'nvs-runtime-vcd-unit.log' 30 'Unit tests prove red GPIO and buzzer VCD assertions reject missing or inactive output traces; not Wokwi runtime evidence.'
    $mqttHarness = Join-Path $repo 'tools/e2e/mqtt_ack_harness.py'
    if (Test-Path $mqttHarness) {
      Invoke-LoggedProcess 'mqtt-sqlite-app-ack-dedup-harness' $script:python.Source @($mqttHarness) $repo 'mqtt-integration.log' 60 'Protocol/backend integration harness with isolated loopback broker and temporary SQLite; does not use firmware/NVS.'
    } else { Add-Blocked 'mqtt-sqlite-app-ack-dedup-harness' 'mqtt-integration.log' 'Isolated MQTT/backend harness is not available in this checkout.' }
    $mqttSecurityTests = Join-Path $repo 'tools/e2e/test_mqtt_security_static.py'
    if (Test-Path -LiteralPath $mqttSecurityTests -PathType Leaf) {
      Invoke-LoggedProcess 'mqtt-security-source-regression' $script:python.Source @('-m','unittest','tools.e2e.test_mqtt_security_static','-v') $repo 'mqtt-security-source-regression.log' 60 'Source/config assertions for fail-closed TLS, ACL policy, and exact NVS-head ACK handling.'
    } else { Add-Blocked 'mqtt-security-source-regression' 'mqtt-security-source-regression.log' 'MQTT security source regression suite is unavailable.' }
    $mqttSecurityHarness = Join-Path $repo 'tools/e2e/mqtt_security_harness.py'
    if (Test-Path -LiteralPath $mqttSecurityHarness -PathType Leaf) {
      Invoke-LoggedProcess 'mqtt-security-policy-harness' $script:python.Source @($mqttSecurityHarness) $repo 'mqtt-security-policy-harness.log' 60 'Loopback-only custom Python policy broker; exercises auth, TLS CA/hostname checks, ACL read/write, and forged ACK denial. Not Mosquitto or firmware runtime.'
    } else { Add-Blocked 'mqtt-security-policy-harness' 'mqtt-security-policy-harness.log' 'Isolated MQTT security harness is unavailable.' }
  } else {
    Add-Blocked 'backend-regression' 'backend-tests.log' 'Python is not available in PATH.'
    Add-Blocked 'frontend-regression' 'frontend-tests.log' 'Python launcher unavailable; frontend test was not scheduled.'
    Add-Blocked 'trace-evaluation-regression' 'evaluation-tests.log' 'Python is not available in PATH.'
    Add-Blocked 'wokwi-config-static' 'wokwi-static-validation.log' 'Python is not available in PATH.'
    Add-Blocked 'wokwi-artifact-validation-regression' 'wokwi-artifact-validation-tests.log' 'Python is not available in PATH.'
    Add-Blocked 'firmware-nvs-runtime-driver-unit' 'nvs-runtime-driver-unit.log' 'Python is not available in PATH.'
    Add-Blocked 'mqtt-sqlite-app-ack-dedup-harness' 'mqtt-integration.log' 'Python is not available in PATH.'
  }

  if ($script:cpp) {
    $fw = Join-Path $repo 'firmware'
    $harnesses = @(
      @{ name='detector-state-machine'; file='detector_recovery_test.cpp'; sources=@('src/Detector.cpp') },
      @{ name='nvs-queue-policy'; file='event_queue_policy_test.cpp'; sources=@() },
      @{ name='nvs-failure-injection'; file='event_queue_nvs_failure_test.cpp'; sources=@() },
      @{ name='ack-remove-source-regression'; file='event_ack_remove_source_test.cpp'; sources=@() },
      @{ name='sensor-drivers-unit'; file='sensor_drivers_test.cpp'; sources=@('src/SensorDrivers.cpp') },
      @{ name='sensor-health-unit'; file='sensor_health_test.cpp'; sources=@('src/Detector.cpp') },
      @{ name='mq7b-heater-sequencer-unit'; file='mq7b_heater_sequencer_test.cpp'; sources=@() }
    )
    foreach ($h in $harnesses) {
      $source = Join-Path $fw ('tests/' + $h.file)
      if (-not (Test-Path $source)) { Add-Blocked $h.name ($h.name + '.log') 'Harness has not been added to this checkout.'; continue }
      $binary = Join-Path $runDir ($h.name + '.exe')
      if ($script:cppKind -eq 'msvc') {
        $objectDir = Join-Path $runDir ($h.name + '-objects')
        New-Item -ItemType Directory -Path $objectDir -Force | Out-Null
        $compileSources = @($source)
        foreach ($s in $h.sources) { $compileSources += (Join-Path $fw $s) }
        $compileArgs = New-MsvcHarnessCompileArguments `
          -IncludeDirectory (Join-Path $fw 'include') `
          -SourceFiles $compileSources `
          -ObjectDirectory $objectDir `
          -ExecutablePath $binary
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
      if ($h.name -eq 'detector-state-machine') {
        $detectorResult = $script:tests | Where-Object { $_.name -eq 'detector-state-machine' } | Select-Object -Last 1
        $metricLogName = 'cross-interference-metrics.log'
        if ($detectorResult -and $detectorResult.status -eq 'PASS') {
          $detectorLogPath = Join-Path $runDir 'detector-state-machine.log'
          $metricLines = @(Get-Content -LiteralPath $detectorLogPath | Where-Object { $_ -match '^negative_interference_metrics gas=' })
          if ($metricLines.Count -eq 3 -and ($metricLines -match 'gas=LPG') -and ($metricLines -match 'gas=CO') -and ($metricLines -match 'gas=ALL')) {
            Set-Content -LiteralPath (Join-Path $runDir $metricLogName) -Value ($metricLines -join "`r`n") -Encoding utf8
            Add-Result 'cross-interference-metrics' 'PASS' 0 $metricLogName 'Three measured synthetic metric rows (LPG, CO, aggregate) were emitted by the passing Detector harness.' (Get-Date) (Get-Date) '' $false
          } else {
            Set-Content -LiteralPath (Join-Path $runDir $metricLogName) -Value "FAIL: expected LPG, CO, and aggregate metric rows from detector-state-machine.log; found $($metricLines.Count)." -Encoding utf8
            Add-Result 'cross-interference-metrics' 'FAIL' 1 $metricLogName 'Detector harness passed but did not emit the required per-gas FNR/latency metrics.' (Get-Date) (Get-Date) '' $false
          }
        } else {
          Add-Blocked 'cross-interference-metrics' $metricLogName 'Detector host harness did not PASS, so synthetic FNR/latency metrics were not accepted.'
        }
      }
    }
  } else {
    foreach ($name in @('detector-state-machine','nvs-queue-policy','nvs-failure-injection','ack-remove-source-regression','sensor-drivers-unit','sensor-health-unit','mq7b-heater-sequencer-unit')) {
      Add-Blocked $name ($name + '.log') 'No host C++ compiler was found (checked g++, clang++, and cl.exe). Xtensa firmware compiler is not a substitute. Run from an x64 Native Tools PowerShell prompt with cl.exe, or from a shell where g++/clang++ is already available; the runner does not install tools.'
    }
  }
  if (-not $ForFull) {
    Add-NotTested 'firmware-nvs-offline-replay-runtime' 'nvs-replay.log' 'Quick mode deliberately excludes firmware runtime, reboot persistence, and broker outage scenarios.'
    Add-NotTested 'nvs-runtime-gpio-vcd' 'nvs-runtime-gpio-vcd.log' 'Quick mode does not start Wokwi or capture simulated GPIO traces.'
    Add-NotTested 'wokwi-node3-sensor-faults' 'wokwi-node3-sensor-faults.log' 'Quick mode does not start the Wokwi sensor-fault scenario.'
    Add-NotTested 'wokwi-node3-sensor-faults-gpio-vcd' 'wokwi-node3-sensor-faults-gpio-vcd.log' 'Quick mode does not start Wokwi or capture sensor-fault GPIO traces.'
    Add-NotTested 'firmware-mqtt-tls-runtime' 'firmware-mqtt-tls-runtime.log' 'Quick mode does not run a firmware TLS simulator session.'
    Add-NotTested 'mqtt-security-test-ca-fixture' 'mqtt-security-test-ca.log' 'Quick mode does not create a compile-only TLS certificate fixture.'
    Add-NotTested 'firmware-build-mqtt-tls-compile' 'firmware-mqtt-tls-build.log' 'Quick mode does not invoke PlatformIO secure transport compilation.'
    Add-NotTested 'mqtt-security-mosquitto-runtime' 'mqtt-security-mosquitto-runtime.log' 'Quick mode does not run a Mosquitto broker compatibility test.'
  }
}

function Get-IsolatedWokwiWindow([string]$Marker) {
  return @(Get-Process Code -ErrorAction SilentlyContinue |
    Where-Object { $_.MainWindowHandle -ne 0 -and $_.MainWindowTitle -like "*$Marker*" })
}

function Send-IsolatedWokwiCommand([string]$Marker, [string]$CommandTitle) {
  $windows = @(Get-IsolatedWokwiWindow $Marker)
  if ($windows.Count -ne 1) {
    throw "Expected exactly one test-owned VS Code window containing '$Marker'; found $($windows.Count)."
  }
  $window = $windows[0]
  $shell = New-Object -ComObject WScript.Shell
  if (-not $shell.AppActivate([int]$window.Id)) {
    throw "Could not activate the test-owned VS Code window for '$Marker'."
  }
  Start-Sleep -Milliseconds 500
  $shell.SendKeys('^+p')
  Start-Sleep -Milliseconds 350
  $shell.SendKeys($CommandTitle)
  Start-Sleep -Milliseconds 500
  $shell.SendKeys('{ENTER}')
}

function Focus-IsolatedWokwiWindow([string]$Marker) {
  $windows = @(Get-IsolatedWokwiWindow $Marker)
  if ($windows.Count -ne 1) {
    throw "Expected exactly one test-owned VS Code window containing '$Marker' while focusing the simulator; found $($windows.Count)."
  }
  $shell = New-Object -ComObject WScript.Shell
  if (-not $shell.AppActivate([int]$windows[0].Id)) {
    throw "Could not keep the test-owned Wokwi window '$Marker' in the foreground."
  }
  Start-Sleep -Milliseconds 300
}

function Wait-IsolatedWokwiRfc2217Listener([int]$Port, [string]$Marker, [int]$TimeoutSeconds = 25) {
  $testWindows = @(Get-IsolatedWokwiWindow $Marker)
  if ($testWindows.Count -ne 1) {
    throw "Expected exactly one test-owned VS Code window '$Marker' while verifying RFC2217 listener ownership; found $($testWindows.Count)."
  }
  $testRootIds = @($testWindows | ForEach-Object { [int]$_.Id })
  $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
  while ((Get-Date) -lt $deadline) {
    try {
      $listeners = @(Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction Stop)
    } catch {
      throw "Could not inspect local TCP listener state for port $Port ($($_.Exception.GetType().Name)): $($_.Exception.Message)"
    }
    if ($listeners.Count -gt 0) {
      $details = [System.Collections.Generic.List[string]]::new()
      foreach ($listener in $listeners) {
        $ownerPid = [int]$listener.OwningProcess
        $processName = 'unknown'
        try {
          $owner = Get-Process -Id $ownerPid -ErrorAction Stop
          $processName = $owner.ProcessName
        } catch {}
        try {
          $belongsToTestWindow = Test-IsolatedWokwiProcessTree $ownerPid $testRootIds $null
        } catch {
          throw "Could not verify RFC2217 listener ownership for test VS Code window '$Marker': $($_.Exception.Message)"
        }
        $listenerDescription = '{0}:{1} pid={2} process={3}' -f $listener.LocalAddress,$listener.LocalPort,$ownerPid,$processName
        if (-not $belongsToTestWindow) {
          throw "RFC2217 port $Port has a listener that does not belong to test VS Code window '$Marker'; refusing serial connection. Listener: $listenerDescription"
        }
        $details.Add("$listenerDescription; owner is in test window process tree")
      }
      return ($details -join '; ')
    }
    Start-Sleep -Milliseconds 250
  }
  throw "Wokwi did not expose an RFC2217 TCP listener on port $Port within $TimeoutSeconds seconds after Start Simulator. The simulator endpoint was never attached."
}

function Start-IsolatedWokwiSimulation([string]$ProjectPath) {
  $marker = Split-Path -Leaf $ProjectPath
  if (@(Get-IsolatedWokwiWindow $marker).Count -gt 0) {
    throw "A VS Code window already uses the unique test project marker '$marker'; refusing to interact with it."
  }
  $arguments = @('--new-window', $ProjectPath)
  $argumentLine = (@($arguments | ForEach-Object { ConvertTo-WindowsCommandLineArgument ([string]$_) }) -join ' ')
  $null = Start-Process -FilePath $script:vsCodePath -ArgumentList $argumentLine -PassThru -WindowStyle Normal
  $deadline = (Get-Date).AddSeconds(60)
  while ((Get-Date) -lt $deadline) {
    $windows = @(Get-IsolatedWokwiWindow $marker)
    if ($windows.Count -eq 1) {
      try {
        Send-IsolatedWokwiCommand $marker 'Wokwi: Start Simulator'
        Start-Sleep -Milliseconds 1200
        Focus-IsolatedWokwiWindow $marker
        return $marker
      } catch {
        Stop-IsolatedWokwiWindow $marker
        throw
      }
    }
    Start-Sleep -Milliseconds 500
  }
  throw "VS Code did not open a test-owned window for '$marker' within 60 seconds."
}

function Stop-IsolatedWokwiWindow([string]$Marker, [string]$VcdPath = '') {
  $windows = @(Get-IsolatedWokwiWindow $Marker)
  if ($windows.Count -ne 1) { return }
  $stopCommandError = ''
  try { Send-IsolatedWokwiCommand $Marker 'Stop Simulation' } catch { $stopCommandError = Protect-DiagnosticText $_.Exception.Message }
  if (-not [string]::IsNullOrWhiteSpace($VcdPath)) {
    $vcdReadyBeforeClose = Wait-ForNonEmptyFile $VcdPath 15
    Set-Content -LiteralPath (Join-Path $runDir 'nvs-runtime-wokwi-stop.log') `
      -Value ("Stop Simulation requested before closing test window.`r`nSTOP_COMMAND_ERROR: $stopCommandError`r`nVCD_PATH: $VcdPath`r`nVCD_NONEMPTY_BEFORE_CLOSE: $vcdReadyBeforeClose") -Encoding utf8
  }
  $windows = @(Get-IsolatedWokwiWindow $Marker)
  if ($windows.Count -eq 1) {
    try {
      $shell = New-Object -ComObject WScript.Shell
      if ($shell.AppActivate([int]$windows[0].Id)) {
        Start-Sleep -Milliseconds 250
        $shell.SendKeys('^+w')
      }
    } catch {}
  }
}

function Invoke-FullTests {
  $passwordCheckStart = Get-Date
  if (-not [string]::IsNullOrWhiteSpace($env:GAS_MQTT_PASSWORD)) {
    $passwordPreflightDetail = 'Required build environment variable is present; its value was not logged.'
    Set-Content -LiteralPath (Join-Path $runDir 'mqtt-password-preflight.log') -Value 'PASS: GAS_MQTT_PASSWORD is present and non-empty. Its value was not recorded.' -Encoding utf8
    Add-Result 'mqtt-build-password-preflight' 'PASS' 0 'mqtt-password-preflight.log' $passwordPreflightDetail $passwordCheckStart (Get-Date) '' $false
  } else {
    Add-Blocked 'mqtt-build-password-preflight' 'mqtt-password-preflight.log' 'GAS_MQTT_PASSWORD is missing or empty in this process. The production node1/node2/node3 build will be skipped; the isolated NVS test target uses no production password. The runner never requests or records a password value.'
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
    $text = Set-IsolatedSimulationNodeBroker -PlatformIoText $text -TestHomeId $testHome
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
      if (-not $isolatedArtifactTreeIsFresh) {
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
        if ($preflightResult.status -eq 'PASS' -and [string]::IsNullOrWhiteSpace($env:GAS_MQTT_PASSWORD)) {
          Add-Blocked 'firmware-build-node1-node2-node3' 'firmware-build.log' 'Production node build skipped because GAS_MQTT_PASSWORD is missing or empty in this process. No credential was logged.'
        } elseif ($preflightResult.status -eq 'PASS') {
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
          Invoke-WokwiNodeScenarioWithRetry ('wokwi-' + $node) $script:wokwiPath @($simDir,'--scenario',$scenario,'--timeout','60000','--timeout-exit-code','124','--serial-log-file',(Join-Path $runDir ($node + '-serial.log')),'--vcd-file',$vcd) $repo ('wokwi-' + $node + '.log') 180 'Wokwi simulation in an isolated source copy. GPIO VCD is collected when supported.'
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

    $faultScenarioName = 'wokwi-node3-sensor-faults'
    $faultVcd = Join-Path $runDir 'node3-sensor-faults-gpio.vcd'
    $faultSerial = Join-Path $runDir 'node3-sensor-faults-serial.log'
    if ($buildResult -and $buildResult.status -eq 'PASS' -and $artifactResult -and $artifactResult.status -eq 'PASS' -and $script:wokwiPath -and $script:wokwiTokenPresent) {
      $faultSimDir = Join-Path $isolatedFirmware 'sim/node3'
      $faultScenario = 'scenarios/sensor-faults.yaml'
      if (Test-Path -LiteralPath (Join-Path $faultSimDir $faultScenario) -PathType Leaf) {
        Invoke-WokwiNodeScenarioWithRetry $faultScenarioName $script:wokwiPath @($faultSimDir,'--scenario',$faultScenario,'--timeout','60000','--timeout-exit-code','124','--serial-log-file',$faultSerial,'--vcd-file',$faultVcd) $repo ($faultScenarioName + '.log') 180 'Wokwi Node 3 CO-only and dual-sensor FAULT/recovery scenario with serial and GPIO VCD evidence; simulation only.'
        $faultResult = $script:tests | Where-Object { $_.name -eq $faultScenarioName } | Select-Object -Last 1
        if ($faultResult -and $faultResult.status -eq 'PASS') {
          Invoke-LoggedProcess 'wokwi-node3-sensor-faults-gpio-vcd' $script:python.Source @('tools/verify_wokwi_vcd.py',$faultVcd) $repo 'wokwi-node3-sensor-faults-gpio-vcd.log' 30 'Independent simulated GPIO VCD checks for FAULT LED/buzzer activity; not physical sensor evidence.'
        } else {
          Add-Blocked 'wokwi-node3-sensor-faults-gpio-vcd' 'wokwi-node3-sensor-faults-gpio-vcd.log' 'CO/dual-FAULT scenario did not PASS, so its GPIO VCD was not accepted.'
        }
      } else {
        Add-Blocked $faultScenarioName ($faultScenarioName + '.log') "Sensor-fault Wokwi scenario is missing: $faultScenario"
        Add-Blocked 'wokwi-node3-sensor-faults-gpio-vcd' 'wokwi-node3-sensor-faults-gpio-vcd.log' 'Sensor-fault scenario was unavailable.'
      }
    } elseif (-not $buildResult -or $buildResult.status -ne 'PASS') {
      Add-Blocked $faultScenarioName ($faultScenarioName + '.log') 'Fresh three-node firmware build did not PASS; sensor-fault scenario was not run.'
      Add-Blocked 'wokwi-node3-sensor-faults-gpio-vcd' 'wokwi-node3-sensor-faults-gpio-vcd.log' 'Fresh firmware build did not PASS.'
    } elseif (-not $artifactResult -or $artifactResult.status -ne 'PASS') {
      Add-Blocked $faultScenarioName ($faultScenarioName + '.log') 'Fresh node3 firmware artifact validation did not PASS; sensor-fault scenario was not run.'
      Add-Blocked 'wokwi-node3-sensor-faults-gpio-vcd' 'wokwi-node3-sensor-faults-gpio-vcd.log' 'Fresh node3 artifact validation did not PASS.'
    } elseif (-not $script:wokwiPath) {
      Add-Blocked $faultScenarioName ($faultScenarioName + '.log') 'Wokwi CLI is unavailable; CO/dual-FAULT runtime was not run.'
      Add-Blocked 'wokwi-node3-sensor-faults-gpio-vcd' 'wokwi-node3-sensor-faults-gpio-vcd.log' 'Wokwi runtime was unavailable.'
    } else {
      Add-Blocked $faultScenarioName ($faultScenarioName + '.log') 'WOKWI_CLI_TOKEN is not present in the current process environment; CO/dual-FAULT runtime was not run.'
      Add-Blocked 'wokwi-node3-sensor-faults-gpio-vcd' 'wokwi-node3-sensor-faults-gpio-vcd.log' 'CO/dual-FAULT runtime did not run because Wokwi authentication was unavailable.'
    }
  } catch {
    $runnerError = $_
    Write-RunnerDiagnostic 'firmware-build-node1-node2-node3' '<prepare-isolated-firmware-copy>' 'RUNNER_ERROR_DURING_PREPARATION' $runnerError
    Add-Blocked 'firmware-build-node1-node2-node3' 'firmware-build.log' ("Runner could not prepare isolated build source: $(Protect-DiagnosticText $runnerError.Exception.Message). See runner-errors.log.")
    foreach ($node in @('node1','node2','node3')) { Add-Blocked ('wokwi-' + $node) ('wokwi-' + $node + '.log') 'Isolated build source could not be prepared.' }
  }

  $securitySourceTests = Join-Path $repo 'tools/e2e/test_mqtt_security_static.py'
  $securityHarness = Join-Path $repo 'tools/e2e/mqtt_security_harness.py'
  $securityCaPath = Join-Path $runDir 'mqtt-security-test-ca.pem'
  if ($script:python -and (Test-Path -LiteralPath $securitySourceTests -PathType Leaf) -and (Test-Path -LiteralPath $securityHarness -PathType Leaf)) {
    Invoke-LoggedProcess 'mqtt-security-test-ca-fixture' $script:python.Source @($securityHarness,'--write-ca',$securityCaPath) $repo 'mqtt-security-test-ca.log' 60 'Generates disposable public test CA PEM in this run directory; no private key or credential is retained.'
    $caFixtureResult = $script:tests | Where-Object { $_.name -eq 'mqtt-security-test-ca-fixture' } | Select-Object -Last 1
    $toolchainResult = $script:tests | Where-Object { $_.name -eq 'platformio-toolchain-preflight' } | Select-Object -Last 1
    if ($caFixtureResult -and $caFixtureResult.status -eq 'PASS' -and $toolchainResult -and $toolchainResult.status -eq 'PASS' -and $script:pioModule -and $isolatedFirmware) {
      $script:mqttCaCertPath = $securityCaPath
      Invoke-LoggedProcess 'firmware-build-mqtt-tls-compile' $script:python.Source @('-m','platformio','run','-d',$isolatedFirmware,'-e','node1_mqtt_tls_compile') $repo 'firmware-mqtt-tls-build.log' 300 'Compile-only secure transport profile using the run-local ephemeral CA; no broker connection and no production credential.'
    } else {
      Add-Blocked 'firmware-build-mqtt-tls-compile' 'firmware-mqtt-tls-build.log' 'Compile-only TLS profile was skipped because the CA fixture, PlatformIO, or direct Xtensa compiler preflight did not PASS.'
    }
    Add-Blocked 'firmware-mqtt-tls-runtime' 'firmware-mqtt-tls-runtime.log' 'TLS firmware runtime was not run: this target is compile-only and no firmware-backed TLS broker session is configured in this runner.'
    if ($script:python -and $script:mosquitto -and $script:mosquittoPasswd -and
        (Test-Path -LiteralPath $script:mosquitto.Source -PathType Leaf) -and
        (Test-Path -LiteralPath $script:mosquittoPasswd.Source -PathType Leaf)) {
      $mosquittoRuntimeOutput = Join-Path $runDir 'mosquitto-runtime'
      Invoke-LoggedProcess 'mqtt-security-mosquitto-runtime' $script:python.Source @(
        'tools/e2e/mosquitto_security_runtime.py',
        '--mosquitto-exe',$script:mosquitto.Source,
        '--mosquitto-passwd-exe',$script:mosquittoPasswd.Source,
        '--output-dir',$mosquittoRuntimeOutput
      ) $repo 'mqtt-security-mosquitto-runtime.log' 60 "Real Mosquitto $script:mosquittoVersion loopback TLS/auth/ACL runtime adapter; generated test credentials and certificates are disposable."
    } else {
      Add-Blocked 'mqtt-security-mosquitto-runtime' 'mqtt-security-mosquitto-runtime.log' 'A real Mosquitto executable, mosquitto_passwd, or Python runtime is unavailable; runtime security tests were not run.'
    }
  } else {
    Add-Blocked 'mqtt-security-test-ca-fixture' 'mqtt-security-test-ca.log' 'Python security harness or source regression suite is unavailable.'
    Add-Blocked 'firmware-build-mqtt-tls-compile' 'firmware-mqtt-tls-build.log' 'Compile-only TLS profile was not built because its CA fixture could not be prepared.'
    Add-Blocked 'firmware-mqtt-tls-runtime' 'firmware-mqtt-tls-runtime.log' 'TLS firmware runtime was not run because the security harness is unavailable.'
    Add-Blocked 'mqtt-security-mosquitto-runtime' 'mqtt-security-mosquitto-runtime.log' 'Mosquitto runtime integration was not run because the security harness is unavailable.'
  }

  $runtimeToolchainPreflight = $script:tests | Where-Object { $_.name -eq 'platformio-toolchain-preflight' } | Select-Object -Last 1
  if ($runtimeToolchainPreflight -and $runtimeToolchainPreflight.status -eq 'PASS' -and $script:python -and $script:pioModule -and $isolatedFirmware -and $isolatedArtifactTreeIsFresh) {
    Invoke-LoggedProcess 'firmware-build-nvs-runtime' $script:python.Source @('-m','platformio','run','-d',$isolatedFirmware,'-e','node1_nvs_runtime') $repo 'firmware-nvs-runtime-build.log' 300 'Fresh isolated simulation test firmware build; its dedicated target uses no production password and connects only to the loopback-only test broker through Wokwi Private Gateway.'
  } elseif (-not $script:python) {
    Add-Blocked 'firmware-build-nvs-runtime' 'firmware-nvs-runtime-build.log' 'Python/PlatformIO is unavailable; isolated NVS runtime firmware was not built.'
  } else {
    Add-Blocked 'firmware-build-nvs-runtime' 'firmware-nvs-runtime-build.log' 'Isolated NVS runtime firmware build was skipped because PlatformIO/compiler preflight, source isolation, or Python availability did not PASS.'
  }

  $runtimeBuild = $script:tests | Where-Object { $_.name -eq 'firmware-build-nvs-runtime' } | Select-Object -Last 1
  if (-not $runtimeBuild -or $runtimeBuild.status -ne 'PASS') {
    Add-Blocked 'firmware-nvs-offline-replay-runtime' 'nvs-replay.log' 'Firmware-backed NVS replay driver was not started because its fresh test firmware build did not PASS; see firmware-nvs-runtime-build.log.'
  } else {
    $runtimeBin = Join-Path $isolatedFirmware '.pio/build/node1_nvs_runtime/firmware.bin'
    $runtimeElf = Join-Path $isolatedFirmware '.pio/build/node1_nvs_runtime/firmware.elf'
    $artifactStart = Get-Date
    if ((Test-Path -LiteralPath $runtimeBin -PathType Leaf) -and (Test-Path -LiteralPath $runtimeElf -PathType Leaf) -and (Get-Item -LiteralPath $runtimeBin).Length -gt 0 -and (Get-Item -LiteralPath $runtimeElf).Length -gt 0) {
      $artifactMessage = "PASS: fresh NVS runtime BIN/ELF exist under this run's isolated .pio tree."
      Set-Content -LiteralPath (Join-Path $runDir 'nvs-runtime-artifact-validation.log') -Value $artifactMessage -Encoding utf8
      Add-Result 'nvs-runtime-artifact-validation' 'PASS' 0 'nvs-runtime-artifact-validation.log' $artifactMessage $artifactStart (Get-Date) '' $false
      $runtimeSimDir = Join-Path $runDir 'nvs-runtime-wokwi'
      try {
        New-Item -ItemType Directory -Path $runtimeSimDir -Force | Out-Null
        $runtimeDiagram = Get-Content -LiteralPath (Join-Path $isolatedFirmware 'sim/node1/diagram.json') -Raw
        $runtimeDiagram = Set-NvsRuntimeLogicAnalyzerTrigger -DiagramJson $runtimeDiagram
        Write-Utf8NoBom (Join-Path $runtimeSimDir 'diagram.json') $runtimeDiagram
        $runtimeRfc2217Port = Get-FreeNvsRuntimeRfc2217Port
        $runtimeWokwi = New-NvsRuntimeWokwiConfig -FirmwareRelativePath '../isolated-source/firmware/.pio/build/node1_nvs_runtime/firmware.bin' -ElfRelativePath '../isolated-source/firmware/.pio/build/node1_nvs_runtime/firmware.elf' -Rfc2217ServerPort $runtimeRfc2217Port -VcdRelativePath 'nvs-runtime-gpio.vcd'
        Write-Utf8NoBom (Join-Path $runtimeSimDir 'wokwi.toml') $runtimeWokwi
        if (-not $script:vsCodePath) {
          Add-Blocked 'firmware-nvs-offline-replay-runtime' 'nvs-replay.log' 'The local firmware runtime test requires Wokwi for VS Code with its Private IoT Gateway; the VS Code CLI was not found. Wokwi CLI cloud cannot reach the loopback test broker.'
        } elseif (-not $script:wokwiVscodeExtensionPath -or -not (Test-Path -LiteralPath (Join-Path $script:wokwiVscodeExtensionPath 'package.json') -PathType Leaf)) {
          Add-Blocked 'firmware-nvs-offline-replay-runtime' 'nvs-replay.log' 'The local firmware runtime test requires the Wokwi VS Code extension, which was not found in the current user extension directory.'
        } else {
            $runtimeWindowMarker = $null
            $runtimeDriverWasStarted = $false
            try {
              $runtimeWindowMarker = Start-IsolatedWokwiSimulation $runtimeSimDir
              $listenerEvidence = Wait-IsolatedWokwiRfc2217Listener $runtimeRfc2217Port $runtimeWindowMarker 25
              Set-Content -LiteralPath (Join-Path $runDir 'nvs-runtime-wokwi-launch.log') -Value ("Opened the unique test VS Code window, started Wokwi, and foregrounded it.`r`nSelected dual-stack RFC2217 port: $runtimeRfc2217Port`r`nRFC2217 listener: $listenerEvidence`r`nThe runtime driver verifies the protocol handshake.") -Encoding utf8
              $runtimeDriverWasStarted = $true
               Invoke-LoggedProcess 'firmware-nvs-offline-replay-runtime' $script:python.Source @('tools/e2e/firmware_nvs_replay.py','--phase','both','--output-dir',$runDir,'--serial-debug','--serial-url',"rfc2217://127.0.0.1:$runtimeRfc2217Port") $repo 'nvs-replay.log' 600 'Actual firmware-backed local Wokwi run covers LPG DANGER plus LPG/CO FAULT offline, reboot persistence, SQLite commit, application ACK, exact NVS removal, and GPIO VCD. Broker is loopback-only on 1884; app ACK is distinct from QoS 1 PUBACK.'
            } catch {
              $launchError = Protect-DiagnosticText $_.Exception.Message
              Set-Content -LiteralPath (Join-Path $runDir 'nvs-runtime-wokwi-launch.log') -Value ("BLOCKED: could not open the isolated Wokwi VS Code test project.`r`nDETAIL: $launchError") -Encoding utf8
              Add-Blocked 'firmware-nvs-offline-replay-runtime' 'nvs-replay.log' "Could not launch the isolated local Wokwi test project: $launchError"
            } finally {
              if ($runtimeWindowMarker) { Stop-IsolatedWokwiWindow $runtimeWindowMarker (Join-Path $runtimeSimDir 'nvs-runtime-gpio.vcd') }
            }
            $runtimeDriverResult = $script:tests | Where-Object { $_.name -eq 'firmware-nvs-offline-replay-runtime' } | Select-Object -Last 1
            if ($runtimeDriverResult -and $runtimeDriverResult.status -eq 'PASS') {
              $runtimeVcd = Join-Path $runtimeSimDir 'nvs-runtime-gpio.vcd'
              $vcdDeadline = (Get-Date).AddSeconds(20)
              while ((Get-Date) -lt $vcdDeadline -and (-not (Test-Path -LiteralPath $runtimeVcd -PathType Leaf) -or (Get-Item -LiteralPath $runtimeVcd).Length -eq 0)) { Start-Sleep -Milliseconds 250 }
              if ((Test-Path -LiteralPath $runtimeVcd -PathType Leaf) -and ((Get-Item -LiteralPath $runtimeVcd).Length -gt 0)) {
                Invoke-LoggedProcess 'nvs-runtime-gpio-vcd' $script:python.Source @('tools/e2e/verify_nvs_runtime_vcd.py',$runtimeVcd) $repo 'nvs-runtime-gpio-vcd.log' 30 'Independent Wokwi logic-analyzer trace confirms simulated GPIO7 red and GPIO4 buzzer activity; not physical hardware evidence.'
                $gpioResult = $script:tests | Where-Object { $_.name -eq 'nvs-runtime-gpio-vcd' } | Select-Object -Last 1
                if ($gpioResult -and $gpioResult.status -ne 'PASS') {
                  Set-ExistingResultStatus 'firmware-nvs-offline-replay-runtime' $gpioResult.status $gpioResult.exit_code "Firmware MQTT/NVS/ACK path completed, but required simulated GPIO VCD assertion did not PASS. See nvs-runtime-gpio-vcd.log. $($gpioResult.detail)"
                }
              } else {
                Add-Blocked 'nvs-runtime-gpio-vcd' 'nvs-runtime-gpio-vcd.log' 'Wokwi runtime completed, but no non-empty configured logic-analyzer VCD was saved; LED/buzzer pin behavior was not independently verified.'
                Set-ExistingResultStatus 'firmware-nvs-offline-replay-runtime' 'BLOCKED' 127 'Firmware MQTT/NVS/ACK path completed, but Wokwi did not produce the required simulated GPIO VCD; see nvs-runtime-gpio-vcd.log.'
              }
            } elseif ($runtimeDriverWasStarted) {
              Add-NotTested 'nvs-runtime-gpio-vcd' 'nvs-runtime-gpio-vcd.log' 'The firmware NVS runtime process started but did not PASS, so its alarm GPIO trace was not accepted.'
            }
        }
      } catch {
        $runtimeSetupError = Protect-DiagnosticText $_.Exception.Message
        Add-Blocked 'firmware-nvs-offline-replay-runtime' 'nvs-replay.log' "Could not prepare the isolated local Wokwi runtime project: $runtimeSetupError"
      }
    } else {
      $artifactMessage = 'FAIL: NVS runtime firmware build returned PASS but its fresh BIN/ELF artifacts were missing or empty.'
      Set-Content -LiteralPath (Join-Path $runDir 'nvs-runtime-artifact-validation.log') -Value $artifactMessage -Encoding utf8
      Add-Result 'nvs-runtime-artifact-validation' 'FAIL' 1 'nvs-runtime-artifact-validation.log' $artifactMessage $artifactStart (Get-Date) '' $false
      Add-Blocked 'firmware-nvs-offline-replay-runtime' 'nvs-replay.log' 'Firmware-backed NVS runtime was not started because its fresh firmware artifacts failed validation.'
    }
  }

  if (@($script:tests | Where-Object { $_.name -eq 'nvs-runtime-gpio-vcd' }).Count -eq 0) {
    Add-NotTested 'nvs-runtime-gpio-vcd' 'nvs-runtime-gpio-vcd.log' 'No completed NVS runtime simulator session was available to export and inspect a Wokwi pin trace.'
  }
  $finalNvsResult = $script:tests | Where-Object { $_.name -eq 'firmware-nvs-offline-replay-runtime' } | Select-Object -Last 1
  if ($finalNvsResult) {
    $finalNvsJson = [pscustomobject]@{
      timestamp_utc = (Get-Date).ToUniversalTime().ToString('o')
      stage = 'RUNNER_FINAL_ACCEPTANCE'
      status = $finalNvsResult.status
      detail = $finalNvsResult.detail
    } | ConvertTo-Json -Compress
    [System.IO.File]::AppendAllText((Join-Path $runDir 'nvs-replay.log'), $finalNvsJson + "`r`n", [System.Text.UTF8Encoding]::new($false))
    $runtimeTimelinePath = Join-Path $runDir 'nvs-runtime-timeline.csv'
    if (-not (Test-Path -LiteralPath $runtimeTimelinePath -PathType Leaf)) {
      $timelineRow = [pscustomobject]@{
        timestamp_utc = (Get-Date).ToUniversalTime().ToString('o')
        stage = 'NOT_TESTED'
        device_id = ''
        boot_id = ''
        seq = ''
        queue_count = ''
        payload_sha256 = ''
        detail = "Firmware runtime driver did not produce event observations. Final status: $($finalNvsResult.status). $($finalNvsResult.detail)"
      }
      $timelineCsv = @($timelineRow | ConvertTo-Csv -NoTypeInformation) -join "`r`n"
      [System.IO.File]::WriteAllText($runtimeTimelinePath, $timelineCsv + "`r`n", [System.Text.UTF8Encoding]::new($false))
    }
  }

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
Write-EmptyLog 'wokwi-node3-sensor-faults.log' 'NOT TESTED: quick mode does not run the extra CO/dual-FAULT Wokwi scenario.'
Write-EmptyLog 'wokwi-node3-sensor-faults-gpio-vcd.log' 'NOT TESTED: quick mode does not capture CO/dual-FAULT GPIO VCD.'
Write-EmptyLog 'mqtt-integration.log' 'NOT TESTED: quick mode does not run MQTT integration.'
Write-EmptyLog 'mqtt-security-test-ca.log' 'NOT TESTED: quick mode does not generate a TLS compile-test CA.'
Write-EmptyLog 'firmware-mqtt-tls-build.log' 'NOT TESTED: quick mode does not compile the TLS firmware profile.'
Write-EmptyLog 'firmware-mqtt-tls-runtime.log' 'NOT TESTED: no firmware TLS runtime was executed.'
Write-EmptyLog 'mqtt-security-mosquitto-runtime.log' 'NOT TESTED: quick mode does not run Mosquitto compatibility tests.'
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
