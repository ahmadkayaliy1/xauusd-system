<#
.SYNOPSIS
    Removes the autostart tasks created by install_autostart.ps1: TradingSystem, every TradingSystem-<PAIR>
    (one system per pair) and TradingSystem-MT5 - only those in this system's own Task Scheduler folder (config
    supervisor.task_folder, D-051): another pair's folder keeps its tasks.
    Does not stop anything that is running: use scripts\stop.bat / scripts\stop_all.bat for the trading system.
    Removing TradingSystem-MT5 while the terminal it started is open may close that terminal on some Windows
    builds - close MT5 first (or re-open it afterwards) if that matters.
.PARAMETER KeepMT5Task
    Remove only the keep-alive tasks; keep TradingSystem-MT5.
#>
[CmdletBinding()]
param([switch]$KeepMT5Task)
# Continue, not Stop: schtasks writes errors about OTHER tasks to stderr (Stop would abort); exit codes are checked
$ErrorActionPreference = "Continue"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { Write-Host "Python venv not found: $py"; exit 1 }
# D-051: this system's Task Scheduler folder (config supervisor.task_folder, "\" = the root folder as before) and task
# times - every pair's project folder registers, finds and removes only the tasks in its own folder
function Get-TaskConfig([string]$py, [string]$root) {
    $cfg = @{}
    Push-Location $root
    try {
        $lines = @(& { $ErrorActionPreference = "Continue"; & $py -m tradingsystem config --tasks })
        $code = $LASTEXITCODE
    } finally { Pop-Location }
    if ($code -ne 0) { throw "could not read the task settings from config (python -m tradingsystem config --tasks)" }
    foreach ($l in $lines) {
        $kv = "$l" -split "=", 2
        if ($kv.Count -eq 2) { $cfg[$kv[0].Trim()] = $kv[1].Trim() }
    }
    if (-not $cfg["task_folder"]) { throw "config --tasks printed no task_folder" }
    return $cfg
}
function Split-TaskLine([string]$line) {
    # one line of "schtasks /Query /FO CSV /NH": its first field is the task's full name "\Folder\Name"
    $full = ($line -split '","')[0].Trim('"')
    $cut = $full.LastIndexOf('\')
    if ($cut -lt 0) { return @("\", $full) }
    return @($full.Substring(0, $cut + 1), $full.Substring($cut + 1))
}
$taskFolder = (Get-TaskConfig $py $root)["task_folder"]
# schtasks, not Get-ScheduledTask: PowerShell 5.1 cannot read back a trigger that repeats indefinitely (0x80041318)
$names = @()
foreach ($line in (schtasks /Query /FO CSV /NH 2>$null)) {
    $folder, $n = Split-TaskLine $line
    if ($folder -ne $taskFolder) { continue }              # D-051: another folder = another pair's system
    if ($n -eq "TradingSystem" -or $n -like "TradingSystem-*") { $names += $n }
}
$names = @($names | Sort-Object -Unique | Where-Object { -not ($KeepMT5Task -and $_ -eq "TradingSystem-MT5") })
if (-not $names) { Write-Host "no TradingSystem task registered in $taskFolder" }
foreach ($name in $names) {
    schtasks /Delete /TN "$taskFolder$name" /F 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) { Write-Host "removed  $taskFolder$name" } else { Write-Host "FAILED   $taskFolder$name (exit $LASTEXITCODE)" }
}
