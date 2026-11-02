"""Dependency install command used by /update.

    .venv/bin/python -m unittest tests.test_service
"""

import sys
import unittest
from unittest import mock

from harness import service


class PipInstallCmdTests(unittest.TestCase):
    def test_prefers_uv(self):
        with mock.patch.object(service.shutil, "which", return_value="/opt/bin/uv"):
            cmd = service.pip_install_cmd()
        self.assertEqual(cmd[:3], ["/opt/bin/uv", "pip", "install"])
        self.assertEqual(cmd[cmd.index("--python") + 1], sys.executable)
        self.assertEqual(cmd[-2:], ["-r", "requirements.txt"])

    def test_falls_back_to_pip(self):
        with mock.patch.object(service.shutil, "which", return_value=None):
            cmd = service.pip_install_cmd()
        self.assertEqual(cmd[:4], [sys.executable, "-m", "pip", "install"])


if __name__ == "__main__":
    unittest.main()
