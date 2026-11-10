param(
    [switch]$Demo,
    [switch]$CheckOnly
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$releaseRoot = Join-Path $projectRoot 'src-tauri\target\release'
$appPath = Join-Path $releaseRoot 'publicgex-dashboard.exe'
$sidecarPath = Join-Path $releaseRoot 'publicgex-backend.exe'

# Check the packaged files, not dist/: a frontend build alone does not update Tauri.
foreach ($artifact in @($appPath, $sidecarPath)) {
    if (-not (Test-Path -LiteralPath $artifact -PathType Leaf)) {
        Write-Warning "Desktop build is missing: $artifact"
        Write-Host 'Run npm run native:build from this project, then launch again.'
        exit 1
    }
}

$appInputs = @('src', 'public', 'src-tauri\src', 'src-tauri\capabilities', 'src-tauri\icons',
    'src-tauri\Cargo.toml', 'src-tauri\Cargo.lock', 'src-tauri\build.rs',
    'src-tauri\tauri.conf.json', 'index.html', 'package.json', 'package-lock.json',
    'tsconfig.json', 'vite.config.ts', 'scripts\native-run.ps1')
$backendInputs = @('backend', 'scripts\build-sidecar.py')
$changed = @()
foreach ($group in @(
    @{ Artifact = $appPath; Inputs = $appInputs },
    @{ Artifact = $sidecarPath; Inputs = $backendInputs }
)) {
    $builtAt = (Get-Item -LiteralPath $group.Artifact).LastWriteTimeUtc
    foreach ($relativePath in $group.Inputs) {
        $inputPath = Join-Path $projectRoot $relativePath
        if (-not (Test-Path -LiteralPath $inputPath)) { continue }
        $changed += Get-ChildItem -LiteralPath $inputPath -File -Recurse |
            Where-Object {
                $_.FullName -notmatch '[\\/](tests|__pycache__|\.pytest_cache)[\\/]' -and
                $_.Extension -ne '.pyc' -and $_.LastWriteTimeUtc -gt $builtAt
            }
    }
}

if ($changed.Count -gt 0) {
    Write-Warning 'The desktop build may be out of date. Project files are newer than the packaged app or backend.'
    $changed | Select-Object -First 5 | ForEach-Object { Write-Host "  $($_.FullName)" }
    Write-Host 'To update it, close the app and run: npm run native:build'
    if ($CheckOnly) { exit 2 }
    $answer = Read-Host 'Launch the existing build anyway? [y/N]'
    if ($answer -notmatch '^(y|yes)$') { exit 0 }
} else {
    Write-Host 'Desktop build check passed: no newer source files found.'
}

if ($CheckOnly) { exit 0 }
$env:PUBLICGEX_DEMO = if ($Demo) { '1' } else { '0' }
Start-Process -FilePath $appPath -WorkingDirectory $projectRoot
