import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


@unittest.skipUnless(os.name == "nt", "Windows PowerShell setup test")
class SetupWizardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(__file__).resolve().parent.parent
        self.repo = Path(self.tmp.name) / "repo"
        (self.repo / "harness").mkdir(parents=True)
        (self.repo / "harness" / "bot.py").write_text("")
        for name in (".env.example", "windows.env.example"):
            shutil.copyfile(self.root / name, self.repo / name)

    def tearDown(self):
        self.tmp.cleanup()

    def run_wizard(self, answers, env_overrides=None):
        env = os.environ.copy()
        env.pop("ANTHROPIC_API_KEY", None)
        if env_overrides:
            env.update(env_overrides)
        try:
            return subprocess.run(
                [
                    "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                    "-File", str(self.root / "setup.ps1"),
                    "-ConfigureOnly", "-NoService", "-SkipChecks", "-SkipProviderTest",
                    "-Directory", str(self.repo),
                ],
                input="\n".join(answers) + "\n",
                text=True,
                capture_output=True,
                timeout=60,
                env=env,
            )
        except subprocess.TimeoutExpired as error:
            self.fail(f"Wizard timed out: {error.stdout}\n{error.stderr}")

    def test_wizard_writes_config_and_preserves_token_on_rerun(self):
        answers = [
            "",
            "test-token",
            "123456789012345678",
            "234567890123456789",
            "claude",
            "C:\\work",
            "-",
            "-",
        ]
        result = self.run_wizard(answers)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        config = (self.repo / ".env").read_text(encoding="utf-8")
        self.assertIn("DISCORD_BOT_TOKEN=test-token", config)
        self.assertIn("DISCORD_GUILD_ID=123456789012345678", config)
        self.assertIn("DISCORD_CATEGORY_ID=234567890123456789", config)
        self.assertIn("DEFAULT_WORKDIR=C:\\work", config)
        self.assertIn("WHATSAPP_OWNER=\n", config)

        with (self.repo / ".env").open("a", encoding="utf-8") as env:
            env.write("CUSTOM_SETTING=kept\n")
        result = self.run_wizard([""] * 7)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        config = (self.repo / ".env").read_text(encoding="utf-8")
        self.assertIn("DISCORD_BOT_TOKEN=test-token", config)
        self.assertIn("CUSTOM_SETTING=kept", config)
        self.assertTrue((self.repo / ".env.bak").exists())

    def test_reference_import_excludes_secrets_and_translates_paths(self):
        fake_bin = Path(self.tmp.name) / "bin"
        fake_bin.mkdir()
        (fake_bin / "ssh.cmd").write_text(
            "@echo off\n"
            "echo DISCORD_GUILD_ID=123456789012345678\n"
            "echo DISCORD_BOT_TOKEN=do-not-copy\n"
            "echo DISCORD_CATEGORY_ID=999999999999999999\n"
            "echo DEFAULT_WORKDIR=/home/jcl5m1\n"
            "echo KIMI_BIN=/home/jcl5m1/.kimi-code/bin/kimi\n"
        )
        answers = [
            "fake-host",
            "",
            "test-token",
            "",
            "234567890123456789",
            "claude",
            "",
            "-",
            "-",
        ]
        result = self.run_wizard(
            answers,
            {
                "PATH": str(fake_bin) + os.pathsep + os.environ["PATH"],
                "USERPROFILE": self.tmp.name,
            },
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        config = (self.repo / ".env").read_text(encoding="utf-8")
        self.assertIn("DISCORD_GUILD_ID=123456789012345678", config)
        self.assertIn("DISCORD_CATEGORY_ID=234567890123456789", config)
        self.assertNotIn("do-not-copy", config)
        self.assertNotIn("999999999999999999", config)
        self.assertNotIn("/home/jcl5m1", config)
        self.assertIn(f"DEFAULT_WORKDIR={self.tmp.name}", config)

    def test_claude_login_check_ignores_api_key(self):
        fake_bin = Path(self.tmp.name) / "bin"
        fake_bin.mkdir()
        (fake_bin / "claude.cmd").write_text(
            "@echo off\n"
            "if defined ANTHROPIC_API_KEY (\n"
            "  echo {\"loggedIn\": false, \"authMethod\": \"api_key\"}\n"
            ") else (\n"
            "  echo {\"loggedIn\": true, \"authMethod\": \"claude.ai\"}\n"
            ")\n"
        )
        with (self.repo / "windows.env.example").open("a", encoding="utf-8") as env_file:
            env_file.write("DEFAULT_PROVIDER=kimi\n")
        answers = [
            "",
            "test-token",
            "123456789012345678",
            "234567890123456789",
            "",
            "C:\\work",
            "-",
            "-",
        ]
        result = self.run_wizard(
            answers,
            {
                "ANTHROPIC_API_KEY": "external-key",
                "PATH": str(fake_bin) + os.pathsep + os.environ["PATH"],
                "USERPROFILE": self.tmp.name,
            },
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        config = (self.repo / ".env").read_text(encoding="utf-8")
        self.assertIn("DEFAULT_PROVIDER=claude", config)
        self.assertIn("ALLOW_AGENT_API_KEYS=false", config)
        self.assertNotIn("CLAUDE_USE_SUBSCRIPTION", config)

    def test_powershell_scripts_parse(self):
        for name in ("setup.ps1", "run.ps1", "save-token.ps1", "install-windows-tasks.ps1"):
            with self.subTest(script=name):
                result = subprocess.run(
                    [
                        "powershell", "-NoProfile", "-NonInteractive", "-Command",
                        "$errors = $null; "
                        "[void][System.Management.Automation.Language.Parser]::ParseFile("
                        "$env:SCRIPT_PATH, [ref]$null, [ref]$errors); "
                        "if ($errors.Count) { $errors | ForEach-Object { Write-Error $_ }; exit 1 }",
                    ],
                    env={**os.environ, "SCRIPT_PATH": str(self.root / name)},
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class WindowsTaskScriptTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parent.parent
        self.installer = (root / "install-windows-tasks.ps1").read_text(encoding="utf-8")
        self.runner = (root / "run.ps1").read_text(encoding="utf-8")

    def test_ownership_requires_repo_python_and_an_agent_module(self):
        self.assertIn("[IO.Path]::GetFullPath($python)", self.installer)
        self.assertRegex(
            self.installer,
            r"\[IO\.Path\]::GetFullPath\(\$process\.ExecutablePath\)\s+-ieq\s+\$pythonPath",
        )
        self.assertIn("$process.CommandLine -match $modulePattern", self.installer)
        self.assertNotIn("repositoryPattern", self.installer)

        match = re.search(r"\$modulePattern = '([^']+)'", self.installer)
        self.assertIsNotNone(match)
        module_pattern = re.compile(match.group(1), re.IGNORECASE)
        for module in ("bot", "watchdog", "uptime"):
            self.assertIsNotNone(
                module_pattern.search(
                    rf'"C:\repo\.venv\Scripts\python.exe" -m harness.{module}'
                )
            )
        for command in (
            r'"C:\repo\.venv\Scripts\python.exe" -m harness.botty',
            r'"C:\repo\.venv\Scripts\python.exe" -m other.harness.bot',
            r'"C:\repo\.venv\Scripts\python.exe" harness.bot',
            r'"C:\repo\.venv\Scripts\python.exe" -m harness.uptime.extra',
        ):
            self.assertIsNone(module_pattern.search(command), command)

    def test_base_python_children_of_the_venv_launcher_are_owned(self):
        # The Windows venv python.exe is a launcher; the real interpreter is its child
        # under the base Python named in .venv\pyvenv.cfg (seen on arcade-rtx5070, 11-28).
        self.assertIn('Join-Path $PSScriptRoot ".venv\\pyvenv.cfg"', self.installer)
        self.assertIn("$venvHome", self.installer)
        self.assertNotRegex(self.installer, r"\$home\s*=")  # $HOME is read-only in PowerShell
        self.assertIn("[IO.Path]::GetFullPath($_.ExecutablePath) -ieq $basePythonPath", self.installer)
        self.assertIn("$isBaseChild = (-not $parent) -or (Test-VenvAgent $parent)", self.installer)
        self.assertIn("$isLauncher -or $isAgent -or $isBaseChild", self.installer)

    def test_launcher_ownership_is_scoped_to_the_exact_run_script(self):
        self.assertIn('$runScript = Join-Path $PSScriptRoot "run.ps1"', self.installer)
        self.assertIn("[regex]::Escape([IO.Path]::GetFullPath($runScript))", self.installer)
        self.assertIn('$_.Name -ieq "powershell.exe"', self.installer)
        self.assertIn('$_.CommandLine -match $launcherPattern', self.installer)
        self.assertIn('(?:^|\\s)-File\\s+', self.installer)

    def test_restart_kills_owned_trees_and_waits_before_starting(self):
        kill = self.installer.index("& taskkill.exe /T /F /PID $process.ProcessId")
        requery = self.installer.index("$remaining = @(Get-OwnedAgentProcess)", kill)
        timeout = self.installer.index("Timed out waiting for owned Discord Agent processes", requery)
        bot_start = self.installer.index('Start-ScheduledTask -TaskName "Discord Agent"', timeout)
        uptime_start = self.installer.index(
            'Start-ScheduledTask -TaskName "Discord Agent Uptime"', bot_start
        )
        self.assertLess(kill, requery)
        self.assertLess(requery, timeout)
        self.assertLess(timeout, bot_start)
        self.assertLess(bot_start, uptime_start)
        self.assertIn("Start-Sleep -Milliseconds 250", self.installer[requery:bot_start])

    def test_cleanup_has_no_global_process_or_provider_fallback(self):
        for source in (self.installer, self.runner):
            self.assertNotIn("Stop-Process", source)
            self.assertNotIn("taskkill.exe /IM", source)
        self.assertNotIn("Get-CimInstance", self.runner)
        self.assertIn("& taskkill.exe /T /F /PID $watchdog.Id", self.runner)
        self.assertIn("if (-not $watchdog.HasExited)", self.runner)


if __name__ == "__main__":
    unittest.main()
