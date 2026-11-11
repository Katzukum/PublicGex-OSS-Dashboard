param(
    [ValidateSet('dev', 'build', 'check', 'test')][string]$Action = 'dev',
    [switch]$Demo,
    [switch]$SkipSidecar
)
$ErrorActionPreference = 'Stop'
$nativeRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $nativeRoot
$env:CARGO_HOME = Join-Path $nativeRoot '.tools\cargo'
$env:RUSTUP_HOME = Join-Path $nativeRoot '.tools\rustup'
$nativeCargo = Join-Path $env:CARGO_HOME 'bin\cargo.exe'
if (-not (Test-Path -LiteralPath $nativeCargo)) {
    & (Join-Path $PSScriptRoot 'native-setup.ps1')
}
$env:PATH = (Join-Path $env:CARGO_HOME 'bin') + [IO.Path]::PathSeparator + $env:PATH
$env:PUBLICGEX_NATIVE = '1'
$env:PUBLICGEX_DEMO = if ($Demo) { '1' } else { '0' }

# Import the already installed MSVC environment into this process only.
$vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
$vsPath = $null
if (Test-Path -LiteralPath $vswhere) {
    $vsPath = & $vswhere -latest -products '*' -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
}
# Existing Build Tools can work even when absent from vswhere's inventory.
if (-not $vsPath) {
    foreach ($base in @($env:ProgramFiles, ${env:ProgramFiles(x86)})) {
        if (-not $base) { continue }
        foreach ($year in @('2022', '2019', '2017')) {
            foreach ($edition in @('BuildTools', 'Community', 'Professional', 'Enterprise')) {
                $candidate = Join-Path $base "Microsoft Visual Studio\$year\$edition"
                $devCmd = Join-Path $candidate 'Common7\Tools\VsDevCmd.bat'
                $toolsDir = Join-Path $candidate 'VC\Tools\MSVC'
                if ((Test-Path -LiteralPath $devCmd) -and (Test-Path -LiteralPath $toolsDir)) {
                    $compiler = Get-ChildItem -LiteralPath $toolsDir -Directory -ErrorAction SilentlyContinue |
                        Where-Object { Test-Path -LiteralPath (Join-Path $_.FullName 'bin\Hostx64\x64\cl.exe') } |
                        Select-Object -First 1
                    if ($compiler) { $vsPath = $candidate; break }
                }
            }
            if ($vsPath) { break }
        }
        if ($vsPath) { break }
    }
}
if ($vsPath) {
    $vsDevCmd = Join-Path $vsPath 'Common7\Tools\VsDevCmd.bat'
    $nativeCommand = '"' + $vsDevCmd + '" -arch=x64 -host_arch=x64 >nul && set'
    & $env:COMSPEC /d /s /c $nativeCommand | ForEach-Object {
        if ($_ -match '^([^=]+)=(.*)$') { [Environment]::SetEnvironmentVariable($matches[1], $matches[2], 'Process') }
    }
}
if ($Action -eq 'check' -or $Action -eq 'test') {
    # Source builds do not require the packaged PyInstaller executable.
    $env:TAURI_CONFIG = '{"bundle":{"externalBin":[]}}'
    & $nativeCargo $Action --manifest-path (Join-Path $nativeRoot 'src-tauri\Cargo.toml')
} elseif ($Action -eq 'build') {
    $python = Join-Path $nativeRoot '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $python)) { throw 'Create the project .venv and install backend dependencies first.' }
    if (-not $SkipSidecar) {
        & $python (Join-Path $PSScriptRoot 'build-sidecar.py')
        if ($LASTEXITCODE -ne 0) { throw 'The analytics sidecar build failed.' }
    } elseif (-not (Test-Path -LiteralPath (Join-Path $nativeRoot 'src-tauri\binaries\publicgex-backend-x86_64-pc-windows-msvc.exe'))) {
        throw 'Build the analytics sidecar before using -SkipSidecar.'
    }
    & npx.cmd tauri build
} else {
    & npx.cmd tauri dev --config src-tauri/tauri.dev.conf.json
}
exit $LASTEXITCODE
