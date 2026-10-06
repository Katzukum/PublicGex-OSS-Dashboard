@echo off
setlocal
cd /d "%~dp0"
set "PUBLICGEX_DEMO=0"
if exist "src-tauri\target\release\publicgex-dashboard.exe" (
    start "PublicGex Dashboard" "src-tauri\target\release\publicgex-dashboard.exe"
) else (
    powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\native-run.ps1" -Action dev
)
