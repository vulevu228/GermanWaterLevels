@echo off
REM Daily PEGELONLINE pull. Registered in Windows Task Scheduler as
REM "GermanWaterLevels-daily" (see README, "Taeglich laufen lassen").
REM Runs mirror + build and appends stdout/stderr to logs\daily.log.

cd /d "%~dp0"
if not exist logs mkdir logs
set PYTHONUTF8=1

echo. >> logs\daily.log
echo ==================== %DATE% %TIME% ==================== >> logs\daily.log
"C:\Users\emira\AppData\Local\Python\pythoncore-3.14-64\python.exe" fetch_pegelonline.py >> logs\daily.log 2>&1
