$ErrorActionPreference = "Stop"
$envPath = Join-Path $PSScriptRoot ".env"
if (-not (Test-Path -LiteralPath $envPath)) {
    $template = Join-Path $PSScriptRoot "windows.config.local"
    if (-not (Test-Path -LiteralPath $template)) {
        $template = Join-Path $PSScriptRoot "windows.env.example"
    }
    Copy-Item -LiteralPath $template -Destination $envPath
}
$secureToken = Read-Host "Discord bot token" -AsSecureString
$credential = New-Object System.Net.NetworkCredential("", $secureToken)
$token = $credential.Password
if ([string]::IsNullOrWhiteSpace($token)) {
    throw "A bot token is required."
}
$found = $false
[string[]]$lines = @(Get-Content -LiteralPath $envPath)
[string[]]$lines = @($lines | ForEach-Object {
    if ($_ -match "^DISCORD_BOT_TOKEN=") {
        $found = $true
        "DISCORD_BOT_TOKEN=$token"
    }
    else {
        $_
    }
})
if (-not $found) {
    $lines += "DISCORD_BOT_TOKEN=$token"
}
[System.IO.File]::WriteAllLines($envPath, $lines, (New-Object System.Text.UTF8Encoding($false)))
$token = $null
$credential = $null
Write-Host "Saved the bot token to $envPath"
