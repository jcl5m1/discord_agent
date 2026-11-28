"""Provider CLIs may print stdout lines longer than asyncio's 64 KiB default.

    .venv/bin/python -m unittest tests.test_provider_stream_limit
"""

import os
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from harness import config
from harness.providers.base import STREAM_LIMIT, get_provider

LONG = 512 * 1024  # a large tool result in one stream-json event


@unittest.skipIf(os.name == "nt", "fake CLIs are POSIX scripts")
class StreamLimitTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def fake_cli(self, name):
        fake = self.root / name
        fake.write_text("#!" + sys.executable + "\n" + textwrap.dedent(f"""
            import json, sys
            sys.stdout.write(json.dumps({{"type": "tool_result", "content": "x" * {LONG}}}) + "\\n")
            sys.stdout.flush()
        """))
        fake.chmod(0o755)
        return fake

    def test_limit_is_larger_than_the_line(self):
        self.assertGreater(STREAM_LIMIT, LONG)

    async def test_every_provider_reads_a_long_line_without_failing(self):
        for name in ("claude", "kimi", "muse"):
            with self.subTest(provider=name):
                fake = self.fake_cli(name)
                with mock.patch.object(config, f"{name.upper()}_BIN", str(fake)):
                    provider = get_provider(name)
                    try:
                        async for _ in provider.run_turn(str(self.root), "hello"):
                            pass
                    except ValueError as e:  # LimitOverrunError surfaces as ValueError
                        self.fail(f"{name}: {e}")
                    except Exception:
                        # Other errors (the fake CLI is not a full agent) are not this test's concern.
                        pass


if __name__ == "__main__":
    unittest.main()
