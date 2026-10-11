@echo off
rem Start the hourly manual-scalping brief loop (D-058), hidden (no window): tools\scalp_brief.py --start-loop.
rem The loop runs one brief at every hour of scalp.hours_utc (config) while scalp.enabled is true; a brief by hand
rem (tools\scalp_brief.py --once) or from the dashboard's Scalp tab runs at any time. Stop it with
rem scripts\stop_scalp.bat (the STOP file). Never two loops: the start is refused while one runs (it prints its pid).
rem No Task Scheduler: after a reboot run this again. Separate from the desk: start_all.bat / stop_all.bat do not
rem touch it, and it never places an order.
setlocal
cd /d "%~dp0.."
set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo Python venv not found: %CD%\%PY%
    goto end
)
del /q "data\scalp\STOP" 2>nul
"%PY%" tools\scalp_brief.py --start-loop
:end
if /i not "%~1"=="/nopause" pause
