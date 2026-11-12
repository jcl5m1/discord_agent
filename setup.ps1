param(
    [string]$Directory,
    [switch]$NoService,
    [switch]$ConfigureOnly,
    [switch]$SkipChecks,
    [switch]$SkipProviderTest
)

$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
$env:PYTHONIOENCODING = "utf-8"
$RepoUrl = "https://github.com/jcl5m1/discord_agent.git"
$ScriptPath = $MyInvocation.MyCommand.Path
$ScriptDirectory = if ($ScriptPath) { Split-Path -Parent $ScriptPath } else { $null }

function Write-Step([string]$Text) {
    Write-Host "`n==> $Text" -ForegroundColor Cyan
}

function Write-Info([string]$Text) {
    Write-Host "  $Text"
}

function Write-Warning([string]$Text) {
    Write-Host "  ! $Text" -ForegroundColor Yellow
}

function Read-Answer([string]$Question, [string]$Default = "") {
    $suffix = if ($Default) { " [$Default]" } else { "" }
    $answer = Read-Host "  $Question$suffix"
    if ([string]::IsNullOrWhiteSpace($answer)) { return $Default }
    return $answer.Trim()
}

function Read-Optional([string]$Question, [string]$Default = "") {
    $answer = Read-Answer $Question $Default
    if ($answer -eq "-") { return "" }
    return $answer
}

function Read-YesNo([string]$Question, [bool]$Default = $true) {
    $suffix = if ($Default) { "Y/n" } else { "y/N" }
    $answer = Read-Host "  $Question [$suffix]"
    if ([string]::IsNullOrWhiteSpace($answer)) { return $Default }
    return $answer -match "^[Yy]"
}

function Read-BotToken([string]$Current) {
    $label = if ($Current) { "Discord bot token [keep current]" } else { "Discord bot token" }
    if ([Console]::IsInputRedirected) {
        $token = Read-Host "  $label"
    }
    else {
        $secure = Read-Host "  $label" -AsSecureString
        $credential = New-Object System.Net.NetworkCredential("", $secure)
        $token = $credential.Password
    }
    if ([string]::IsNullOrWhiteSpace($token)) { return $Current }
    return $token
}

