<#
.SYNOPSIS
  Запуск команды завода внутри WSL из Windows: fz.ps1 <команда factory> [аргументы]

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File fz.ps1 setup check
  powershell -ExecutionPolicy Bypass -File fz.ps1 -Detached setup wizard --open
  powershell -ExecutionPolicy Bypass -File fz.ps1 run --dry-run

.PARAMETER Detached
  Запустить в отдельном свёрнутом окне и не ждать: для мастера настройки и долгих прогонов.
  Окно держит WSL запущенным, пока открыто.
#>
param(
    [switch]$Detached,
    [Parameter(ValueFromRemainingArguments = $true)][string[]]$FactoryArgs
)
. "$PSScriptRoot\common.ps1"

$distro = Get-CzDistro
$repo = Get-CzRepoDir
$quoted = ($FactoryArgs | ForEach-Object { ConvertTo-BashArg $_ }) -join ' '
$cmd = "cd $repo && . .venv/bin/activate && factory $quoted"

if ($Detached) {
    Start-Process -FilePath 'wsl.exe' -ArgumentList @('-d', $distro, '--', 'bash', '-lc', "`"$cmd`"") -WindowStyle Minimized
    Write-Output "Запущено в свёрнутом окне WSL: factory $($FactoryArgs -join ' ')"
    exit 0
}
& wsl.exe -d $distro -- bash -lc $cmd
exit $LASTEXITCODE
