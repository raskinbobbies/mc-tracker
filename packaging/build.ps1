<#
.SYNOPSIS
  Build the windowed mc-tracker.exe (PyInstaller one-folder) into D:\Maple-dist\mc-tracker\, then install it
  into the fixed folder D:\Maple-app\mc-tracker\ (the one the "Start and stop with MapleStory" login entry names).

.DESCRIPTION
  The build is stamped (build.txt next to the exe: UTC time and commit), then install.ps1 stages it as
  <InstallDir>.next and swaps it in now if the installed exe isn't running; if it is, the running install
  swaps it in by itself when it next starts or the game closes. -InstallDir "" builds without installing.

.EXAMPLE
  .\packaging\build.ps1                          # clean build venv in <repo>\.venv-build
  .\packaging\build.ps1 -VenvPath D:\Maple\.venv # reuse an existing venv (must have .[vision] installed)
#>
param(
    [string]$OutDir = "D:\Maple-dist",
    [string]$InstallDir = "D:\Maple-app\mc-tracker",
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
}
# Every build: pick up new or changed dependencies (a no-op when nothing changed). Only the dependencies from
# pyproject.toml, not the project itself: PyInstaller reads src\ directly, and a reused venv may hold an
# editable install that a copy would shadow.
$deps = & $Py -c "import sys, tomllib; p = tomllib.load(open(sys.argv[1], 'rb'))['project']; print(*(p['dependencies'] + p['optional-dependencies']['vision']), sep=chr(10))" (Join-Path $Repo "pyproject.toml")
if ($LASTEXITCODE -ne 0) { throw "could not read dependencies from pyproject.toml" }
Invoke-Checked $Py (@("-m", "pip", "install") + @($deps | Where-Object { $_ }))
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

# The stamp lets a newer build take the login entry over from an older build folder (follow.check_login_entry).
$Commit = (& git -C $Repo rev-parse --short HEAD 2>$null)
if ($LASTEXITCODE -ne 0 -or -not $Commit) { $Commit = "unknown" }
$Stamp = "$((Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')) $Commit"
Set-Content -LiteralPath (Join-Path $OutDir "mc-trackeruild.txt") -Value $Stamp -Encoding ascii

if ($InstallDir) {
    & (Join-Path $PSScriptRoot "install.ps1") -BuildDir (Join-Path $OutDir "mc-tracker") -InstallDir $InstallDir
    if ($LASTEXITCODE -ne 0 -and $LASTEXITCODE -ne 3) { throw "install failed ($LASTEXITCODE)" }  # 3: staged
}
