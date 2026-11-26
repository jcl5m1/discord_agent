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
$pythonPath = [IO.Path]::GetFullPath($python)
$runScriptPattern = [regex]::Escape([IO.Path]::GetFullPath($runScript))
$launcherPattern = '(?:^|\s)-File\s+(?:"' + $runScriptPattern + '"|' + $runScriptPattern + ')(?=\s|$)'
$modulePattern = '(?:^|\s)-m\s+"?harness\.(?:bot|watchdog|uptime)"?(?=\s|$)'

function Get-OwnedAgentProcess {
    Get-CimInstance Win32_Process | Where-Object {
        $isLauncher = $_.Name -ieq "powershell.exe" -and $_.CommandLine -match $launcherPattern
        $isAgent = $_.ExecutablePath -and
            [IO.Path]::GetFullPath($_.ExecutablePath) -ieq $pythonPath -and
            $_.CommandLine -match $modulePattern
        $isLauncher -or $isAgent
    }
}

Register-ScheduledTask -TaskName "Discord Agent" -Action $botAction -Trigger $trigger -Settings $settings -Description "Discord agent harness and watchdog" -Force | Out-Null
Register-ScheduledTask -TaskName "Discord Agent Uptime" -Action $uptimeAction -Trigger $trigger -Settings $settings -Description "Discord agent internet and system uptime monitor" -Force | Out-Null
if ($Start) {
    foreach ($name in $taskNames) {
        $task = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
        if ($task -and $task.State -eq "Running") {
            Stop-ScheduledTask -TaskName $name
        }
    }

    $processes = @(Get-OwnedAgentProcess)
    $launchers = @($processes | Where-Object { $_.Name -ieq "powershell.exe" })
    $pythonOwners = @($processes | Where-Object { $_.Name -ine "powershell.exe" })
    foreach ($process in $launchers + $pythonOwners) {
        & taskkill.exe /T /F /PID $process.ProcessId | Out-Null
    }

    $deadline = [DateTime]::UtcNow.AddSeconds(30)
    do {
        $remaining = @(Get-OwnedAgentProcess)
        if ($remaining.Count -eq 0) {
            break
        }
        if ([DateTime]::UtcNow -ge $deadline) {
            $remainingIds = ($remaining | ForEach-Object { $_.ProcessId }) -join ", "
            throw "Timed out waiting for owned Discord Agent processes to exit (PIDs: $remainingIds)."
        }
        Start-Sleep -Milliseconds 250
    } while ($true)

    Start-ScheduledTask -TaskName "Discord Agent"
    Start-ScheduledTask -TaskName "Discord Agent Uptime"
}
Write-Host "Installed the Discord Agent and Discord Agent Uptime logon tasks."
