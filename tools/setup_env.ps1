<#
.SYNOPSIS
    Prepare the envimcp virtual environment on this machine.

.DESCRIPTION
    Creates .venv (or reuses it), installs the package in editable mode plus the
    dev extra, and reports the result of `envimcp doctor`. Two environment facts
    drive the choices here:

      * `python` and `python3` do not resolve in this shell -- the interpreter is
        D:\python312\python.exe.
      * pypi.org is unreachable, so packages come from the Tsinghua mirror.

.PARAMETER Geo
    Also install the optional [geo] extra (GDAL/Rasterio). Only needed for
    Cloud-Optimized GeoTIFF export; ENVI 5.2 cannot write COG itself.

.EXAMPLE
    pwsh -File tools/setup_env.ps1
#>
[CmdletBinding()]
param(
    [switch]$Geo,
    [string]$Python = 'D:\python312\python.exe',
    [string]$Mirror = 'https://pypi.tuna.tsinghua.edu.cn/simple'
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$venv = Join-Path $root '.venv'
$py = Join-Path $venv 'Scripts\python.exe'

Write-Host "envimcp setup" -ForegroundColor Cyan
Write-Host "  root   : $root"
Write-Host "  venv   : $venv"
Write-Host "  python : $Python"

if (-not (Test-Path -LiteralPath $Python)) {
    throw "interpreter not found: $Python (pass -Python <path>)"
}

if (-not (Test-Path -LiteralPath $py)) {
    Write-Host "creating the virtual environment..." -ForegroundColor Yellow
    & $Python -m venv $venv
    if ($LASTEXITCODE -ne 0) { throw "venv creation failed" }
} else {
    Write-Host "reusing the existing virtual environment"
}

Write-Host "installing (editable, dev extra) from $Mirror" -ForegroundColor Yellow
& $py -m pip install -q --upgrade pip -i $Mirror
$spec = if ($Geo) { "$root[dev,geo]" } else { "$root[dev]" }
& $py -m pip install -q -e $spec -i $Mirror
if ($LASTEXITCODE -ne 0) { throw "install failed" }

Write-Host "versions:" -ForegroundColor Cyan
& $py -c "import sys, mcp, pytest; print('  python ', sys.version.split()[0]); print('  mcp    ', getattr(mcp, '__version__', '2.x')); print('  pytest ', pytest.__version__)"

Write-Host "`ndoctor:" -ForegroundColor Cyan
& $py -m envimcp.cli --config (Join-Path $root 'config\envimcp.toml') doctor
exit $LASTEXITCODE
