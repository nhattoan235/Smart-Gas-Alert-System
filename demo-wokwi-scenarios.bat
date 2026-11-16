@echo off
setlocal EnableExtensions

for %%I in ("%~dp0.") do set "REPO=%%~fI"
set "SIMROOT=%REPO%\firmware\sim"
set "PIO=%USERPROFILE%\.platformio\penv\Scripts\platformio.exe"
set "WOKWI=D:\Learning\IOT\.tools\wokwi\wokwi-cli.exe"

if not exist "%PIO%" (
  where platformio.exe >nul 2>nul
  if errorlevel 1 (
    echo Khong tim thay PlatformIO. Kiem tra lai cai dat PlatformIO.
    pause
    exit /b 1
  )
  set "PIO=platformio.exe"
)

if not exist "%WOKWI%" (
  where wokwi-cli.exe >nul 2>nul
  if errorlevel 1 (
    echo Khong tim thay Wokwi CLI. Kiem tra lai D:\Learning\IOT\.tools\wokwi.
    pause
    exit /b 1
  )
  set "WOKWI=wokwi-cli.exe"
)

:MENU
cls
echo ================================================
echo     GAS SENTINEL - CHAY TUNG KICH BAN WOKWI
echo ================================================
echo 1. LPG tang muc va phuc hoi - Node 1
echo 2. Loi cam bien LPG va phuc hoi - Node 2
echo 3. CO WARNING/DANGER va phuc hoi - Node 3
echo 4. Cam bien lua F1/F2 - Node 1
echo 5. Loi CO va loi ca hai cam bien - Node 3
echo 0. Thoat
echo.
set "CHOICE="
set /p "CHOICE=Chon kich ban: "

if "%CHOICE%"=="1" call :RUN node1 node1.yaml "LPG tang muc va phuc hoi"
if "%CHOICE%"=="2" call :RUN node2 node2.yaml "Loi cam bien LPG va phuc hoi"
if "%CHOICE%"=="3" call :RUN node3 node3.yaml "CO WARNING/DANGER va phuc hoi"
if "%CHOICE%"=="4" call :RUN node1 flame.yaml "Cam bien lua F1/F2"
if "%CHOICE%"=="5" call :RUN node3 sensor-faults.yaml "Loi CO va loi ca hai cam bien"
if "%CHOICE%"=="0" exit /b 0
if not "%CHOICE%"=="1" if not "%CHOICE%"=="2" if not "%CHOICE%"=="3" if not "%CHOICE%"=="4" if not "%CHOICE%"=="5" if not "%CHOICE%"=="0" echo Lua chon khong hop le.
if not "%CHOICE%"=="0" goto MENU

:RUN
set "NODE=%~1"
set "SCENARIO=%~2"
set "DESCRIPTION=%~3"
echo.
echo Dang chay: %DESCRIPTION%
echo Dang build firmware %NODE% de dung ban moi nhat...
echo Ket qua scenario se hien trong Serial cua CLI.

powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; $tokenSecure=$null; $mqttSecure=$null; $exitCode=1; try { $storePath=Join-Path $env:LOCALAPPDATA 'SmartGasAlertSystem\test-secrets.clixml'; if (-not (Test-Path -LiteralPath $storePath -PathType Leaf)) { throw 'Chua co credential da luu. Chay tools\save-test-secrets.ps1 mot lan truoc.' }; $store=Import-Clixml -LiteralPath $storePath; if ($store.schema -ne 1 -or [string]::IsNullOrWhiteSpace([string]$store.WOKWI_CLI_TOKEN) -or [string]::IsNullOrWhiteSpace([string]$store.GAS_MQTT_PASSWORD)) { throw 'Credential da luu khong hop le. Chay tools\save-test-secrets.ps1 de cap nhat.' }; $tokenSecure=ConvertTo-SecureString -String ([string]$store.WOKWI_CLI_TOKEN); $mqttSecure=ConvertTo-SecureString -String ([string]$store.GAS_MQTT_PASSWORD); $env:WOKWI_CLI_TOKEN=[System.Net.NetworkCredential]::new('', $tokenSecure).Password; $env:GAS_MQTT_PASSWORD=[System.Net.NetworkCredential]::new('', $mqttSecure).Password; Set-Location -LiteralPath '%REPO%\firmware'; & '%PIO%' run -e '%NODE%'; if ($LASTEXITCODE -ne 0) { $exitCode=$LASTEXITCODE } else { Set-Location -LiteralPath '%SIMROOT%\%NODE%'; & '%WOKWI%' . --scenario 'scenarios\%SCENARIO%' --timeout 120000; $exitCode=$LASTEXITCODE } } catch { Write-Error $_.Exception.Message; $exitCode=1 } finally { if ($tokenSecure) { $tokenSecure.Dispose() }; if ($mqttSecure) { $mqttSecure.Dispose() } }; exit $exitCode"

set "RESULT=%ERRORLEVEL%"
echo.
if "%RESULT%"=="0" (
  echo Kich ban da ket thuc thanh cong.
) else (
  echo Kich ban ket thuc voi ma loi %RESULT%. Kiem tra token Wokwi, PlatformIO va log o tren.
)
pause
exit /b %RESULT%
