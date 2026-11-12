"""Session listing for /session.

    .venv/bin/python -m unittest tests.test_sessions
"""

import json
import os
import tempfile
import unittest
from pathlib import Path

from harness.providers import claude, kimi
from harness.providers.base import get_provider


class KimiSessionsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _state(self, ws, sid, **data):
        folder = self.root / ws / sid
        folder.mkdir(parents=True)
        (folder / "state.json").write_text(json.dumps({"id": sid, **data}), encoding="utf-8")

    def test_lists_newest_first_with_cwd_and_title(self):
        self._state("wd_a", "session_old", cwd="C:/work/a", title="Old", updatedAt=1000)
        self._state("wd_b", "session_new", cwd="C:/work/b", title="New", updatedAt=5000)
        found = kimi.list_sessions(self.root)
        self.assertEqual([s.id for s in found], ["session_new", "session_old"])
        self.assertEqual(found[0].workdir, os.path.normpath("C:/work/b"))
        self.assertEqual(found[0].title, "New")
        self.assertEqual(found[0].updated, 5.0)

    def test_skips_archived_broken_and_cwdless(self):
        self._state("wd_a", "session_arch", cwd="C:/a", archived=True)
        self._state("wd_a", "session_nocwd")
        bad = self.root / "wd_a" / "session_bad"
        bad.mkdir(parents=True)
        (bad / "state.json").write_text("{not json", encoding="utf-8")
        self.assertEqual(kimi.list_sessions(self.root), [])

    def test_missing_root_is_empty(self):
        self.assertEqual(kimi.list_sessions(self.root / "nope"), [])


class ClaudeSessionsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _transcript(self, sid, *entries, mtime=None):
        folder = self.root / "C--work-a"
        folder.mkdir(exist_ok=True)
        path = folder / f"{sid}.jsonl"
        path.write_text("\n".join(json.dumps(e) for e in entries), encoding="utf-8")
        if mtime:
            os.utime(path, (mtime, mtime))

    def test_reads_cwd_and_first_prompt(self):
        self._transcript(
            "abc",
            {"type": "summary"},
            {"type": "user", "cwd": "C:/work/a", "message": {"content": "fix   the\nbug"}},
        )
        [info] = claude.list_sessions(self.root)
        self.assertEqual((info.id, info.workdir, info.title), ("abc", os.path.normpath("C:/work/a"), "fix the bug"))

    def test_strips_harness_brief_from_title(self):
        prompt = "[harness capabilities — x]\nstuff\n[end of harness capabilities]\nreal ask"
        self._transcript(
            "abc", {"type": "user", "cwd": "C:/a", "message": {"content": [{"type": "text", "text": prompt}]}}
        )
        self.assertEqual(claude.list_sessions(self.root)[0].title, "real ask")

    def test_newest_first_and_skips_cwdless(self):
        self._transcript("old", {"type": "user", "cwd": "C:/a", "message": {"content": "o"}}, mtime=1000)
        self._transcript("new", {"type": "user", "cwd": "C:/a", "message": {"content": "n"}}, mtime=2000)
        self._transcript("none", {"type": "summary"}, mtime=3000)
        self.assertEqual([s.id for s in claude.list_sessions(self.root)], ["new", "old"])


class FindSessionTests(unittest.TestCase):
    def test_providers_without_listing_find_nothing(self):
        self.assertIsNone(get_provider("muse").find_session("x"))


class KimiInUseHintTests(unittest.TestCase):
    def test_hint_matches_kimi_error(self):
        err = "This session is in use by another Kimi Code CLI instance. Stop the session in that instance"
        self.assertIn(kimi._IN_USE, err)


if __name__ == "__main__":
    unittest.main()
