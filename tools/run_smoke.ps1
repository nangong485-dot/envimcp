<#
.SYNOPSIS
    Run envimcp's end-to-end checks: the CLI smoke, then the pytest suite.

.DESCRIPTION
    Three levels, each opt-in so the script is useful on a slow machine:

      * always          - `envimcp smoke`, which asserts the NDVI statistics
                          against the measured golden baseline within 1e-6
      * -Fast           - the 48 pure-Python tests (no IDL, ~1 s)
      * -Acceptance     - the 4 tests that really launch IDL/ENVI (~20 s)
      * -Transports     - a real MCP handshake over stdio and streamable HTTP,
                          starting and stopping a server on -Port

    The slow and transport stages need this machine's ENVI 5.2 / IDL 8.4 and
    consume the single ENVI licence while they run.

.EXAMPLE
    pwsh -File tools/run_smoke.ps1
.EXAMPLE
    pwsh -File tools/run_smoke.ps1 -Fast -Acceptance -Transports
#>
[CmdletBinding()]
param(
    [switch]$Fast,
    [switch]$Acceptance,
    [switch]$Transports,
    [int]$Port = 8765
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$py = Join-Path $root '.venv\Scripts\python.exe'
$cfg = Join-Path $root 'config\envimcp.toml'
$env:PYTHONIOENCODING = 'utf-8'

if (-not (Test-Path -LiteralPath $py)) {
    throw "no virtual environment; run tools/setup_env.ps1 first"
}

$failures = @()

function Invoke-Stage {
    param([string]$Name, [scriptblock]$Body)
    Write-Host "`n=== $Name ===" -ForegroundColor Cyan
    & $Body
    if ($LASTEXITCODE -ne 0) {
        Write-Host "$Name FAILED (exit $LASTEXITCODE)" -ForegroundColor Red
        $script:failures += $Name
    }
}

Invoke-Stage 'envimcp smoke (golden NDVI baseline)' {
    & $py -m envimcp.cli --config $cfg smoke
}

if ($Fast) {
    Invoke-Stage 'pytest (fast: no IDL)' {
        Push-Location $root
        try { & $py -m pytest tests -q -m 'not slow' } finally { Pop-Location }
    }
}

if ($Acceptance) {
    Invoke-Stage 'pytest (acceptance: launches IDL/ENVI)' {
        Push-Location $root
        try { & $py -m pytest tests -q -m slow } finally { Pop-Location }
    }
}

if ($Transports) {
    Invoke-Stage 'MCP transports (stdio + streamable HTTP)' {
        & $py (Join-Path $PSScriptRoot 'smoke_transport.py')
    }
}

Write-Host ''
if ($failures.Count -eq 0) {
    Write-Host 'all requested stages passed' -ForegroundColor Green
    exit 0
}
Write-Host ("failed stage(s): " + ($failures -join ', ')) -ForegroundColor Red
exit 1
