[CmdletBinding()]
param(
  [switch]$ResetPassword
)

$ErrorActionPreference = 'Stop'
$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
# Nạp cùng cấu hình broker cho firmware lúc build và dashboard lúc chạy.
$envFile = Join-Path $repoRoot '.env'
if (-not (Test-Path -LiteralPath $envFile -PathType Leaf)) {
  throw 'Missing .env. Copy .env.example to .env and set MQTT_HOST, MQTT_PORT, and MQTT_USER.'
}

$settings = @{}
$allowedNames = @('MQTT_HOST', 'MQTT_PORT', 'MQTT_USER')
foreach ($line in Get-Content -LiteralPath $envFile) {
  $trimmed = $line.Trim()
  if ([string]::IsNullOrWhiteSpace($trimmed) -or $trimmed.StartsWith('#')) { continue }
  $parts = $trimmed -split '=', 2
  if ($parts.Count -ne 2) { throw 'Invalid .env entry; use KEY=VALUE lines.' }
  $name = $parts[0].Trim()
  $value = $parts[1].Trim()
  if ($name -notin $allowedNames) { throw "Unsupported .env key: $name" }
  if ($value.Length -ge 2 -and $value.StartsWith('"') -and $value.EndsWith('"')) {
    $value = $value.Substring(1, $value.Length - 2)
  }
  if ([string]::IsNullOrWhiteSpace($value)) { throw "$name must not be empty." }
  $settings[$name] = $value
}
foreach ($name in $allowedNames) {
  if (-not $settings.ContainsKey($name)) { throw "Missing $name in .env." }
}
try { $mqttPort = [int]$settings['MQTT_PORT'] } catch { throw 'MQTT_PORT must be an integer from 1 to 65535.' }
if ($mqttPort -lt 1 -or $mqttPort -gt 65535) { throw 'MQTT_PORT must be an integer from 1 to 65535.' }

if ([string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
  throw 'LOCALAPPDATA is unavailable; cannot load the encrypted MQTT password.'
}
# Mật khẩu broker được lưu riêng bằng DPAPI theo tài khoản Windows, không đặt trong .env/Git.
$secretDirectory = Join-Path $env:LOCALAPPDATA 'SmartGasAlertSystem'
$secretPath = Join-Path $secretDirectory 'mqtt-password.dpapi'
$securePassword = $null
$environmentNames = @('MQTT_HOST', 'MQTT_PORT', 'MQTT_USER', 'GAS_MQTT_PASSWORD')
$previousEnvironment = @{}
foreach ($name in $environmentNames) {
  $previousEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
}

try {
  foreach ($name in $allowedNames) {
    [Environment]::SetEnvironmentVariable($name, [string]$settings[$name], 'Process')
  }

  if ($ResetPassword -or -not (Test-Path -LiteralPath $secretPath -PathType Leaf)) {
    $securePassword = Read-Host 'MQTT password (saved encrypted for this Windows user)' -AsSecureString
    if ($securePassword.Length -eq 0) { throw 'MQTT password is empty; nothing was saved.' }
    New-Item -ItemType Directory -Path $secretDirectory -Force | Out-Null
    $encryptedPassword = ConvertFrom-SecureString -SecureString $securePassword
    Set-Content -LiteralPath $secretPath -Value $encryptedPassword -NoNewline -Encoding ascii
  } else {
    try {
      $encryptedPassword = Get-Content -LiteralPath $secretPath -Raw
      $securePassword = ConvertTo-SecureString -String $encryptedPassword
    } catch {
      throw 'The saved MQTT password cannot be decrypted by this Windows account. Run with -ResetPassword to replace it.'
    }
  }
  if ($securePassword.Length -eq 0) { throw 'Saved MQTT password is empty; run with -ResetPassword.' }
  $plainPassword = [System.Net.NetworkCredential]::new('', $securePassword).Password
  [Environment]::SetEnvironmentVariable('GAS_MQTT_PASSWORD', $plainPassword, 'Process')

  $pioCommand = Get-Command platformio.exe -ErrorAction SilentlyContinue | Select-Object -First 1
  $pioPath = if ($pioCommand) { $pioCommand.Source } else { Join-Path $env:USERPROFILE '.platformio\penv\Scripts\platformio.exe' }
  if (-not (Test-Path -LiteralPath $pioPath -PathType Leaf)) {
    throw 'PlatformIO Core was not found. Install PlatformIO Core or add platformio.exe to PATH.'
  }
  $pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue | Select-Object -First 1
  if (-not $pythonCommand) { throw 'Python 3 was not found in PATH.' }

  # Build firmware với cùng MQTT_* và GAS_MQTT_PASSWORD sẽ được dashboard dùng bên dưới.
  Push-Location (Join-Path $repoRoot 'firmware')
  try {
    & $pioPath run
    $buildExitCode = $LASTEXITCODE
    if ($buildExitCode -ne 0) { throw "PlatformIO build failed with exit code $buildExitCode." }
  } finally {
    Pop-Location
  }

  & $pythonCommand.Source (Join-Path $repoRoot 'dashboard\server.py')
  $dashboardExitCode = $LASTEXITCODE
  if ($dashboardExitCode -ne 0) { throw "Dashboard stopped with exit code $dashboardExitCode." }
} finally {
  $plainPassword = $null
  if ($securePassword) { $securePassword.Dispose() }
  foreach ($name in $environmentNames) {
    [Environment]::SetEnvironmentVariable($name, $previousEnvironment[$name], 'Process')
  }
}
