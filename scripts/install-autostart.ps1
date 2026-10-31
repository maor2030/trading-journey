# Registers a scheduled task that starts the trading agent when you log in to Windows
# and restarts it if it crashes. Remove it with:
#   Unregister-ScheduledTask -TaskName "trading-journey agent" -Confirm:$false
$launcher = Join-Path $PSScriptRoot "start-agent.ps1"
$action = New-ScheduledTaskAction -Execute "conhost.exe" `
    -Argument "--headless powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$launcher`"" `
    -WorkingDirectory (Split-Path $PSScriptRoot -Parent)
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$trigger.Delay = "PT1M"  # let the network come up first
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 5) `
    -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName "trading-journey agent" -Action $action -Trigger $trigger -Settings $settings `
    -Principal $principal -Description "Starts the trading-journey agent (dashboard at http://127.0.0.1:8050)" -Force
