@echo off
setlocal
cd /d "%~dp0"
set "PUBLICGEX_DEMO=1"
if exist "src-tauri\target\release\publicgex-dashboard.exe" (
    start "PublicGex Demo" "src-tauri\target\release\publicgex-dashboard.exe"
) else (
    powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\native-run.ps1" -Action dev -Demo
)
