@echo off
rem D-051: the FIRST start of this pair folder - once, when this pair moves out of the old three-pair checkout:
rem   scripts\first_start.bat C:\the_claude_new
rem 1. checks that this folder runs nothing yet (tools\import_from_old.py --preflight)
rem 2. stops ONLY this pair's system in the old checkout (with the old checkout's own stop, run from there - never
rem    stop_all: the old code finds supervisors by command line on the whole machine and would also stop a pair folder
rem    that already switched), removes the old keep-alive task of this pair ("\TradingSystem-<PAIR>") and the old
rem    operator tasks ("\TradingSystemOps-*": monitor, reviews, backup, recorder keep-alive), and tells the old price
rem    recorder to stop (its STOP file). The first pair folder to switch removes the old operator tasks; the others
rem    find nothing left. The old "\TradingSystem-MT5" task is kept: deleting it may close the running terminal (it
rem    only starts MT5 at logon - remove it later with MT5 closed: schtasks /Delete /TN "\TradingSystem-MT5" /F).
rem 3. imports this pair's data (tools\import_from_old.py: copies and hard links - the old checkout is only read)
rem 4. registers this folder's own tasks in its own Task Scheduler folder (\TradingSystems\<PAIR>\): autostart and
rem    the operator tasks (monitor, reviews, backup, recorder keep-alive)
rem 5. starts this folder's system and its price recorder
rem Another pair's folder and its tasks are never touched. Run it once per pair folder, in any order.
setlocal
cd /d "%~dp0.."
set "PY=.venv\Scripts\python.exe"
set "OLD=%~1"
set "RC=0"
if "%OLD%"=="" (
    echo usage: scripts\first_start.bat C:\the_claude_new   ^(the old three-pair checkout^)
    set "RC=2"
    goto end
)
if not exist "%PY%" (
    echo Python venv not found: %CD%\%PY%
    set "RC=1"
    goto end
)
if not exist "%OLD%\.venv\Scripts\python.exe" (
    echo The old checkout has no venv: %OLD%\.venv\Scripts\python.exe
    set "RC=2"
    goto end
)
set "PAIR="
for /f "usebackq delims=" %%P in (`%PY% -m tradingsystem config --instances`) do set "PAIR=%%P"
if "%PAIR%"=="" (
    echo Could not read this folder's pair from config\config.yaml ^(the error is above^).
    set "RC=1"
    goto end
)
echo ===== 1/5 %PAIR%: this folder runs nothing yet
"%PY%" tools\import_from_old.py --from "%OLD%" --preflight
if errorlevel 1 (
    set "RC=2"
    goto end
)
echo ===== 2/5 %PAIR%: stop this pair in the old checkout %OLD%
pushd "%OLD%"
".venv\Scripts\python.exe" -m tradingsystem run --stop --instance %PAIR%
popd
schtasks /Query /TN "\TradingSystem-%PAIR%" >nul 2>nul && schtasks /Delete /TN "\TradingSystem-%PAIR%" /F
for %%T in (Monitor ReviewDaily ReviewWeekly Backup Recorder) do (
    schtasks /Query /TN "\TradingSystemOps-%%T" >nul 2>nul && schtasks /Delete /TN "\TradingSystemOps-%%T" /F
)
if exist "%OLD%\data\research\price_matching\" if not exist "%OLD%\data\research\price_matching\STOP" (
    type nul > "%OLD%\data\research\price_matching\STOP"
    echo old price recorder: STOP file written ^(it stops at its next flush^)
)
echo ===== 3/5 %PAIR%: import this pair's data
"%PY%" tools\import_from_old.py --from "%OLD%"
if errorlevel 1 (
    echo The import did not finish - nothing was started. Fix what it says and run this again.
    set "RC=1"
    goto end
)
echo ===== 4/5 %PAIR%: this folder's tasks
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_autostart.ps1" -AllPairs
if errorlevel 1 set "RC=1"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_operator_tasks.ps1"
if errorlevel 1 set "RC=1"
echo ===== 5/5 %PAIR%: start
call "%~dp0start_all.bat" /nopause
if errorlevel 1 set "RC=1"
call "%~dp0start_recorder.bat" /nopause
echo.
echo %PAIR% runs from %CD%. Check: scripts\status_all.bat and scripts\check_ops.bat
:end
if /i not "%~2"=="/nopause" pause
exit /b %RC%
