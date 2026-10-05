@echo off
rem Runs the nitrogen gauge monitor and restarts it if it stops.
rem To start it at logon: Win+R, shell:startup, and put a shortcut to this file there.
cd /d "%~dp0"

where py >nul 2>nul
if %errorlevel%==0 (set PY=py) else (set PY=python)

:loop
%PY% gauge_monitor.py run
echo.
echo Monitor stopped - restarting in 30 s. Close this window to stop it for good.
timeout /t 30
goto loop
