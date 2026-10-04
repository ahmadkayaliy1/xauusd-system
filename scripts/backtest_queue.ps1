<#
.SYNOPSIS
    Runs the backtest queue (P12.7) one run after the other, resumably:
        powershell -NoProfile -ExecutionPolicy Bypass -File scripts\backtest_queue.ps1
.DESCRIPTION
    Reads data\backtests\queue.txt (one run per line: "<run id> <tools/backtest.py arguments>", # = comment); the
    claude answers and --max-calls 60 are added. A run with a clean report.json is skipped; a run folder without a
    report (the PC went off) is removed and run again; an interrupted run (the PC slept, a call failed) is kept aside
    once and run again. While data\backtests\PAUSE exists no new run starts. Log: data\backtests\queue.log.
    Refuses to start while another queue runner works. Runs from this checkout (its tools and src), reads the market of
    C:\TradingSystems\XAUUSD\data (read only) and writes only under data\backtests.
#>
$ErrorActionPreference = 'Continue'
$root = Split-Path -Parent $PSScriptRoot
$data = 'C:\TradingSystems\XAUUSD\data'
$bt = Join-Path $data 'backtests'
$py = 'C:\TradingSystems\XAUUSD\.venv\Scripts\python.exe'
$log = Join-Path $bt 'queue.log'
$others = Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" | Where-Object {
    $_.ProcessId -ne $PID -and $_.CommandLine -match 'backtest_queue\.ps1|run_queue2?\.ps1|run_ab\.ps1' }
if ($others) { Write-Host "another backtest queue runs already (pid $($others.ProcessId -join ', ')) - nothing done"; exit 1 }
$env:PYTHONPATH = Join-Path $root 'src'
Set-Location $root
"$(Get-Date -Format o) queue runner started (pid $PID)" | Out-File -Append $log -Encoding utf8
while ($true) {
    $lines = Get-Content (Join-Path $bt 'queue.txt') -ErrorAction SilentlyContinue | ForEach-Object { $_.TrimStart([char]0xFEFF) } | Where-Object { $_ -match '\S' -and $_ -notmatch '^\s*#' }
    $next = $null
    foreach ($l in $lines) {
        $id = ($l -split '\s+')[0]
        $dir = Join-Path $bt $id
        $rep = Join-Path $dir 'report.json'
        if (Test-Path $rep) {
            $j = Get-Content $rep -Raw | ConvertFrom-Json
            $mark = Join-Path $bt "retried_$id"
            if ($j.meta.result.interrupted -and -not (Test-Path $mark)) {
                New-Item -ItemType File $mark | Out-Null
                Rename-Item $dir "$($id)_interrupted_$(Get-Date -Format yyyyMMddHHmmss)"
                "$(Get-Date -Format o) $id was interrupted - kept aside, running it again" | Out-File -Append $log -Encoding utf8
                $next = $l; break
            }
            continue
        }
        if (Test-Path $dir) {
            Remove-Item -Recurse -Force $dir
            "$(Get-Date -Format o) $id had no report (cut off) - removed, running it again" | Out-File -Append $log -Encoding utf8
        }
        $next = $l; break
    }
    if (-not $next) { "$(Get-Date -Format o) QUEUE EMPTY" | Out-File -Append $log -Encoding utf8; break }
    while (Test-Path (Join-Path $bt 'PAUSE')) { Start-Sleep -Seconds 30 }
    $parts = $next -split '\s+'
    $id = $parts[0]; $rest = $parts[1..($parts.Length - 1)]
    "$(Get-Date -Format o) start $id" | Out-File -Append $log -Encoding utf8
    & $py tools/backtest.py --answers claude --max-calls 60 --data $data --run-id $id @rest *>> $log
    "$(Get-Date -Format o) end $id exit=$LASTEXITCODE" | Out-File -Append $log -Encoding utf8
}
