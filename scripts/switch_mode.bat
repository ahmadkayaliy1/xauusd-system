@echo off
rem Switch the XAUUSD system between the DEMO and the REAL account on the SAME MetaTrader terminal (D-064).
rem   switch_mode.bat live            (log the terminal into the real account first; the tool asks you to type LIVE)
rem   switch_mode.bat demo
rem   switch_mode.bat live --adopt-server   (the first time: takes the terminal's real server name into config.local)
rem Refuses while a desk position / pending order or a scalp auto-trade is open, or while the terminal is on the
rem wrong account. Then stops the system and the scalp loop, writes the mode, starts both, confirms, notifies.
setlocal
cd /d "%~dp0.."
set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo Python venv not found: %CD%\%PY%
    exit /b 1
)
"%PY%" tools\switch_mode.py %*
set "RC=%ERRORLEVEL%"
if "%RC%"=="0" echo done.
if "%RC%"=="2" echo refused - nothing changed.
if "%RC%"=="1" echo FAILED - check the output above and scripts\status_all.bat.
exit /b %RC%
