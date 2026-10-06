param(
    [ValidateRange(1, 65535)][int]$Port = 5011,
    [switch]$Report
)
$ErrorActionPreference = "Stop"
$predictionRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$predictionPython = Join-Path $predictionRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $predictionPython)) {
    $predictionPython = "python"
}
Push-Location -LiteralPath $predictionRoot
$predictionExitCode = 0
try {
    $predictionArgs = @("-m", "prediction.service", "--port", $Port)
    if ($Report) { $predictionArgs += "--report" }
    & $predictionPython @predictionArgs
    $predictionExitCode = $LASTEXITCODE
    if ($predictionExitCode -ne 0) {
        Write-Host "Forecast service exited with code $predictionExitCode. See the message above for the cause." -ForegroundColor Red
    }
} finally {
    Pop-Location
}
exit $predictionExitCode
