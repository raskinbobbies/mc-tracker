<#
.SYNOPSIS
  Swap the staged build (<InstallDir>.next) into the fixed install folder, once the installed exe is free.

.DESCRIPTION
  The login entry always names <InstallDir>\mc-tracker.exe, so every new build has to land in that one
  folder. A running exe can't be replaced, so build.ps1 stages the new build next to it and runs this:
  if the installed exe isn't running, the folders are swapped now; otherwise the build stays staged and
  the running install starts this script again (from a temp copy) when it is free to go: the waiter when
  the game and the app are both closed, the app when it is started.

  -BuildDir first copies a fresh build there as the staged one (replacing an older staged build).

  Exit codes: 0 installed (or nothing staged), 3 still in use after -WaitSeconds (left staged).

.EXAMPLE
  .\packaging\install.ps1 -BuildDir D:\Maple-dist\mc-tracker -InstallDir D:\Maple-app\mc-tracker
  .\install.ps1 -InstallDir D:\Maple-app\mc-tracker -WaitSeconds 60 -Start -Arguments "--follow-game"
#>
param(
    [Parameter(Mandatory = $true)][string]$InstallDir,
    [string]$BuildDir = "",
    [int]$WaitSeconds = 0,
    [switch]$Start,           # start the installed exe afterwards (also when nothing was staged)
    [string]$Arguments = ""
)

$ErrorActionPreference = "Stop"
$ExeName = "mc-tracker.exe"
$InstallDir = [IO.Path]::GetFullPath($InstallDir).TrimEnd('\')
$Next = "$InstallDir.next"
$Leaf = Split-Path $InstallDir -Leaf

function Test-InUse([string]$Dir) {
    # A running exe can't be opened for exclusive write. (Renaming its folder would still work, and pull
    # the files out from under the running app.)
    $exe = Join-Path $Dir $ExeName
    if (-not (Test-Path -LiteralPath $exe)) { return $false }
    try { [IO.File]::Open($exe, 'Open', 'ReadWrite', 'None').Close(); return $false } catch { return $true }
}

function Start-Installed {
    if (-not $Start) { return }
    $exe = Join-Path $InstallDir $ExeName
    if (-not (Test-Path -LiteralPath $exe)) { return }
    if ($Arguments.Trim()) { Start-Process -FilePath $exe -ArgumentList $Arguments } else { Start-Process -FilePath $exe }
}

if ($BuildDir) {
    if (-not (Test-Path -LiteralPath (Join-Path $BuildDir $ExeName))) { throw "no $ExeName in $BuildDir" }
    New-Item -ItemType Directory -Force (Split-Path $InstallDir -Parent) | Out-Null
    # Copied under another name first: a running waiter swaps in whatever <InstallDir>.next holds.
    $Copying = "$InstallDir.copying"
    foreach ($d in @($Copying, $Next)) { if (Test-Path -LiteralPath $d) { Remove-Item -LiteralPath $d -Recurse -Force } }
    Copy-Item -LiteralPath $BuildDir -Destination $Copying -Recurse
    Rename-Item -LiteralPath $Copying -NewName (Split-Path $Next -Leaf)
    Write-Host "Staged $Next"
}

if (-not (Test-Path -LiteralPath (Join-Path $Next $ExeName))) {
    Write-Host "Nothing staged at $Next"
    Start-Installed
    exit 0
}

$deadline = (Get-Date).AddSeconds($WaitSeconds)
while (Test-InUse $InstallDir) {
    if ((Get-Date) -ge $deadline) {
        Write-Host "$InstallDir is running: the new build stays staged at $Next and swaps in when mc-tracker next starts or the game closes."
        exit 3
    }
    Start-Sleep -Milliseconds 500
}

$Old = "$InstallDir.old-" + (Get-Date -Format "yyyyMMddHHmmss")
if (Test-Path -LiteralPath $InstallDir) { Rename-Item -LiteralPath $InstallDir -NewName (Split-Path $Old -Leaf) }
try {
    Rename-Item -LiteralPath $Next -NewName $Leaf
} catch {
    if (Test-Path -LiteralPath $Old) { Rename-Item -LiteralPath $Old -NewName $Leaf }  # put the old build back
    throw
}
# Old builds, this one's and any a previous swap couldn't delete; one that is somehow still in use stays.
Get-ChildItem -LiteralPath (Split-Path $InstallDir -Parent) -Directory -Filter "$Leaf.old-*" | ForEach-Object {
    Remove-Item -LiteralPath $_.FullName -Recurse -Force -ErrorAction SilentlyContinue
}
Write-Host "Installed $InstallDir\$ExeName"
Start-Installed
exit 0
