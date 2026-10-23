param(
    [string]$TargetPath = "$PWD\dist\MYKRO.exe",
    [string]$ShortcutName = 'MYKRO Desktop.lnk'
)

$desktop = [Environment]::GetFolderPath('Desktop')
$shortcutPath = Join-Path $desktop $ShortcutName

$wshell = New-Object -ComObject WScript.Shell
$shortcut = $wshell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = Resolve-Path $TargetPath
$shortcut.WorkingDirectory = Split-Path $TargetPath
$shortcut.WindowStyle = 1
$shortcut.IconLocation = "$TargetPath,0"
$shortcut.Save()

Write-Host "Created desktop shortcut: $shortcutPath"