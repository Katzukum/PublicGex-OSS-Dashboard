<#
.SYNOPSIS
Compile the complete OpenGamma indicator class against an installed NinjaTrader.
.DESCRIPTION
Uses Roslyn from a local .NET SDK and the real NinjaTrader/SharpDX/.NET Framework
assemblies. The generated wrapper region is excluded: its companion partial
classes are supplied by NinjaTrader's full custom-project build. This verifies
the indicator implementation and API references, not a running chart or render.
Nothing is copied into or changed in the NinjaTrader installation/user folder.
The temporary workspace-local build directory is removed on success or failure.
#>
[CmdletBinding()]
param(
    [string]$NinjaTraderBin = (Join-Path $env:ProgramFiles 'NinjaTrader 8\bin'),
    [string]$CustomAssembly = (Join-Path ([Environment]::GetFolderPath('MyDocuments')) 'NinjaTrader 8\bin\Custom\NinjaTrader.Custom.dll')
)

$ErrorActionPreference = 'Stop'
$workspacePath = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$sourcePath = Join-Path $workspacePath 'OpenGamma.cs'
$dotnetPath = Join-Path $env:ProgramFiles 'dotnet\dotnet.exe'
if (-not (Test-Path -LiteralPath $dotnetPath)) {
    $dotnetCommand = Get-Command dotnet -ErrorAction Stop
    $dotnetPath = $dotnetCommand.Source
}
$sdkLines = @(& $dotnetPath --list-sdks)
if ($LASTEXITCODE -ne 0 -or $sdkLines.Count -eq 0) {
    throw 'A local .NET SDK with the Roslyn compiler is required.'
}
$compilerPath = $null
foreach ($sdkLine in $sdkLines) {
    if ($sdkLine -match '^([^ ]+) \[(.+)\]$') {
        $candidatePath = Join-Path (Join-Path $Matches[2] $Matches[1]) 'Roslyn\bincore\csc.dll'
        if (Test-Path -LiteralPath $candidatePath) { $compilerPath = $candidatePath }
    }
}
if (-not $compilerPath) { throw 'No Roslyn csc.dll was found in the installed SDKs.' }

$frameworkPath = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319'
if (-not (Test-Path -LiteralPath $frameworkPath)) {
    $frameworkPath = Join-Path $env:WINDIR 'Microsoft.NET\Framework\v4.0.30319'
}
$references = @(
    'mscorlib.dll', 'System.dll', 'System.Core.dll', 'System.Xml.dll',
    'System.ComponentModel.DataAnnotations.dll', 'System.Xaml.dll',
    'WPF\PresentationCore.dll', 'WPF\PresentationFramework.dll', 'WPF\WindowsBase.dll'
) | ForEach-Object { Join-Path $frameworkPath $_ }
$references += @(
    'NinjaTrader.Core.dll', 'NinjaTrader.Gui.dll', 'SharpDX.dll',
    'SharpDX.Direct2D1.dll', 'SharpDX.DXGI.dll', 'SharpDX.Direct3D10.dll'
) | ForEach-Object { Join-Path $NinjaTraderBin $_ }
$references += $CustomAssembly
foreach ($referencePath in $references) {
    if (-not (Test-Path -LiteralPath $referencePath)) {
        throw "Required installed assembly is missing: $referencePath"
    }
}

$buildPath = [IO.Path]::GetFullPath((Join-Path $workspacePath ('.prediction-indicator-check-' + [Guid]::NewGuid().ToString('N'))))
New-Item -ItemType Directory -Path $buildPath | Out-Null
try {
    $source = [IO.File]::ReadAllText($sourcePath)
    $marker = '#region NinjaScript generated code. Neither change nor remove.'
    $markerIndex = $source.IndexOf($marker, [StringComparison]::Ordinal)
    if ($markerIndex -lt 0) { throw 'Could not identify the NinjaScript generated-wrapper boundary.' }
    $validationPath = Join-Path $buildPath 'OpenGamma.Validation.cs'
    [IO.File]::WriteAllText($validationPath, $source.Substring(0, $markerIndex), (New-Object Text.UTF8Encoding($false)))
    $compilerArgs = @(
        '/nologo', '/target:library', '/nostdlib+', '/langversion:latest', '/nowarn:0436',
        ('/out:"' + (Join-Path $buildPath 'OpenGamma.Validation.dll') + '"')
    )
    $compilerArgs += $references | ForEach-Object { '/reference:"' + $_ + '"' }
    $compilerArgs += '"' + $validationPath + '"'
    $responsePath = Join-Path $buildPath 'compile.rsp'
    [IO.File]::WriteAllLines($responsePath, $compilerArgs)
    & $dotnetPath $compilerPath ('@' + $responsePath)
    if ($LASTEXITCODE -ne 0) { throw "OpenGamma compile validation failed with exit code $LASTEXITCODE." }
    Write-Output 'PASS: complete OpenGamma indicator implementation compiles against installed NinjaTrader and SharpDX assemblies.'
    Write-Output 'Scope: generated companion wrappers and actual live-chart rendering still require NinjaTrader validation.'
}
finally {
    $expectedParent = $workspacePath.TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar
    $resolvedBuildPath = [IO.Path]::GetFullPath($buildPath)
    if (-not $resolvedBuildPath.StartsWith($expectedParent, [StringComparison]::OrdinalIgnoreCase) -or
        -not ([IO.Path]::GetFileName($resolvedBuildPath).StartsWith('.prediction-indicator-check-'))) {
        throw 'Refusing to remove a build directory outside the intended workspace.'
    }
    if (Test-Path -LiteralPath $resolvedBuildPath) {
        Remove-Item -LiteralPath $resolvedBuildPath -Recurse -Force
    }
}
