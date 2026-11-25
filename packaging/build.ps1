<#
.SYNOPSIS
  Build the windowed mc-tracker.exe (PyInstaller one-folder) into D:\Maple-dist\mc-tracker\.

.EXAMPLE
  .\packaging\build.ps1                          # clean build venv in <repo>\.venv-build
  .\packaging\build.ps1 -VenvPath D:\Maple\.venv # reuse an existing venv (must have .[vision] installed)
#>
param(
    [string]$OutDir = "D:\Maple-dist",
    [string]$VenvPath = "",
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$PyInstallerVersion = "6.22.3"   # pinned

$Repo = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if (-not $VenvPath) { $VenvPath = Join-Path $Repo ".venv-build" }
$Py = Join-Path $VenvPath "Scripts\python.exe"

function Invoke-Checked {
    param([string]$Exe, [string[]]$Arguments)
    & $Exe @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Exe $($Arguments -join ' ') failed ($LASTEXITCODE)" }
}

if (-not (Test-Path $Py)) {
    Write-Host "Creating build venv at $VenvPath"
    Invoke-Checked $Python @("-m", "venv", $VenvPath)
    Invoke-Checked $Py @("-m", "pip", "install", "--upgrade", "pip")
    Invoke-Checked $Py @("-m", "pip", "install", "$Repo[vision]")
}
Invoke-Checked $Py @("-m", "pip", "install", "pyinstaller==$PyInstallerVersion")

# RapidOCR downloads its ONNX models into its package folder on first use; do that now so they get bundled.
Invoke-Checked $Py @("-c", "from rapidocr import RapidOCR; RapidOCR()")

New-Item -ItemType Directory -Force $OutDir | Out-Null
Invoke-Checked $Py @("-m", "PyInstaller", (Join-Path $Repo "packaging\mc-tracker.spec"),
    "--noconfirm", "--clean", "--distpath", $OutDir, "--workpath", (Join-Path $Repo "build"))

$Exe = Join-Path $OutDir "mc-tracker\mc-tracker.exe"
if (-not (Test-Path $Exe)) { throw "build finished but $Exe is missing" }
$MB = [math]::Round(((Get-ChildItem (Join-Path $OutDir "mc-tracker") -Recurse -File | Measure-Object Length -Sum).Sum / 1MB), 1)
Write-Host "Built $Exe ($MB MB). User data stays in D:\Maple\.mc-tracker (override: MC_TRACKER_HOME)."
