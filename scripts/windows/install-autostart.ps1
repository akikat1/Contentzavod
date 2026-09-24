<#
.SYNOPSIS
  Режим 24/7 для контент-завода: ПК не засыпает, WSL не выключается, завод запускается сам.

.DESCRIPTION
  Что делает (всё откатывается скриптом uninstall-autostart.ps1):
    1. Питание: сон и гибернация при работе от сети — «никогда» (прежние значения сохраняются для отката).
    2. %UserProfile%\.wslconfig: vmIdleTimeout=-1 — WSL не выключается при простое.
    3. Задачи Планировщика (для текущего пользователя, «только при входе в систему»):
         Contentzavod Tick      — при входе и каждые 30 минут: factory daemon --once (второй экземпляр не запускается)
         Contentzavod KeepAlive — при входе: держит WSL запущенным и поднимает контейнер Telegram Bot API
         Contentzavod Lock      — при входе: сразу блокирует экран (автовход не оставляет ПК открытым)
    4. Автовход после перезагрузки: скачивает Sysinternals Autologon и открывает его.
       Пароль Windows вводите вы — скрипт его не видит и не хранит.

  Почему «при входе», а не «без входа в систему»: задачи без входа выполняются в изолированной сессии 0,
  где WSL запускается ненадёжно. Автовход + блокировка экрана дают тот же результат надёжно.

.PARAMETER SkipAutologon
  Не открывать Autologon (например, если автовход уже настроен).
.PARAMETER NoLock
  Не блокировать экран после входа.
#>
param([switch]$SkipAutologon, [switch]$NoLock, [int]$IntervalMinutes = 30)
. "$PSScriptRoot\common.ps1"

$distro = Get-CzDistro
$repo = Get-CzRepoDir
$czHome = Get-CzHome
$user = "$env:USERDOMAIN\$env:USERNAME"
Write-Output "Дистрибутив WSL: $distro; проект: $repo; служебная папка: $czHome"

# --- 0. завод на месте? ---
$rc = Invoke-Wsl -Distro $distro -Command "cd $repo && test -x .venv/bin/factory"
if ($rc -ne 0) { throw "В WSL не найден $repo/.venv/bin/factory — сначала scripts/setup_wsl.sh" }

# --- 1. питание ---
function Get-PowerIndex([string]$Setting) {
    $out = & powercfg.exe /query SCHEME_CURRENT SUB_SLEEP $Setting
    $hex = [regex]::Matches(($out -join "`n"), '0x[0-9a-fA-F]{8}') | ForEach-Object { $_.Value }
    if ($hex.Count -ge 2) { return [Convert]::ToInt32($hex[$hex.Count - 2], 16) }
    return $null
}
$backup = Join-Path $czHome 'power_backup.json'
if (-not (Test-Path $backup)) {
    @{ standby_ac = (Get-PowerIndex 'STANDBYIDLE'); hibernate_ac = (Get-PowerIndex 'HIBERNATEIDLE') } |
        ConvertTo-Json | Set-Content -Path $backup -Encoding UTF8
}
& powercfg.exe /change standby-timeout-ac 0
& powercfg.exe /change hibernate-timeout-ac 0
Write-Output '✔ Сон и гибернация от сети отключены (экран гаснуть может)'

# --- 2. .wslconfig ---
$wslcfg = Join-Path $env:USERPROFILE '.wslconfig'
$lines = @()
if (Test-Path $wslcfg) { $lines = @(Get-Content -Path $wslcfg -Encoding UTF8) }   # @() — однострочный файл тоже массив
if (-not ($lines -match '^\s*vmIdleTimeout\s*=\s*-1')) {
    $lines = @($lines | Where-Object { $_ -notmatch '^\s*vmIdleTimeout\s*=' })
    $idx = -1
    for ($i = 0; $i -lt $lines.Count; $i++) { if ($lines[$i].Trim() -ieq '[wsl2]') { $idx = $i; break } }
    if ($idx -ge 0) {
        $before = $lines[0..$idx]
        $after = if ($idx + 1 -lt $lines.Count) { $lines[($idx + 1)..($lines.Count - 1)] } else { @() }
        $lines = @($before) + '# contentzavod' + 'vmIdleTimeout=-1' + @($after)
    } else {
        $lines = @($lines) + '' + '[wsl2]' + '# contentzavod' + 'vmIdleTimeout=-1'
    }
    Set-Content -Path $wslcfg -Value $lines -Encoding ASCII
    Write-Output '✔ .wslconfig: vmIdleTimeout=-1 (вступит в силу после wsl --shutdown или перезагрузки)'
} else {
    Write-Output '✔ .wslconfig уже настроен'
}

