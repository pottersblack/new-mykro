# Workspace-root wrapper for MYKRO Web GUI launcher
Set-Location (Split-Path -Parent $MyInvocation.MyCommand.Definition)
& ".\gui_flask\start-web.ps1" @args
