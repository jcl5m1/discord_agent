import os
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

    def test_claude_subscription_prompt_updates_setting(self):
        fake_bin = Path(self.tmp.name) / "bin"
        fake_bin.mkdir()
        (fake_bin / "claude.cmd").write_text(
            "@echo off\n"
            "echo {\"loggedIn\": true, \"subscriptionType\": \"max\", \"planTier\": \"max_20x\"}\n"
        )
        env = {
            "ANTHROPIC_API_KEY": "external-key",
            "PATH": str(fake_bin) + os.pathsep + os.environ["PATH"],
            "USERPROFILE": self.tmp.name,
        }
        answers = [
            "",
            "test-token",
            "123456789012345678",
            "234567890123456789",
            "claude",
            "y",
            "C:\\work",
            "-",
            "-",
        ]
        result = self.run_wizard(answers, env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        config = (self.repo / ".env").read_text(encoding="utf-8")
        self.assertIn("CLAUDE_USE_SUBSCRIPTION=true", config)

        result = self.run_wizard(["", "", "", "", "n", "", "", ""], env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        config = (self.repo / ".env").read_text(encoding="utf-8")
        self.assertIn("CLAUDE_USE_SUBSCRIPTION=false", config)
        self.assertIn("DISCORD_BOT_TOKEN=test-token", config)

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


if __name__ == "__main__":
    unittest.main()
