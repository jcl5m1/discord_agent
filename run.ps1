$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$env:PYTHONIOENCODING = "utf-8"
$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
$watchdog = Start-Process -FilePath $python -ArgumentList "-m", "harness.watchdog" -WorkingDirectory $PSScriptRoot -NoNewWindow -PassThru
$exitCode = 0
try {
    & $python -m harness.bot
    $exitCode = $LASTEXITCODE
}
finally {
    if (-not $watchdog.HasExited) {
        Stop-Process -Id $watchdog.Id -Force
    }
}
exit $exitCode