function Read-ReferenceDefaults {
    if (-not (Get-Command ssh -ErrorAction SilentlyContinue)) { return @{} }
    $sshHost = Read-Answer "Copy non-secret defaults from an SSH host (blank skips)"
    if (-not $sshHost) { return @{} }
    $remotePath = Read-Answer "Remote .env path" "~/github/jcl5m1/discord_agent/.env"
    $python = @'
import base64
import os
import sys
path = os.path.expanduser(base64.b64decode(sys.argv[1]).decode("utf-8"))
excluded = {"DISCORD_BOT_TOKEN", "DISCORD_CATEGORY_ID"}
with open(path, encoding="utf-8") as source:
    for line in source:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key = line.split("=", 1)[0]
        if key not in excluded:
            print(line)
'@
    $script64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($python))
    $path64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($remotePath))
    $remoteCommand = "python3 -c `"import base64;exec(base64.b64decode('$script64'))`" '$path64'"
    $output = & ssh -o BatchMode=yes $sshHost $remoteCommand
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "Could not import reference defaults; continuing without them"
        return @{}
    }
    $map = @{}
    foreach ($line in $output) {
        if ($line -match "^([A-Z_][A-Z0-9_]*)=(.*)$") {
            $map[$matches[1]] = $matches[2]
        }
    }
    if ($map["DEFAULT_WORKDIR"] -match "^/(home|Users)/") { $map["DEFAULT_WORKDIR"] = $HOME }
    if ($map["KIMI_BIN"]) { $map["KIMI_BIN"] = Join-Path $HOME ".kimi-code\bin\kimi.exe" }
    if ($map["MUSE_BIN"]) { $map["MUSE_BIN"] = Join-Path $HOME ".local\bin\muse.exe" }
    Write-Info "Imported $($map.Count) non-secret settings; bot token and category ID were excluded"
    return $map
}

function Update-UserPath {
    $directories = @(
        (Join-Path $HOME ".local\bin"),
        (Join-Path $HOME ".kimi-code\bin"),
        (Join-Path $env:LOCALAPPDATA "Programs\muse"),
        (Join-Path $env:ProgramFiles "Git\cmd")
    )
    foreach ($directory in $directories) {
        if ((Test-Path -LiteralPath $directory) -and (($env:PATH -split ";") -notcontains $directory)) {
            $env:PATH = "$directory;$env:PATH"
        }
    }
}

function Get-EnvMap([string]$Path) {
    $map = @{}
    if (-not (Test-Path -LiteralPath $Path)) { return $map }
    foreach ($line in [IO.File]::ReadAllLines($Path)) {
        if ($line -match "^([A-Z_][A-Z0-9_]*)=(.*)$") {
            $value = $matches[2].Trim()
            if ($value.Length -ge 2 -and (($value.StartsWith('"') -and $value.EndsWith('"')) -or ($value.StartsWith("'") -and $value.EndsWith("'")))) {
                $value = $value.Substring(1, $value.Length - 2)
            }
            $map[$matches[1]] = $value
        }
    }
    return $map
}

function Get-AgentPath([string]$Name) {
    $command = Get-Command $Name -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    $candidates = switch ($Name) {
        "kimi" { @((Join-Path $HOME ".kimi-code\bin\kimi.exe")) }
        "muse" {
            @(
                (Join-Path $HOME ".local\bin\muse.exe"),
                (Join-Path $env:LOCALAPPDATA "Programs\muse\muse.exe")
            )
        }
        default { @((Join-Path $HOME ".local\bin\$Name.exe")) }
    }
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate) { return $candidate }
    }
    return $null
}

function Get-ClaudeStatus([string]$Path, [switch]$Subscription) {
    $previousKey = [Environment]::GetEnvironmentVariable("ANTHROPIC_API_KEY", "Process")
    try {
        if ($Subscription) { Remove-Item Env:ANTHROPIC_API_KEY -ErrorAction SilentlyContinue }
        return & $Path auth status --json 2>$null | Out-String | ConvertFrom-Json
    }
    finally {
        if ($null -eq $previousKey) {
            Remove-Item Env:ANTHROPIC_API_KEY -ErrorAction SilentlyContinue
        }
        else {
            $env:ANTHROPIC_API_KEY = $previousKey
        }
    }
}

function Test-AgentLogin([string]$Name, [string]$Path) {
    if (-not $Path) { return $false }
    try {
        switch ($Name) {
            "claude" {
                $status = Get-ClaudeStatus $Path
                return $status.loggedIn -eq $true
            }
            "kimi" {
                $providers = (& $Path provider list 2>$null | Out-String)
                return $providers -match "source=oauth"
            }
            "muse" {
                $authPath = Join-Path $HOME ".config\muse\auth.json"
                return -not [string]::IsNullOrWhiteSpace($env:META_API_KEY) -or (Test-Path -LiteralPath $authPath)
            }
        }
    }
    catch {
        return $false
    }
    return $false
}

function Install-Agent([string]$Name) {
    $url = switch ($Name) {
        "claude" { "https://claude.ai/install.ps1" }
        "kimi" { "https://code.kimi.com/kimi-code/install.ps1" }
        "muse" { "https://api.meta.ai/muse-launcher.ps1" }
    }
    $installer = Invoke-RestMethod -Uri $url
    & ([scriptblock]::Create($installer))
    Update-UserPath
}

function Invoke-AgentLogin([string]$Name, [string]$Path) {
    if ($Name -eq "claude") {
        & $Path auth login
    }
    else {
        & $Path login
    }
}

function Get-AgentLabel([string]$Name) {
    switch ($Name) {
        "claude" { return "Claude Code (Anthropic account)" }
        "kimi" { return "Kimi Code (Moonshot account)" }
        "muse" { return "Muse (Meta account)" }
    }
}

function Invoke-DiscordApi([string]$Token, [string]$Path) {
    return Invoke-RestMethod -Method Get -Uri "https://discord.com/api/v10$Path" -Headers @{ Authorization = "Bot $Token" } -UserAgent "DiscordBot (https://github.com/jcl5m1/discord_agent, 1.0)"
}

function Test-ProviderTurn([string]$Python, [string]$Provider, [string]$Workdir) {
    $providerScript = @'
import asyncio
import os
import sys
from harness.providers.base import get_provider

async def main():
    provider = sys.argv[1]
    workdir = os.path.expanduser(sys.argv[2])
    model = "haiku" if provider == "claude" else None
    final = None
    async for event in get_provider(provider).run_turn(
        workdir,
        "Reply with exactly DISCORD_AGENT_SETUP_OK and nothing else. Do not use tools.",
        model=model,
    ):
        if event.kind in ("done", "error"):
            final = event
    if final is None or final.kind != "done":
        raise RuntimeError(final.text if final else "provider returned no result")
    print(final.text)

asyncio.run(main())
'@
    $providerScript64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($providerScript))
    $providerCommand = "import base64;exec(base64.b64decode('$providerScript64'))"
    $ErrorActionPreference = "Continue"
    $output = & $Python -c $providerCommand $Provider $Workdir 2>&1 | Out-String
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "Provider test failed:`n$($output.Trim())"
        return $false
    }
    Write-Info "Provider test passed: $($output.Trim())"
    return $true
}

