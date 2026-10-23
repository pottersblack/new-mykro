# Workspace-root wrapper for MYKRO full stack launcher
Set-Location (Split-Path -Parent $MyInvocation.MyCommand.Definition)
& ".\gui_flask\start-all.ps1" @args
