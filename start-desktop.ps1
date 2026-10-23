# Workspace-root wrapper for MYKRO desktop GUI launcher
Set-Location (Split-Path -Parent $MyInvocation.MyCommand.Definition)
& ".\gui_flask\start-desktop.ps1" @args
