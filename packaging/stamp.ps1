<#
.SYNOPSIS
  Write build.txt next to a built mc-tracker.exe: UTC build time, then the commit. A newer stamp lets a build take
  the login entry over from an older one (follow.check_login_entry).
#>
param(
    [Parameter(Mandatory = $true)][string]$BuildDir,
    [string]$Repo = ""  # default: the checkout this script is in
)

$ErrorActionPreference = "Stop"
if (-not $Repo) { $Repo = Split-Path $PSScriptRoot -Parent }
if (-not (Test-Path -LiteralPath (Join-Path $BuildDir "mc-tracker.exe"))) { throw "no mc-tracker.exe in $BuildDir" }
$Commit = "unknown"
try {
    $out = & git -C $Repo rev-parse --short HEAD 2>$null
    if ($LASTEXITCODE -eq 0 -and $out) { $Commit = "$out".Trim() }
} catch { }
$Stamp = "$((Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')) $Commit"
Set-Content -LiteralPath (Join-Path $BuildDir "build.txt") -Value $Stamp -Encoding ascii
Write-Host "Stamped $Stamp"
