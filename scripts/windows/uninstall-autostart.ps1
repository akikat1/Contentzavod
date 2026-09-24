<#
.SYNOPSIS
  Откат install-autostart.ps1: задачи Планировщика, настройки питания, vmIdleTimeout.
  Автовход выключается вручную: откройте Autologon64.exe и нажмите Disable.
#>
. "$PSScriptRoot\common.ps1"
$czHome = Get-CzHome

foreach ($t in 'Contentzavod Tick', 'Contentzavod KeepAlive', 'Contentzavod Lock') {
    if (Get-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $t -Confirm:$false
        Write-Output "✔ удалена задача «$t»"
    }
}

$backup = Join-Path $czHome 'power_backup.json'
if (Test-Path $backup) {
    $b = Get-Content $backup -Raw | ConvertFrom-Json
    if ($null -ne $b.standby_ac) { & powercfg.exe /change standby-timeout-ac ([int]($b.standby_ac / 60)) }
    if ($null -ne $b.hibernate_ac) { & powercfg.exe /change hibernate-timeout-ac ([int]($b.hibernate_ac / 60)) }
    Remove-Item $backup
    Write-Output '✔ настройки сна восстановлены'
}

$wslcfg = Join-Path $env:USERPROFILE '.wslconfig'
if (Test-Path $wslcfg) {
    $src = @(Get-Content $wslcfg)
    $lines = @()
    for ($i = 0; $i -lt $src.Count; $i++) {
        if ($src[$i].Trim() -eq '# contentzavod') {
            if ($i + 1 -lt $src.Count -and $src[$i + 1] -match '^\s*vmIdleTimeout\s*=\s*-1\s*$') { $i++ }
            continue
        }
        $lines += $src[$i]
    }
    Set-Content -Path $wslcfg -Value $lines -Encoding ASCII
    Write-Output '✔ .wslconfig: строка vmIdleTimeout удалена'
}
foreach ($f in 'tick.vbs', 'keepalive.vbs') {
    $p = Join-Path $czHome $f
    if (Test-Path $p) { Remove-Item $p }
}
Write-Output 'Готово. Автовход: откройте Autologon64.exe и нажмите Disable.'
