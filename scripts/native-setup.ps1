$ErrorActionPreference = 'Stop'
$nativeRoot = Split-Path -Parent $PSScriptRoot
$nativeTools = Join-Path $nativeRoot '.tools'
$env:CARGO_HOME = Join-Path $nativeTools 'cargo'
$env:RUSTUP_HOME = Join-Path $nativeTools 'rustup'
$nativeCargo = Join-Path $env:CARGO_HOME 'bin\cargo.exe'
if (-not (Test-Path -LiteralPath $nativeCargo)) {
    New-Item -ItemType Directory -Force -Path $nativeTools | Out-Null
    $installer = Join-Path $nativeTools 'rustup-init.exe'
    Invoke-WebRequest -Uri 'https://static.rust-lang.org/rustup/dist/x86_64-pc-windows-msvc/rustup-init.exe' -OutFile $installer
    & $installer -y --no-modify-path --profile minimal --default-toolchain stable --default-host x86_64-pc-windows-msvc
    if ($LASTEXITCODE -ne 0) { throw 'The project-local Rust installation failed.' }
}
Write-Host 'Rust is available in this project .tools folder. No global PATH changes were made.'
& $nativeCargo --version