function Write-Configuration(
    [string]$Repository,
    [hashtable]$Answers,
    [hashtable]$Existing,
    [hashtable]$Defaults
) {
    $envPath = Join-Path $Repository ".env"
    $templatePath = Join-Path $Repository ".env.example"
    $seen = @{}
    [string[]]$lines = @(foreach ($line in [IO.File]::ReadAllLines($templatePath)) {
        if ($line -match "^([A-Z_][A-Z0-9_]*)=(.*)$") {
            $key = $matches[1]
            $seen[$key] = $true
            $value = if ($Answers.ContainsKey($key)) {
                $Answers[$key]
            }
            elseif ($Existing.ContainsKey($key)) {
                $Existing[$key]
            }
            elseif ($Defaults.ContainsKey($key)) {
                $Defaults[$key]
            }
            else {
                $matches[2]
            }
            "$key=$value"
        }
        else {
            $line
        }
    })
    $extra = @($Existing.Keys | Where-Object { -not $seen.ContainsKey($_) } | Sort-Object)
    if ($extra.Count -gt 0) {
        $lines += ""
        $lines += "# --- kept from the previous .env ---"
        foreach ($key in $extra) { $lines += "$key=$($Existing[$key])" }
    }
    if (Test-Path -LiteralPath $envPath) {
        Copy-Item -LiteralPath $envPath -Destination "$envPath.bak" -Force
        Write-Info "Previous .env saved as .env.bak"
    }
    [IO.File]::WriteAllLines($envPath, $lines, (New-Object System.Text.UTF8Encoding($false)))
    Write-Info "Wrote $envPath"
}