# --- 3. задачи Планировщика ---
$tickVbs = Join-Path $czHome 'tick.vbs'
$keepVbs = Join-Path $czHome 'keepalive.vbs'
$tickCmd = "cd $repo && mkdir -p data/logs && .venv/bin/factory daemon --once >> data/logs/tick.log 2>&1"
$keepCmd = "cd $repo && (docker compose -f deploy/docker-compose.yml --env-file .env up -d telegram-bot-api " +
           ">/dev/null 2>&1 || true) && exec sleep infinity"
# VBS запускает wsl.exe без окна; кавычки внутри строки VBS удваиваются
$vbs = 'CreateObject("WScript.Shell").Run "wsl.exe -d {0} -- bash -lc ""{1}""", 0, {2}'
Set-Content -Path $tickVbs -Value ($vbs -f $distro, $tickCmd, 'True') -Encoding ASCII
Set-Content -Path $keepVbs -Value ($vbs -f $distro, $keepCmd, 'False') -Encoding ASCII

$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$repeat = (New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) `
    -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes)).Repetition

$tickLogon = New-ScheduledTaskTrigger -AtLogOn -User $user
$tickLogon.Delay = 'PT2M'
$tickLogon.Repetition = $repeat
$tickNow = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes)
$tickSettings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 4) `
    -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName 'Contentzavod Tick' -Force -Principal $principal -Settings $tickSettings `
    -Trigger @($tickLogon, $tickNow) -Action (New-ScheduledTaskAction -Execute 'wscript.exe' -Argument "`"$tickVbs`"") `
    -Description 'Контент-завод: публикации, статистика, производство по плану дня' | Out-Null
Write-Output "✔ Задача «Contentzavod Tick»: при входе и каждые $IntervalMinutes мин"

$keepSettings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName 'Contentzavod KeepAlive' -Force -Principal $principal -Settings $keepSettings `
    -Trigger (New-ScheduledTaskTrigger -AtLogOn -User $user) `
    -Action (New-ScheduledTaskAction -Execute 'wscript.exe' -Argument "`"$keepVbs`"") `
    -Description 'Контент-завод: держит WSL и Telegram Bot API запущенными' | Out-Null
Start-ScheduledTask -TaskName 'Contentzavod KeepAlive'
Write-Output '✔ Задача «Contentzavod KeepAlive»: при входе (запущена сейчас)'

if (-not $NoLock) {
    $lockTrigger = New-ScheduledTaskTrigger -AtLogOn -User $user
    $lockTrigger.Delay = 'PT20S'
    Register-ScheduledTask -TaskName 'Contentzavod Lock' -Force -Principal $principal `
        -Settings (New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries) -Trigger $lockTrigger `
        -Action (New-ScheduledTaskAction -Execute 'rundll32.exe' -Argument 'user32.dll,LockWorkStation') `
        -Description 'Контент-завод: блокировка экрана сразу после автовхода' | Out-Null
    Write-Output '✔ Задача «Contentzavod Lock»: экран блокируется через 20 с после входа'
}

# --- 4. автовход ---
$auto = (Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon' -ErrorAction SilentlyContinue).AutoAdminLogon
if ($auto -eq '1') {
    Write-Output '✔ Автовход уже включён'
} elseif (-not $SkipAutologon) {
    $dir = Join-Path $czHome 'Autologon'
    $exe = Join-Path $dir 'Autologon64.exe'
    if (-not (Test-Path $exe)) {
        $zip = Join-Path $czHome 'AutoLogon.zip'
        Invoke-WebRequest -Uri 'https://download.sysinternals.com/files/AutoLogon.zip' -OutFile $zip -UseBasicParsing
        Expand-Archive -Path $zip -DestinationPath $dir -Force
        Remove-Item $zip
    }
    Write-Output ''
    Write-Output '✋ Открываю Sysinternals Autologon: введите пароль Windows и нажмите Enable.'
    Write-Output '   Пароль хранится Windows в зашифрованном виде (LSA), скрипт его не видит.'
    Start-Process -FilePath $exe -Verb RunAs
} else {
    Write-Output '! Автовход не настроен: после перезагрузки завод стартует только после вашего входа'
}

Write-Output ''
Write-Output 'Готово. Проверка: перезагрузите ПК и через 5 минут выполните  fz.ps1 jobs  и  fz.ps1 setup check'
