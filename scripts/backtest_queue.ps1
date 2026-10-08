<#
.SYNOPSIS
    Runs the backtest queue (P12.7), resumably, one or two workers at a time:
        powershell -NoProfile -ExecutionPolicy Bypass -File scripts\backtest_queue.ps1
    (start it twice for two runs in parallel; a third is refused - the 5-hour plan window and the laptop's RAM)
.DESCRIPTION
    Reads data\backtests\queue.txt (one run per line: "<run id> <tools/backtest.py arguments>", # = comment); the
    claude answers and --max-calls 130 are added (D-053: production's 120 calls a day). A worker claims a run with data\backtests\<id>.claim (its process
    id) before it starts it; the other worker skips a run whose claim belongs to a live process. A run with a clean
    report.json is skipped; a run folder without a report whose claim is dead (the PC went off) is removed and run
    again; an interrupted run (the PC slept, failed calls) is kept aside once and run again. While
    data\backtests\PAUSE exists no new run starts. Log: data\backtests\queue.log. Runs from this checkout (its tools
    and src), reads the market of C:\TradingSystems\XAUUSD\data (read only) and writes only under data\backtests.
#>
$ErrorActionPreference = 'Continue'
$MaxWorkers = 2                                   # BT_MAX_WORKERS raises it (e.g. 4 while the live system is stopped)
if ($env:BT_MAX_WORKERS -match '^[1-6]$') { $MaxWorkers = [int]$env:BT_MAX_WORKERS }
$root = Split-Path -Parent $PSScriptRoot
$data = 'C:\TradingSystems\XAUUSD\data'
$bt = Join-Path $data 'backtests'
$py = 'C:\TradingSystems\XAUUSD\.venv\Scripts\python.exe'
$log = Join-Path $bt 'queue.log'
$others = @(Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" | Where-Object {
    $_.ProcessId -ne $PID -and $_.CommandLine -match 'backtest_queue\.ps1' })
if ($others.Count -ge $MaxWorkers) { Write-Host "already $($others.Count) queue workers - nothing done"; exit 1 }
$env:PYTHONPATH = Join-Path $root 'src'
# the live system comes first: this worker and the runs it starts (they inherit it) take spare CPU only
# (2026-10-08: the live executor's heartbeat stalled three times under two runs + the unit suite)
try { (Get-Process -Id $PID).PriorityClass = 'BelowNormal' } catch { }
Set-Location $root

function Online {
    # api.anthropic.com resolves and answers on 443 - else every call of a run would fail (2026-10-06: hours lost)
    try {
        $c = New-Object System.Net.Sockets.TcpClient
        $ok = $c.ConnectAsync('api.anthropic.com', 443).Wait(5000) -and $c.Connected
        $c.Close()
        return $ok
    } catch { return $false }
}

function Alive($claim) {
    if (-not (Test-Path $claim)) { return $false }
    $p = (Get-Content $claim -ErrorAction SilentlyContinue | Select-Object -First 1)
    return [bool]($p -and (Get-Process -Id ([int]$p) -ErrorAction SilentlyContinue))
}

"$(Get-Date -Format o) worker started (pid $PID)" | Out-File -Append $log -Encoding utf8
while ($true) {
    $lines = Get-Content (Join-Path $bt 'queue.txt') -ErrorAction SilentlyContinue |
        ForEach-Object { $_.TrimStart([char]0xFEFF) } | Where-Object { $_ -match '\S' -and $_ -notmatch '^\s*#' }
    $next = $null
    foreach ($l in $lines) {
        $id = ($l -split '\s+')[0]
        $dir = Join-Path $bt $id
        $rep = Join-Path $dir 'report.json'
        $claim = Join-Path $bt "$id.claim"
        if (Alive $claim) { continue }                                   # the other worker runs it
        if (Test-Path $rep) {
            $j = Get-Content $rep -Raw | ConvertFrom-Json
            $mark = Join-Path $bt "retried_$id"
            if (-not ($j.meta.result.interrupted -and -not (Test-Path $mark))) { continue }
        }
        try { New-Item -ItemType File $claim -ErrorAction Stop | Out-Null } catch {
            if (Alive $claim) { continue }                               # lost the race to the other worker
            Remove-Item -Force $claim -ErrorAction SilentlyContinue
            try { New-Item -ItemType File $claim -ErrorAction Stop | Out-Null } catch { continue }
        }
        Set-Content -Path $claim -Value $PID -Encoding ascii
        if (Test-Path $rep) {
            New-Item -ItemType File (Join-Path $bt "retried_$id") -Force | Out-Null
            Rename-Item $dir "$($id)_interrupted_$(Get-Date -Format yyyyMMddHHmmss)"
            "$(Get-Date -Format o) $id was interrupted - kept aside, running it again" | Out-File -Append $log -Encoding utf8
        } elseif (Test-Path $dir) {
            Remove-Item -Recurse -Force $dir
            "$(Get-Date -Format o) $id had no report (cut off) - removed, running it again" | Out-File -Append $log -Encoding utf8
        }
        $next = $l; break
    }
    if (-not $next) { "$(Get-Date -Format o) worker $PID - queue empty" | Out-File -Append $log -Encoding utf8; break }
    while (Test-Path (Join-Path $bt 'PAUSE')) { Start-Sleep -Seconds 30 }
    if (-not (Online)) {
        "$(Get-Date -Format o) worker $PID - no internet (api.anthropic.com:443) - waiting" | Out-File -Append $log -Encoding utf8
        while (-not (Online)) { Start-Sleep -Seconds 60 }
        "$(Get-Date -Format o) worker $PID - internet back" | Out-File -Append $log -Encoding utf8
    }
    $parts = $next -split '\s+'
    $id = $parts[0]; $rest = $parts[1..($parts.Length - 1)]
    "$(Get-Date -Format o) start $id (worker $PID)" | Out-File -Append $log -Encoding utf8
    & $py tools/backtest.py --answers claude --max-calls 130 --data $data --run-id $id @rest *>> (Join-Path $bt "$id.out")
    "$(Get-Date -Format o) end $id exit=$LASTEXITCODE (worker $PID)" | Out-File -Append $log -Encoding utf8
    Remove-Item -Force (Join-Path $bt "$id.claim") -ErrorAction SilentlyContinue
    $rep = Join-Path $dir 'report.json'
    if (Test-Path $rep) {
        $j = Get-Content $rep -Raw | ConvertFrom-Json
        if ($j.meta.result.stopped_by -eq 'ai errors') {        # its calls kept failing: no sample - run it again
            Remove-Item -Recurse -Force $dir -ErrorAction SilentlyContinue
            Remove-Item -Force (Join-Path $bt "retried_$id") -ErrorAction SilentlyContinue
            "$(Get-Date -Format o) $id stopped by failed calls (network/provider) - removed, it runs again" |
                Out-File -Append $log -Encoding utf8
            Start-Sleep -Seconds 60
            continue
        }
    }
    if ($next -notmatch '--keep-market') {               # safety net: the run's market copy once its process is gone
        foreach ($sub in 'hot', 'cold') { Remove-Item -Recurse -Force (Join-Path $dir "data\$sub") -ErrorAction SilentlyContinue }
    }
}
