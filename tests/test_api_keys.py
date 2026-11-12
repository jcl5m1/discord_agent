"""Provider API keys are dropped so agents use subscription logins.

    .venv/bin/python -m unittest tests.test_api_keys
"""

import os
import subprocess
import sys
import unittest
from pathlib import Path

KEYS = ("ANTHROPIC_API_KEY", "META_API_KEY", "MOONSHOT_API_KEY")


class ApiKeyTests(unittest.TestCase):
    def config_env(self, allow):
        """Import config in a fresh process; return which keys survive and which it reports."""
        env = {**os.environ, **{k: "test-key" for k in KEYS}, "ALLOW_AGENT_API_KEYS": allow}
        code = (
            "import os; from harness import config; "
            f"print(','.join(k for k in {KEYS!r} if k in os.environ)); "
            "print(','.join(config.IGNORED_API_KEYS))"
        )
        out = subprocess.run(
            [sys.executable, "-c", code], cwd=Path(__file__).resolve().parent.parent,
            env=env, check=True, capture_output=True, text=True,
        ).stdout.splitlines()
        return out[0], out[1]

    def test_keys_removed_by_default(self):
        kept, ignored = self.config_env("")
        self.assertEqual(kept, "")
        self.assertEqual(set(ignored.split(",")), set(KEYS))

    def test_opt_in_keeps_keys(self):
        kept, ignored = self.config_env("true")
        self.assertEqual(set(kept.split(",")), set(KEYS))
        self.assertEqual(ignored, "")

    def test_value_never_reported(self):
        _, ignored = self.config_env("")
        self.assertNotIn("test-key", ignored)


if __name__ == "__main__":
    unittest.main()
