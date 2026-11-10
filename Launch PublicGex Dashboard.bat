@echo off
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\launch-desktop.ps1"
if errorlevel 1 (
    echo PublicGex Dashboard could not be launched. See the message above.
    pause
    exit /b 1
)
