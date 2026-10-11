@echo off
rem Stop the manual-scalping brief loop (D-058): creates data\scalp\STOP; the loop reads it every 15 s and ends.
rem A brief already running finishes first (at most scalp.run_timeout_s). The status printed after 20 s shows
rem "loop_running": false once the loop is gone. Start again with scripts\start_scalp.bat (it removes STOP).
setlocal
cd /d "%~dp0.."
set "PY=.venv\Scripts\python.exe"
if not exist "data\scalp" mkdir "data\scalp"
echo stop> "data\scalp\STOP"
echo STOP written - waiting 20 s for the loop to end...
timeout /t 20 /nobreak >nul
if exist "%PY%" "%PY%" tools\scalp_brief.py --status
if /i not "%~1"=="/nopause" pause
