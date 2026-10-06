@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Start-Predictions.ps1"
set "predictionExitCode=%ERRORLEVEL%"
pause
exit /b %predictionExitCode%