Update-UserPath
$locationPushed = $false
$token = $null
try {
    if (-not $ConfigureOnly) {
        Write-Step "1/7 Prerequisites"
        if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
            $winget = Get-Command winget -ErrorAction SilentlyContinue
            if (-not $winget) { throw "Git is required. Install Git for Windows, then rerun setup." }
            Write-Info "Installing Git for Windows with winget"
            & $winget.Source install --id Git.Git -e --source winget --accept-package-agreements --accept-source-agreements
            if ($LASTEXITCODE -ne 0) { throw "Git installation failed" }
            Update-UserPath
            if (-not (Get-Command git -ErrorAction SilentlyContinue)) { throw "Git was installed but could not be found on PATH" }
        }
        $useUv = $false
        $pythonLauncher = $null
        $py = Get-Command py -ErrorAction SilentlyContinue
        if ($py) {
            & $py.Source -3.12 -c "import sys" 2>$null
            if ($LASTEXITCODE -eq 0) { $pythonLauncher = @($py.Source, "-3.12") }
        }
        if (-not $pythonLauncher) {
            $python = Get-Command python -ErrorAction SilentlyContinue
            if ($python) {
                & $python.Source -c "import sys; assert sys.version_info[:2] == (3, 12)" 2>$null
                if ($LASTEXITCODE -eq 0) { $pythonLauncher = @($python.Source) }
            }
        }
        if (-not $pythonLauncher) {
            $useUv = $true
            if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
                Write-Info "Installing uv, which will provide Python 3.12"
                $uvInstaller = Invoke-RestMethod -Uri "https://astral.sh/uv/install.ps1"
                & ([scriptblock]::Create($uvInstaller))
                Update-UserPath
            }
            if (-not (Get-Command uv -ErrorAction SilentlyContinue)) { throw "uv installation failed" }
        }
        Write-Info "Git and Python 3.12 prerequisites ready"

        Write-Step "2/7 Getting the code"
    }

    if ([string]::IsNullOrWhiteSpace($Directory)) {
        if ($ScriptDirectory -and (Test-Path -LiteralPath (Join-Path $ScriptDirectory "harness\bot.py"))) {
            $Directory = $ScriptDirectory
        }
        else {
            $Directory = Join-Path $HOME "discord_agent"
        }
    }
    $Directory = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Directory)

    if (-not $ConfigureOnly) {
        if (Test-Path -LiteralPath (Join-Path $Directory ".git")) {
            Write-Info "Updating $Directory"
            & git -C $Directory pull --ff-only
            if ($LASTEXITCODE -ne 0) { Write-Warning "git pull failed; continuing with the current checkout" }
        }
        else {
            if ((Test-Path -LiteralPath $Directory) -and @(Get-ChildItem -LiteralPath $Directory -Force).Count -gt 0) {
                throw "$Directory exists and is not an empty Git checkout. Choose another -Directory."
            }
            $parent = Split-Path -Parent $Directory
            if ($parent) { New-Item -ItemType Directory -Path $parent -Force | Out-Null }
            Write-Info "Cloning into $Directory"
            & git clone $RepoUrl $Directory
            if ($LASTEXITCODE -ne 0) { throw "git clone failed" }
        }
    }
    if (-not (Test-Path -LiteralPath (Join-Path $Directory "harness\bot.py"))) {
        throw "No discord_agent checkout found at $Directory"
    }
    Push-Location $Directory
    $locationPushed = $true

    $venvPython = Join-Path $Directory ".venv\Scripts\python.exe"
    if (-not $ConfigureOnly) {
        Write-Step "3/7 Installing Python dependencies"
        if (-not (Test-Path -LiteralPath $venvPython)) {
            if ($useUv) {
                & uv venv --python 3.12 ".venv"
            }
            else {
                $pythonPrefixArgs = @($pythonLauncher | Select-Object -Skip 1)
                & $pythonLauncher[0] @pythonPrefixArgs -m venv ".venv"
            }
            if ($LASTEXITCODE -ne 0) { throw "Could not create the Python virtual environment" }
        }
        if ($useUv) {
            & uv pip install --python $venvPython -q -r requirements.txt
        }
        else {
            & $venvPython -m pip install -r requirements.txt
        }
        if ($LASTEXITCODE -ne 0) { throw "Python dependency installation failed" }

        Write-Info "Installing headless Chromium for HTML rendering"
        $env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $Directory ".pw-browsers"
        & $venvPython -m playwright install chromium
        if ($LASTEXITCODE -ne 0) {
            Write-Warning "Chromium download failed; only HTML rendering is affected"
        }
        else {
            $browserCheck = @'
import asyncio
from playwright.async_api import async_playwright
async def main():
    async with async_playwright() as p:
        await (await p.chromium.launch()).close()
asyncio.run(main())
'@
            & $venvPython -c $browserCheck 2>$null
            if ($LASTEXITCODE -ne 0) { Write-Warning "Chromium could not start; only HTML rendering is affected" }
        }
    }

    $ready = @()
    if (-not $ConfigureOnly) { Write-Step "4/7 Agent CLIs" }
    foreach ($name in @("claude", "kimi", "muse")) {
        $path = Get-AgentPath $name
        if (-not $ConfigureOnly) {
            if (-not $path) {
                $defaultInstall = $name -eq "claude"
                if (-not (Read-YesNo "Install $(Get-AgentLabel $name)?" $defaultInstall)) { continue }
                try {
                    Install-Agent $name
                    $path = Get-AgentPath $name
                }
                catch {
                    Write-Warning "$name installation failed: $($_.Exception.Message)"
                    continue
                }
            }
            if (-not $path) {
                Write-Warning "$name was installed but could not be found on PATH"
                continue
            }
            if (-not (Test-AgentLogin $name $path)) {
                if (Read-YesNo "$name is installed but not logged in. Log in now?" $true) {
                    try { Invoke-AgentLogin $name $path } catch { Write-Warning "$name login failed" }
                }
            }
        }
        if (Test-AgentLogin $name $path) {
            $ready += $name
            if (-not $ConfigureOnly) { Write-Info "${name}: $path - logged in" }
        }
        elseif (-not $ConfigureOnly) {
            $loginCommand = if ($name -eq "claude") { "claude auth login" } else { "$name login" }
            Write-Warning "$name is not logged in; run '$loginCommand' later"
        }
    }
    if ($ready.Count -eq 0) { Write-Warning "No agent CLI is ready; the bot can start but turns will fail" }

    Write-Step "5/7 Configuration"
    $envPath = Join-Path $Directory ".env"
    $existing = Get-EnvMap $envPath
    $defaults = @{}
    foreach ($template in @(".env.example", "windows.env.example", "windows.config.local")) {
        $values = Get-EnvMap (Join-Path $Directory $template)
        foreach ($key in $values.Keys) { $defaults[$key] = $values[$key] }
    }
    if ($existing.Count -eq 0) {
        $reference = Read-ReferenceDefaults
        foreach ($key in $reference.Keys) { $defaults[$key] = $reference[$key] }
    }
    foreach ($key in $existing.Keys) { $defaults[$key] = $existing[$key] }

    if (-not $existing.ContainsKey("DISCORD_BOT_TOKEN") -or -not $existing["DISCORD_BOT_TOKEN"]) {
        Write-Host @"
  Create the bot at https://discord.com/developers/applications:
    - New Application -> name it -> Create
    - Bot -> Reset Token -> Copy
    - Bot -> Privileged Gateway Intents -> enable Message Content Intent
  Discord server and category IDs are available by right-clicking them with
  User Settings -> Advanced -> Developer Mode enabled.
"@
    }
    $token = Read-BotToken $defaults["DISCORD_BOT_TOKEN"]
    if (-not $token) { throw "A Discord bot token is required" }
    $guild = Read-Answer "Server (guild) ID" $defaults["DISCORD_GUILD_ID"]
    $category = Read-Answer "Category ID for agent channels" $defaults["DISCORD_CATEGORY_ID"]
    if ($guild -notmatch "^[0-9]{15,22}$") { throw "Server ID must be a 15-22 digit number" }
    if ($category -notmatch "^[0-9]{15,22}$") { throw "Category ID must be a 15-22 digit number" }

    $providerDefault = $defaults["DEFAULT_PROVIDER"]
    if ($ready.Count -gt 0 -and $ready -notcontains $providerDefault) { $providerDefault = $ready[0] }
    if (-not $providerDefault) { $providerDefault = "claude" }
    $provider = Read-Answer "Default agent CLI (ready: $($ready -join ', '))" $providerDefault
    if (@("claude", "kimi", "muse") -notcontains $provider) { throw "Provider must be claude, kimi, or muse" }
    $claudePath = Get-AgentPath "claude"
    $claudeUseSubscription = "$($defaults["CLAUDE_USE_SUBSCRIPTION"])" -match "^(?i)(1|true|yes)$"
    if ($provider -eq "claude" -and $env:ANTHROPIC_API_KEY) {
        try {
            $subscriptionStatus = Get-ClaudeStatus $claudePath -Subscription
            if ($subscriptionStatus.loggedIn) {
                $plan = if ($subscriptionStatus.planTier) { $subscriptionStatus.planTier } else { $subscriptionStatus.subscriptionType }
                $claudeUseSubscription = Read-YesNo "Use your Claude subscription ($plan) instead of ANTHROPIC_API_KEY?" $claudeUseSubscription
            }
            elseif ($claudeUseSubscription) {
                Write-Warning "No Claude subscription login is available; run 'claude auth login' without ANTHROPIC_API_KEY"
            }
        }
        catch {
            Write-Warning "Could not check the Claude subscription login: $($_.Exception.Message)"
        }
    }
    $workdirDefault = if ($defaults["DEFAULT_WORKDIR"]) { $defaults["DEFAULT_WORKDIR"] } else { $HOME }
    $workdir = Read-Answer "Default working directory for new agents" $workdirDefault

    Write-Info "Optional settings: Enter keeps the default; '-' clears it"
    $publicUrl = (Read-Optional "Public base URL for live web pages" $defaults["PUBLIC_BASE_URL"]).TrimEnd("/")
    $whatsAppOwner = (Read-Optional "Your WhatsApp number with country code" $defaults["WHATSAPP_OWNER"]) -replace "\D", ""
    $whatsAppBot = ""
    if ($whatsAppOwner) {
        $whatsAppBot = (Read-Optional "The bot's separate WhatsApp number" $defaults["WHATSAPP_BOT_NUMBER"]) -replace "\D", ""
    }
    $kimiPath = Get-AgentPath "kimi"
    $musePath = Get-AgentPath "muse"
    $answers = @{
        DISCORD_BOT_TOKEN = $token
        DISCORD_GUILD_ID = $guild
        DISCORD_CATEGORY_ID = $category
        DEFAULT_PROVIDER = $provider
        CLAUDE_USE_SUBSCRIPTION = $claudeUseSubscription.ToString().ToLower()
        DEFAULT_WORKDIR = $workdir
        PUBLIC_BASE_URL = $publicUrl
        WHATSAPP_OWNER = $whatsAppOwner
        WHATSAPP_BOT_NUMBER = $whatsAppBot
    }
    if ($kimiPath) { $answers["KIMI_BIN"] = $kimiPath }
    if ($claudePath) { $answers["CLAUDE_BIN"] = $claudePath }
    if ($musePath) { $answers["MUSE_BIN"] = $musePath }
    Write-Configuration $Directory $answers $existing $defaults

    if (-not $SkipChecks) {
        Write-Step "6/7 Checking Discord"
        $botId = $null
        try {
            $me = Invoke-DiscordApi $token "/users/@me"
            $botId = $me.id
            Write-Info "Token OK - bot is $($me.username)"
        }
        catch {
            Write-Warning "Discord rejected the token or the API is unavailable: $($_.Exception.Message)"
        }
        if ($botId) {
            try {
                $app = Invoke-DiscordApi $token "/applications/@me"
                $intentMask = (1 -shl 18) -bor (1 -shl 19)
                if (([int64]$app.flags -band $intentMask) -eq 0) {
                    Write-Warning "Message Content Intent appears to be off; enable it in the developer portal"
                }
            }
            catch {
                Write-Warning "Could not check Message Content Intent: $($_.Exception.Message)"
            }
            $guildInfo = $null
            $invite = "https://discord.com/oauth2/authorize?client_id=$botId&scope=bot+applications.commands&permissions=268561488"
            for ($attempt = 1; $attempt -le 3 -and -not $guildInfo; $attempt++) {
                try {
                    $guildInfo = Invoke-DiscordApi $token "/guilds/$guild"
                    Write-Info "Bot is in server '$($guildInfo.name)'"
                }
                catch {
                    Write-Warning "The bot is not in that server yet. Invite it with:`n  $invite"
                    if ($attempt -lt 3) {
                        $retry = Read-Answer "Press Enter after inviting the bot, or type 'skip'"
                        if ($retry -match "^(?i)skip$") { break }
                    }
                }
            }
            try {
                $categoryInfo = Invoke-DiscordApi $token "/channels/$category"
                if ($categoryInfo.type -eq 4) {
                    Write-Info "Category '$($categoryInfo.name)' found"
                }
                else {
                    Write-Warning "The category ID refers to a channel, not a category"
                }
            }
            catch {
                Write-Warning "The category is not visible to the bot: $($_.Exception.Message)"
            }
        }
    }
    if ($ready.Count -gt 0 -and $ready -notcontains $provider) {
        Write-Warning "Default provider $provider is not installed and logged in"
    }
    if (-not $SkipProviderTest -and $ready -contains $provider -and (Test-Path -LiteralPath $venvPython)) {
        if (Read-YesNo "Run a small $provider turn to verify the saved configuration?" $true) {
            $null = Test-ProviderTurn $venvPython $provider $workdir
        }
    }

    Write-Step "7/7 Windows logon tasks"
    if ($NoService) {
        Write-Info "Skipping logon tasks; start the harness with: $Directory\run.ps1"
    }
    elseif (Read-YesNo "Install and start the bot, watchdog, and uptime logon tasks?" $true) {
        $serviceStarted = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
        & (Join-Path $Directory "install-windows-tasks.ps1") -Start
        Start-Sleep -Seconds 5
        $tasks = Get-ScheduledTask -TaskName "Discord Agent", "Discord Agent Uptime"
        foreach ($task in $tasks) {
            if ($task.State -eq "Running") {
                Write-Info "$($task.TaskName) is running"
            }
            else {
                Write-Warning "$($task.TaskName) is not running; check Task Scheduler (state: $($task.State))"
            }
        }
        $runtimeCheck = @'
import json
import sys
import time
from pathlib import Path
started = float(sys.argv[1])
for name, key in (("heartbeat.json", "ts"), ("uptime_state.json", "last_alive")):
    data = json.loads((Path("data") / name).read_text())
    stamp = data[key]
    assert started - 5 <= stamp <= time.time() + 5, f"{name} is stale"
    assert time.time() - stamp < 90, f"{name} is not updating"
print("Bot heartbeat and uptime state are fresh")
'@
        $runtimeCheck64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($runtimeCheck))
        $runtimeCommand = "import base64;exec(base64.b64decode('$runtimeCheck64'))"
        & $venvPython -c $runtimeCommand $serviceStarted 2>$null | Out-Null
        if ($LASTEXITCODE -eq 0) {
            Write-Info "Bot heartbeat and uptime state are fresh"
        }
        else {
            Write-Warning "Tasks started, but fresh heartbeat files were not detected"
        }
    }
    else {
        Write-Info "Tasks not installed; start the harness with: $Directory\run.ps1"
    }

    Write-Step "Done"
    Write-Info "In Discord: /new name:my-project prompt:hello"
    if ($whatsAppOwner) { Write-Info "WhatsApp: run /whatsapp in Discord to link the bot account" }
}
finally {
    if ($locationPushed) { Pop-Location }
    $token = $null
}
