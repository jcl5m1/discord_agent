"""Log files and the /logs export: rotation order, line limits, redaction.

    .venv/bin/python -m unittest tests.test_logfile
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from harness import config, logfile

# Shaped like a Discord bot token, assembled here so the source holds nothing a
# secret scanner would flag.
DISCORD_TOKEN = "M" + "x" * 25 + "." + "y" * 6 + "." + "z" * 38


class RedactTests(unittest.TestCase):
    def test_secrets_removed_context_kept(self):
        text = "\n".join([
            f"token {DISCORD_TOKEN} in a traceback",
            "Authorization: Bearer abcdefghijklmnopqrstuvwxyz123456",
            'got {"access_token": "eyJhbGciOiJIUzI1NiJ9.payload", "expires_in": 3600}',
            "ANTHROPIC key sk-ant-api03-ABCDEFGHIJKLMNOPQRSTUV",
            "client_secret=s3cr3tvalue99",
            "turn done in 12s, 340 chars of reply",
        ])
        out = logfile.redact(text)
        for secret in (DISCORD_TOKEN, "abcdefghijklmnopqrstuvwxyz123456", "eyJhbGciOiJIUzI1NiJ9",
                       "sk-ant-api03", "s3cr3tvalue99"):
            self.assertNotIn(secret, out)
        self.assertIn("Bearer [redacted]", out)
        self.assertIn('"expires_in": 3600', out)
        self.assertIn("turn done in 12s, 340 chars of reply", out)

    def test_configured_bot_token_removed_whatever_its_shape(self):
        with mock.patch.object(config, "BOT_TOKEN", "weird-token-value-123"):
            self.assertEqual(logfile.redact("x weird-token-value-123 y"), "x [redacted] y")


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch.object(logfile, "LOG_DIR", Path(self.tmp.name))
        p.start()
        self.addCleanup(p.stop)
        j = mock.patch.object(logfile, "_journal", return_value="")
        j.start()
        self.addCleanup(j.stop)

    def write(self, name, lines):
        (Path(self.tmp.name) / name).write_text("\n".join(lines) + "\n")

    def test_tail_reads_rotated_files_oldest_first(self):
        self.write("bot.log.2", ["old-1", "old-2"])
        self.write("bot.log.1", ["mid-1"])
        self.write("bot.log", ["new-1", f"leaked {DISCORD_TOKEN}"])
        out = logfile.export(4, header=["instance x: claude"])
        section = out.split("## bot.log (last 4 lines)\n", 1)[1].split("\n\n", 1)[0].splitlines()
        self.assertEqual(section, ["old-2", "mid-1", "new-1", "leaked [redacted]"])
        self.assertIn("# instance x: claude", out)
        self.assertIn("## watchdog.log (last 0 lines)", out)
        self.assertNotIn(DISCORD_TOKEN, out)


if __name__ == "__main__":
    unittest.main()
