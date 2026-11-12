"""Folder-by-folder workdir suggestions for /new.

    .venv/bin/python -m unittest tests.test_workdirs
"""

import tempfile
import unittest
from pathlib import Path

from harness.workdirs import MAX_CHOICES, suggest


class SuggestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        for d in ("github/jcl5m1/discord_agent", "github/jcl5m1/audio", "Github2", "slr-client",
                  ".config", "Documents"):
            (self.home / d).mkdir(parents=True)
        (self.home / "notes.txt").write_text("not a folder")

    def values(self, current, recent=None):
        return [v for _, v in suggest(current, self.home, recent)]

    def test_empty_lists_home_with_recent_first(self):
        out = suggest("", self.home, recent=[str(self.home / "slr-client")])
        self.assertEqual(out[0], ("🕘 ~/slr-client (used before)", "~/slr-client"))
        self.assertEqual(out[1], ("📂 ~ (use this folder)", "~"))
        self.assertEqual([v for _, v in out[2:]],
                         ["~/Documents/", "~/github/", "~/Github2/", "~/slr-client/"])

    def test_picking_a_folder_lists_its_subfolders(self):
        self.assertEqual(self.values("~/github/"), ["~/github", "~/github/jcl5m1/"])
        self.assertEqual(self.values("~/github/jcl5m1/"),
                         ["~/github/jcl5m1", "~/github/jcl5m1/audio/", "~/github/jcl5m1/discord_agent/"])

    def test_typing_filters_case_insensitively(self):
        self.assertEqual(self.values("~/git"), ["~/github/", "~/Github2/"])
        self.assertEqual(self.values("~/github/jcl5m1/dis"), ["~/github/jcl5m1/discord_agent/"])

    def test_hidden_folders_only_when_asked(self):
        self.assertNotIn("~/.config/", self.values("~/"))
        self.assertEqual(self.values("~/.c"), ["~/.config/"])

    def test_no_match_offers_a_new_folder(self):
        self.assertEqual(suggest("~/github/newproj", self.home),
                         [("✨ ~/github/newproj (new folder)", "~/github/newproj")])

    def test_files_are_not_suggested(self):
        self.assertNotIn("~/notes.txt/", self.values("~/"))

    def test_missing_folder_gives_nothing_to_browse(self):
        self.assertEqual(self.values("~/nope/x"), ["~/nope/x"])

    def test_absolute_paths_and_backslashes(self):
        self.assertEqual(self.values(str(self.home / "github") + "/")[:1], ["~/github"])
        self.assertEqual(self.values("~\\github\\"), ["~/github", "~/github/jcl5m1/"])

    def test_limits(self):
        many = self.home / "many"
        for i in range(40):
            (many / f"d{i:02}").mkdir(parents=True)
        out = suggest("~/many/", self.home)
        self.assertEqual(len(out), MAX_CHOICES)
        deep = self.home / ("x" * 90) / "y"
        deep.mkdir(parents=True)
        self.assertTrue(all(len(v) <= 100 and len(n) <= 100 for n, v in suggest("~/" + "x" * 90 + "/", self.home)))


if __name__ == "__main__":
    unittest.main()
