param(
    [string]$NinjaTraderDocuments = (Join-Path ([Environment]::GetFolderPath('MyDocuments')) 'NinjaTrader 8')
)
$ErrorActionPreference = "Stop"
$predictionRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$predictionSource = Join-Path $predictionRoot 'OpenGamma.cs'
$predictionTargetDirectory = Join-Path $NinjaTraderDocuments 'bin\Custom\Indicators'
if (-not (Test-Path -LiteralPath $predictionTargetDirectory -PathType Container)) {
    throw "NinjaTrader Indicators folder not found: $predictionTargetDirectory"
}
$predictionTarget = Join-Path $predictionTargetDirectory 'OpenGamma.cs'
if (Test-Path -LiteralPath $predictionTarget) {
    if ((Get-FileHash -LiteralPath $predictionSource).Hash -eq (Get-FileHash -LiteralPath $predictionTarget).Hash) {
        Write-Host 'The installed OpenGamma source is already current. Compile it in NinjaScript Editor with F5.'
        exit 0
    }
    # Keep backups outside bin/Custom so NinjaTrader will not compile duplicates.
    $predictionBackupDirectory = Join-Path $NinjaTraderDocuments 'OpenGammaBackups'
    New-Item -ItemType Directory -Path $predictionBackupDirectory -Force | Out-Null
    $predictionBackup = Join-Path $predictionBackupDirectory ("OpenGamma-{0}-{1}.cs.bak" -f (Get-Date -Format 'yyyyMMdd-HHmmss'), ([Guid]::NewGuid().ToString('N').Substring(0, 8)))
    Copy-Item -LiteralPath $predictionTarget -Destination $predictionBackup
    Write-Host "Previous indicator saved to: $predictionBackup"
}
Copy-Item -LiteralPath $predictionSource -Destination $predictionTarget
Write-Host 'Updated OpenGamma source installed. In NinjaScript Editor press F5, then remove and re-add the indicator on your chart.'
Write-Host 'For predictions: keep your existing merge policy, use a chart Trading Hours template that includes the hours you want, and start Start-Predictions.ps1. Provisional bands need six consecutive completed five-minute bars at any hour; there is no minimum day count.'
