param(
    [switch]$Start
)

$ErrorActionPreference = "Stop"
$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
$runScript = Join-Path $PSScriptRoot "run.ps1"
$botAction = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$runScript`"" -WorkingDirectory $PSScriptRoot
$uptimeAction = New-ScheduledTaskAction -Execute $python -Argument "-m harness.uptime" -WorkingDirectory $PSScriptRoot
Register-ScheduledTask -TaskName "Discord Agent" -Action $botAction -Trigger $trigger -Settings $settings -Description "Discord agent harness and watchdog" -Force | Out-Null
Register-ScheduledTask -TaskName "Discord Agent Uptime" -Action $uptimeAction -Trigger $trigger -Settings $settings -Description "Discord agent internet and system uptime monitor" -Force | Out-Null
if ($Start) {
    Start-ScheduledTask -TaskName "Discord Agent"
    Start-ScheduledTask -TaskName "Discord Agent Uptime"
}
Write-Host "Installed the Discord Agent and Discord Agent Uptime logon tasks."
