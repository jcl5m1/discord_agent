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

    def run_wizard(self, answers):
        try:
            return subprocess.run(
                [
                    "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                    "-File", str(self.root / "setup.ps1"),
                    "-ConfigureOnly", "-NoService", "-SkipChecks",
                    "-Directory", str(self.repo),
                ],
                input="\n".join(answers) + "\n",
                text=True,
                capture_output=True,
                timeout=60,
            )
        except subprocess.TimeoutExpired as error:
            self.fail(f"Wizard timed out: {error.stdout}\n{error.stderr}")

    def test_wizard_writes_config_and_preserves_token_on_rerun(self):
        answers = [
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


if __name__ == "__main__":
    unittest.main()
