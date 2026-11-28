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

# On Windows the venv's python.exe is a launcher: the real interpreter runs as its child
# under the base Python (the "home" in .venv\pyvenv.cfg). Such a child is ours when its
# parent is our venv launcher, or when its parent is gone (a process left behind).
$basePythonPath = $null
$venvConfig = Join-Path $PSScriptRoot ".venv\pyvenv.cfg"
if (Test-Path $venvConfig) {
    $venvHome = Get-Content $venvConfig | Where-Object { $_ -match '^\s*home\s*=' } | Select-Object -First 1
    if ($venvHome) {
        $basePythonPath = [IO.Path]::GetFullPath((Join-Path ($venvHome -replace '^\s*home\s*=\s*', '').Trim() "python.exe"))
    }
}

function Test-VenvAgent($process) {
    $process.ExecutablePath -and
        [IO.Path]::GetFullPath($process.ExecutablePath) -ieq $pythonPath -and
        $process.CommandLine -match $modulePattern
}

function Get-OwnedAgentProcess {
    $all = @(Get-CimInstance Win32_Process)
    $byId = @{}
    foreach ($process in $all) { $byId[[int]$process.ProcessId] = $process }
    $all | Where-Object {
        $isLauncher = $_.Name -ieq "powershell.exe" -and $_.CommandLine -match $launcherPattern
        $isAgent = Test-VenvAgent $_
        $isBaseChild = $false
        if (-not $isAgent -and $basePythonPath -and $_.ExecutablePath -and
                [IO.Path]::GetFullPath($_.ExecutablePath) -ieq $basePythonPath -and
                $_.CommandLine -match $modulePattern) {
            $parent = $byId[[int]$_.ParentProcessId]
            $isBaseChild = (-not $parent) -or (Test-VenvAgent $parent)
        }
        $isLauncher -or $isAgent -or $isBaseChild
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
