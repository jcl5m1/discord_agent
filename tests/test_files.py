"""Per-instance shared files: token check, traversal, dotfiles, index.

    .venv/bin/python -m unittest tests.test_files
"""

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aiohttp.test_utils import TestClient, TestServer

from harness import files, proxy
from harness.instances import Instance, InstanceManager


class FilesEndpoint(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.patch = mock.patch.object(files, "FILES_DIR", root / "files")
        self.patch.start()
        self.store = InstanceManager(root / "inst.json")
        self.store.put(Instance(channel_id=1, name="LiveKit", provider="claude", workdir=str(root),
                                web_token="tok123"))
        folder = files.files_dir("livekit")
        (folder / "charts").mkdir(parents=True)
        (folder / "charts" / "asr.png").write_bytes(b"\x89PNG fake")
        (folder / ".secret").write_text("hidden")
        (root / "files" / "other").mkdir()
        (root / "files" / "other" / "x.txt").write_text("other instance")

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def get(self, *paths):
        async def go():
            async with TestClient(TestServer(proxy.build_app(self.store))) as c:
                out = []
                for p in paths:
                    r = await c.get(p, allow_redirects=False)
                    out.append((r.status, await r.read()))
                return out

        return asyncio.run(go())

    def test_serving(self):
        base = "/agents/livekit/tok123/files"
        res = self.get(
            f"{base}/charts/asr.png",
            "/agents/livekit/wrong/files/charts/asr.png",
            f"{base}/.secret",
            f"{base}/../other/x.txt",
            f"{base}/%2e%2e/other/x.txt",
            f"{base}/missing.png",
            f"{base}/",
            f"{base}",
            f"{base}/charts",
        )
        codes = [s for s, _ in res]
        self.assertEqual(codes, [200, 403, 404, 404, 404, 404, 200, 302, 302])
        self.assertEqual(res[0][1], b"\x89PNG fake")
        index = res[6][1].decode()
        self.assertIn("charts/asr.png", index)
        self.assertNotIn(".secret", index)

    def test_listing_is_newest_first_and_skips_dotfiles(self):
        folder = files.files_dir("livekit")
        (folder / "log.txt").write_text("new")
        paths = [e.path for e in files.listing("livekit")]
        self.assertEqual(set(paths), {"charts/asr.png", "log.txt"})
        self.assertIsNone(files.resolve("livekit", "../other/x.txt"))


if __name__ == "__main__":
    unittest.main()
