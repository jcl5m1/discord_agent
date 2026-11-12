param(
    [switch]$Start
)

$ErrorActionPreference = "Stop"
$taskNames = @("Discord Agent", "Discord Agent Uptime")
$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
$runScript = Join-Path $PSScriptRoot "run.ps1"
$botAction = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$runScript`"" -WorkingDirectory $PSScriptRoot
$uptimeAction = New-ScheduledTaskAction -Execute $python -Argument "-m harness.uptime" -WorkingDirectory $PSScriptRoot
Register-ScheduledTask -TaskName "Discord Agent" -Action $botAction -Trigger $trigger -Settings $settings -Description "Discord agent harness and watchdog" -Force | Out-Null
Register-ScheduledTask -TaskName "Discord Agent Uptime" -Action $uptimeAction -Trigger $trigger -Settings $settings -Description "Discord agent internet and system uptime monitor" -Force | Out-Null
if ($Start) {
    foreach ($name in $taskNames) {
        $task = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
        if ($task -and $task.State -eq "Running") {
            Stop-ScheduledTask -TaskName $name
        }
    }
    Start-Sleep -Seconds 2
    $repositoryPattern = [regex]::Escape($PSScriptRoot)
    $processes = Get-CimInstance Win32_Process | Where-Object {
        $_.CommandLine -match "harness\.(bot|watchdog|uptime)" -and
        $_.CommandLine -match $repositoryPattern
    }
    foreach ($process in $processes) {
        Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
    }
    Start-ScheduledTask -TaskName "Discord Agent"
    Start-ScheduledTask -TaskName "Discord Agent Uptime"
}
Write-Host "Installed the Discord Agent and Discord Agent Uptime logon tasks."
