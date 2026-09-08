@echo off
setlocal
title OpenGamma

pushd "%~dp0"
if errorlevel 1 (
    echo Could not open the OpenGamma project folder.
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo OpenGamma's Python environment was not found.
    echo Expected: %CD%\.venv\Scripts\python.exe
    echo Set up the project environment before launching the app.
    pause
    popd
    exit /b 1
)

echo Starting OpenGamma and its market data collector...
echo Keep this window open while using the app.
".venv\Scripts\python.exe" -u app.py
set "opengamma_exit_code=%errorlevel%"

if not "%opengamma_exit_code%"=="0" (
    echo.
    echo OpenGamma stopped with error code %opengamma_exit_code%.
    echo Review the messages above for details.
    pause
)

popd
exit /b %opengamma_exit_code%
