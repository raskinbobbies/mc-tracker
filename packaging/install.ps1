<#
.SYNOPSIS
  Swap the staged build (<InstallDir>.next) into the fixed install folder, once the installed exe is free.

.DESCRIPTION
  The login entry always names <InstallDir>\mc-tracker.exe, so every new build has to land in that one
  folder. A running exe can't be replaced, so build.ps1 stages the new build next to it and runs this:
  if the installed exe isn't running, the folders are swapped now; otherwise the build stays staged and
  the running install starts this script again (from a temp copy) when it is free to go: the waiter when
  the game and the app are both closed, the app when it is started.

  -BuildDir first copies a fresh build there as the staged one (replacing an older staged build) and marks
  it as installed in <InstallDir> (install.txt). -Start starts the installed exe at the end, whatever
  happened: the process that handed over has exited and must come back. After a failed or timed-out swap it
  gets --no-swap, so it doesn't hand over again at once.

  One run at a time per install folder (<InstallDir>.lock). A swap cut off between its two renames (power cut)
  is finished by the next run, and by the app at startup (follow.repair_install).

  Exit codes: 0 installed (or nothing staged), 3 still in use after -WaitSeconds (left staged), 1 failed.

.EXAMPLE
  .\packaging\install.ps1 -BuildDir D:\Maple-dist\mc-tracker -InstallDir D:\Maple-app\mc-tracker
  .\install.ps1 -InstallDir D:\Maple-app\mc-tracker -WaitSeconds 60 -Start -Arguments "--follow-game"
#>
param(
    [Parameter(Mandatory = $true)][string]$InstallDir,
    [string]$BuildDir = "",
    [int]$WaitSeconds = 0,
    [int]$LockSeconds = 90,   # how long to wait for another run on the same folder (it may wait for the exe)
    [switch]$Start,
    [string]$Arguments = ""
)

$ErrorActionPreference = "Stop"
$ExeName = "mc-tracker.exe"
$InstallDir = [IO.Path]::GetFullPath($InstallDir).TrimEnd('\')
if ($BuildDir) { $BuildDir = [IO.Path]::GetFullPath($BuildDir) }
$Next = "$InstallDir.next"
$Leaf = Split-Path $InstallDir -Leaf
$Parent = Split-Path $InstallDir -Parent
# Never sit inside the folders being renamed: a working folder there blocks the rename.
Set-Location -LiteralPath $env:TEMP
[IO.Directory]::SetCurrentDirectory($env:TEMP)

function Test-InUse([string]$Dir) {
    # A running exe can't be opened for exclusive write. (Renaming its folder would still work, and pull
    # the files out from under the running app.)
    $exe = Join-Path $Dir $ExeName
    if (-not (Test-Path -LiteralPath $exe)) { return $false }
    try { [IO.File]::Open($exe, 'Open', 'ReadWrite', 'None').Close(); return $false } catch { return $true }
}

function Repair-Install {
    # A swap cut off between its renames leaves no install folder: finish it, else put the old build back.
    if (Test-Path -LiteralPath $InstallDir) { return }
    $from = $null
    if (Test-Path -LiteralPath (Join-Path $Next $ExeName)) { $from = $Next }
    else {
        $from = Get-ChildItem -LiteralPath $Parent -Directory -Filter "$Leaf.old-*" -ErrorAction SilentlyContinue |
            Where-Object { Test-Path -LiteralPath (Join-Path $_.FullName $ExeName) } |
            Sort-Object Name -Descending | Select-Object -First 1 -ExpandProperty FullName
    }
    if ($from) { Rename-Item -LiteralPath $from -NewName $Leaf; Write-Host "Repaired $InstallDir from $from" }
}

function Swap-Staged {
    # 0: swapped in (or nothing staged), 3: the installed exe is still running.
    if (-not (Test-Path -LiteralPath (Join-Path $Next $ExeName))) { Write-Host "Nothing staged at $Next"; return 0 }
    $deadline = (Get-Date).AddSeconds($WaitSeconds)
    while (Test-InUse $InstallDir) {
        if ((Get-Date) -ge $deadline) {
            Write-Host "$InstallDir is running: the new build stays staged at $Next and swaps in when mc-tracker next starts or the game closes."
            return 3
        }
        Start-Sleep -Milliseconds 500
    }
    $Old = "$InstallDir.old-" + (Get-Date -Format "yyyyMMddHHmmssfff")
    if (Test-Path -LiteralPath $InstallDir) { Rename-Item -LiteralPath $InstallDir -NewName (Split-Path $Old -Leaf) }
    try {
        Rename-Item -LiteralPath $Next -NewName $Leaf
    } catch {
        if (Test-Path -LiteralPath $Old) { Rename-Item -LiteralPath $Old -NewName $Leaf }  # put the old build back
        throw
    }
    if (Test-Path -LiteralPath $Old) { Remove-Item -LiteralPath $Old -Recurse -Force -ErrorAction SilentlyContinue }
    Write-Host "Installed $InstallDir\$ExeName"
    return 0
}

$code = 1
$lock = $null
try {
    New-Item -ItemType Directory -Force $Parent | Out-Null
    $deadline = (Get-Date).AddSeconds($LockSeconds)
    while ($null -eq $lock) {  # one run at a time: two swaps at once could delete each other's builds
        try { $lock = [IO.File]::Open("$InstallDir.lock", 'OpenOrCreate', 'ReadWrite', 'None') }
        catch { if ((Get-Date) -ge $deadline) { throw "another install of $InstallDir is still running" }; Start-Sleep -Milliseconds 300 }
    }
    Repair-Install

    if ($BuildDir) {
        if (-not (Test-Path -LiteralPath (Join-Path $BuildDir $ExeName))) { throw "no $ExeName in $BuildDir" }
        # Copied under another name first: a running waiter swaps in whatever <InstallDir>.next holds.
        $Copying = "$InstallDir.copying"
        foreach ($d in @($Copying, $Next)) { if (Test-Path -LiteralPath $d) { Remove-Item -LiteralPath $d -Recurse -Force } }
        Copy-Item -LiteralPath $BuildDir -Destination $Copying -Recurse
        Set-Content -LiteralPath (Join-Path $Copying "install.txt") -Value $InstallDir -Encoding utf8
        Rename-Item -LiteralPath $Copying -NewName (Split-Path $Next -Leaf)
        Write-Host "Staged $Next"
    }
    $code = Swap-Staged
} catch {
    Write-Host "Install failed: $_"
    $code = 1
} finally {
    if ($lock) { $lock.Close(); Remove-Item -LiteralPath "$InstallDir.lock" -ErrorAction SilentlyContinue }
    $exe = Join-Path $InstallDir $ExeName
    if ($Start -and (Test-Path -LiteralPath $exe)) {  # always: the process that handed over has exited
        $argsLine = $Arguments.Trim()
        if ($code -ne 0) { $argsLine = ("$argsLine --no-swap").Trim() }  # no swap: don't hand over again at once
        try {
            if ($argsLine) { Start-Process -FilePath $exe -ArgumentList $argsLine -WorkingDirectory $env:TEMP }
            else { Start-Process -FilePath $exe -WorkingDirectory $env:TEMP }
        } catch { Write-Host "Couldn't start ${exe}: $_" }
    }
}
exit $code
