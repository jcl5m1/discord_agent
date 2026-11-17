"""Workdir paths: labels stripped, never resolved inside the harness folder.

    .venv/bin/python -m unittest tests.test_workdir_paths
"""

import unittest
from pathlib import Path
from unittest import mock

from harness import instances
from harness.instances import normalize_workdir
from harness.workdirs import clean

HOME = Path("/home/u")


class NormalizeTests(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(Path, "home", return_value=HOME)
        p.start()
        self.addCleanup(p.stop)

    def check(self, raw, want):
        with mock.patch("os.path.expanduser", side_effect=lambda s: str(HOME) + s[1:] if s.startswith("~") else s):
            self.assertEqual(normalize_workdir(raw), want)

    def test_decorated_or_unexpanded_tilde_is_rerooted_at_home(self):
        self.check("/srv/discord_agent/📁~/github/placeframe", "/home/u/github/placeframe")
        self.check("📁~/github/placeframe", "/home/u/github/placeframe")
        self.check("📁 ~/github/x/", "/home/u/github/x")
        self.check("/srv/discord_agent/~/slr-client", "/home/u/slr-client")

    def test_relative_paths_count_from_home(self):
        self.check("github/x", "/home/u/github/x")

    def test_ordinary_paths(self):
        self.check("~/github/x", "/home/u/github/x")
        self.check("/tmp/a/../b", "/tmp/b")
        self.check("/home/u/my~notes", "/home/u/my~notes")


class CleanTests(unittest.TestCase):
    def test_labels_from_older_choices_are_removed(self):
        self.assertEqual(clean("📂 ~/github (use this folder)"), "~/github")
        self.assertEqual(clean("🕘 ~/slr-client (used before)"), "~/slr-client")
        self.assertEqual(clean("✨ ~/github/new (new folder)"), "~/github/new")
        self.assertEqual(clean("📁 ~/github/"), "~/github/")
        self.assertEqual(clean("~\\github\\"), "~/github/")


class ClaudeProjectDirTests(unittest.TestCase):
    def test_emoji_counts_as_two_characters_like_claude_code(self):
        self.assertEqual(instances._claude_project_dir("/a/📁~/b").name, "-a-----b")
        self.assertEqual(instances._claude_project_dir("/home/u/x_y").name, "-home-u-x-y")


if __name__ == "__main__":
    unittest.main()
