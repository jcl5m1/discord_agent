import os
import subprocess
import sys
import unittest
from pathlib import Path


class ClaudeAuthTests(unittest.TestCase):
    def check_api_key(self, subscription, expected):
        env = os.environ.copy()
        env["ANTHROPIC_API_KEY"] = "test-key"
        env["CLAUDE_USE_SUBSCRIPTION"] = subscription
        code = (
            "import os; from harness import config; "
            f"assert ('ANTHROPIC_API_KEY' in os.environ) is {expected}"
        )
        subprocess.run(
            [sys.executable, "-c", code],
            cwd=Path(__file__).resolve().parent.parent,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )

    def test_subscription_removes_api_key(self):
        self.check_api_key("true", False)

    def test_default_preserves_api_key(self):
        self.check_api_key("false", True)


if __name__ == "__main__":
    unittest.main()
