# Общие функции для скриптов Windows. Совместимо с Windows PowerShell 5.1.
# Подключение: . "$PSScriptRoot\common.ps1"

$ErrorActionPreference = 'Stop'
$env:WSL_UTF8 = '1'   # вывод wsl.exe в UTF-8, а не UTF-16

function Get-CzDistro {
    if ($env:CZ_DISTRO) { return $env:CZ_DISTRO }
    $names = & wsl.exe -l -q 2>$null | ForEach-Object { ($_ -replace "`0", '').Trim() } | Where-Object { $_ }
    $pick = $names | Where-Object { $_ -match '^Ubuntu' } | Select-Object -First 1
    if (-not $pick) { $pick = $names | Where-Object { $_ -notmatch '^docker-desktop' } | Select-Object -First 1 }
    if (-not $pick) { throw 'Не найден дистрибутив WSL. Установите: wsl --install -d Ubuntu-24.04' }
    return $pick
}

function ConvertTo-BashArg([string]$s) {
    return "'" + ($s -replace "'", "'\''") + "'"
}

function Invoke-Wsl {
    param([string]$Command, [string]$Distro = (Get-CzDistro), [switch]$Root)
    $wslArgs = @('-d', $Distro)
    if ($Root) { $wslArgs += @('-u', 'root') }
    $wslArgs += @('--', 'bash', '-lc', $Command)
    & wsl.exe @wslArgs
    return $LASTEXITCODE
}

function Get-CzRepoDir {
    if ($env:CZ_REPO) { return $env:CZ_REPO }
    return '~/Contentzavod'
}

function Get-CzHome {
    $dir = Join-Path $env:LOCALAPPDATA 'Contentzavod'
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir | Out-Null }
    return $dir
}
