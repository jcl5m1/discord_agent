"""The gdoc report tool: Doc ids, links between reports, header, images, setup check.

No network: the Google APIs and rclone are replaced by fakes.

    .venv/bin/python -m unittest tests.test_gdoc
"""

import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

from harness import gdoc

DOC = "1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789abcd"


class DocIdTests(unittest.TestCase):
    def test_link_id_and_registered_source(self):
        self.assertEqual(gdoc._doc_id(f"https://docs.google.com/document/d/{DOC}/edit", {}), DOC)
        self.assertEqual(gdoc._doc_id(DOC, {}), DOC)
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "r.md"
            src.write_text("# R")
            self.assertEqual(gdoc._doc_id(str(src), {str(src.resolve()): {"id": "x" * 30}}), "x" * 30)
        with self.assertRaises(SystemExit):
            gdoc._doc_id("not-a-doc", {})


class LinkTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.src = self.dir / "a.md"
        self.other = self.dir / "sub" / "b.md"
        self.other.parent.mkdir()
        self.reg = {str(self.other): {"url": "https://docs.google.com/document/d/B/edit", "title": "Report B"}}

    def test_link_to_other_report_goes_to_its_doc_with_its_title(self):
        html = gdoc._link_reports(self.src, '<p>See <a href="sub/b.md">sub/b.md</a>.</p>', self.reg)
        self.assertIn('<a href="https://docs.google.com/document/d/B/edit">Report B</a>', html)

    def test_plain_file_name_becomes_a_link(self):
        html = gdoc._link_reports(self.src, "<p>As in sub/b.md, the</p>", self.reg)
        self.assertIn('<a href="https://docs.google.com/document/d/B/edit">Report B</a>', html)

    def test_other_links_unchanged(self):
        html = '<p><a href="https://example.com/x.md">x</a> <a href="#top">top</a></p>'
        self.assertEqual(gdoc._link_reports(self.src, html, self.reg), html)


class HeaderAndImageTests(unittest.TestCase):
    def test_header_lists_authors_agents_and_times(self):
        h = gdoc._header(["Ada"], ["harness-2/sub-1"], 1_800_000_000, 1_800_003_600)
        self.assertIn("<b>Authors:</b> Ada", h)
        self.assertIn("<b>Written by agents:</b> harness-2/sub-1", h)
        self.assertIn("Last updated", h)

    def test_wide_image_fits_the_page(self):
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "r.md"
            buf = io.BytesIO()
            Image.new("RGB", (2000, 1000)).save(buf, format="PNG")
            (Path(d) / "big.png").write_bytes(buf.getvalue())
            tag = gdoc._fit_image(src, '<img alt="x" src="big.png" width="50">')
        self.assertIn(f'width="{gdoc.PAGE_PX}" height="{gdoc.PAGE_PX // 2}"', tag)
        self.assertIn("data:image/png;base64,", tag)
        self.assertNotIn('width="50"', tag)


class CheckTests(unittest.TestCase):
    def run_check(self, remotes: str, api_error: str | None = None):
        def fake_run(cmd, **kw):
            return mock.Mock(stdout=remotes, returncode=0)

        def fake_api(method, url, body=None):
            if "docs.googleapis.com" in url and api_error:
                raise SystemExit(api_error)
            if "docs.googleapis.com" in url:
                raise SystemExit("Drive API GET ...: HTTP 404 b'not found'")
            return {"user": {"displayName": "x"}}

        with mock.patch.object(gdoc.shutil if hasattr(gdoc, "shutil") else __import__("shutil"),
                               "which", return_value="/usr/bin/rclone"), \
                mock.patch.object(gdoc.subprocess, "run", side_effect=fake_run), \
                mock.patch.object(gdoc, "_token", return_value="t"), \
                mock.patch.object(gdoc, "_api", side_effect=fake_api):
            return gdoc.check()

    def test_all_set_up(self):
        self.assertEqual(self.run_check(f"{gdoc.REMOTE}:\n"), [])

    def test_missing_remote(self):
        (msg,) = self.run_check("other:\n")
        self.assertIn(f"no rclone remote {gdoc.REMOTE!r}", msg)

    def test_docs_api_off_names_the_console_page(self):
        err = ("HTTP 403 SERVICE_DISABLED docs.googleapis.com has not been used: visit "
               "https://console.developers.google.com/apis/api/docs.googleapis.com/overview?project=123 then retry")
        (msg,) = self.run_check(f"{gdoc.REMOTE}:\n", err)
        self.assertIn("overview?project=123", msg)


if __name__ == "__main__":
    unittest.main()
