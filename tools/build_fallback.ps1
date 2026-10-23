param(
    [string]$Root = (Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Definition)),
    [switch]$CreateShortcuts
)

# Build runnable .bat launchers under <repo>\dist when PyInstaller is unavailable.
# The script lives in tools\, so the default root is the repository root.
# The generated bats use %~dp0\..\..\ to reach that root from dist\<name>\.

$dist = Join-Path $Root 'dist'
if (-not (Test-Path $dist)) { New-Item -ItemType Directory -Path $dist -Force | Out-Null }

$launcherDir = Join-Path $dist 'mykro-launcher'
$desktopDir = Join-Path $dist 'mykro-desktop'
New-Item -ItemType Directory -Path $launcherDir -Force | Out-Null
New-Item -ItemType Directory -Path $desktopDir -Force | Out-Null

$venvPy = Join-Path $Root '.venv\Scripts\python.exe'
if (-not (Test-Path $venvPy)) { $venvPy = 'python' }

$orch = Join-Path $Root 'orch.py'
if (-not (Test-Path $orch)) {
    Write-Error "Cannot find orch.py at $orch"
    exit 1
}

# Launcher (console)
$launcherBat = Join-Path $launcherDir 'mykro-launcher.bat'
$launcherContent = @'
@echo off
pushd %~dp0\..\..\
"%~dp0\..\..\.venv\Scripts\python.exe" "%~dp0\..\..\orch.py" %*
popd
'@
Set-Content -LiteralPath $launcherBat -Value $launcherContent -Encoding ASCII

# Desktop (windowed) launcher
$desktopBat = Join-Path $desktopDir 'mykro-desktop.bat'
$desktopContent = @'
@echo off
pushd %~dp0\..\..\gui_flask
start "MYKRO" "%~dp0\..\..\.venv\Scripts\python.exe" "orchestrator_tk.py" %*
popd
'@
Set-Content -LiteralPath $desktopBat -Value $desktopContent -Encoding ASCII

# Copy icon if available
$iconSrc = Join-Path $Root 'tools\mykro_icon.ico'
if (Test-Path $iconSrc) {
    Copy-Item -Path $iconSrc -Destination (Join-Path $dist 'mykro_icon.ico') -Force
}

Write-Host "Created fallback dist structure at: $dist"

if ($CreateShortcuts) {
    # Create desktop shortcuts pointing to the BAT files
    $desktop = [Environment]::GetFolderPath('Desktop')
    $wshell = New-Object -ComObject WScript.Shell

    $lnk1 = Join-Path $desktop 'MYKRO Launcher.lnk'
    $s1 = $wshell.CreateShortcut($lnk1)
    $s1.TargetPath = (Resolve-Path $launcherBat).Path
    $s1.WorkingDirectory = Split-Path $launcherBat
    if (Test-Path (Join-Path $dist 'mykro_icon.ico')) { $s1.IconLocation = (Join-Path $dist 'mykro_icon.ico') }
    $s1.Save()

    $lnk2 = Join-Path $desktop 'MYKRO Desktop.lnk'
    $s2 = $wshell.CreateShortcut($lnk2)
    $s2.TargetPath = (Resolve-Path $desktopBat).Path
    $s2.WorkingDirectory = Split-Path $desktopBat
    if (Test-Path (Join-Path $dist 'mykro_icon.ico')) { $s2.IconLocation = (Join-Path $dist 'mykro_icon.ico') }
    $s2.Save()

    Write-Host "Created desktop shortcuts: $lnk1, $lnk2"
}

Write-Host "Fallback build complete. You can distribute the 'dist' folder or create proper .exe later with PyInstaller." 
