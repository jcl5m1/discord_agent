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


class SyncImageTests(unittest.TestCase):
    """gdoc_sync: a changed image in a paragraph just before a table (task #57)."""

    @staticmethod
    def doc(uri: str) -> dict:
        cell = {"content": [{"startIndex": 5, "endIndex": 7, "paragraph": {"elements": [
            {"startIndex": 5, "endIndex": 7, "textRun": {"content": "x\n"}}]}}]}
        return {"body": {"content": [
            {"startIndex": 1, "endIndex": 3, "paragraph": {"elements": [
                {"startIndex": 1, "endIndex": 2, "inlineObjectElement": {"inlineObjectId": "img"}},
                {"startIndex": 2, "endIndex": 3, "textRun": {"content": "\n"}}]}},
            {"startIndex": 3, "endIndex": 9, "table": {"tableRows": [{"tableCells": [cell]}]}},
            {"startIndex": 9, "endIndex": 10, "paragraph": {"elements": [
                {"startIndex": 9, "endIndex": 10, "textRun": {"content": "\n"}}]}}]},
            "inlineObjects": {"img": {"inlineObjectProperties": {"embeddedObject": {
                "imageProperties": {"contentUri": uri}, "size": {"width": {"magnitude": 100, "unit": "PT"}}}}}}}

    def test_newline_before_the_table_is_kept(self):
        from harness import gdoc_sync as gs
        with mock.patch.object(gs, "_image_hash", side_effect=lambda props, cache:
                               props["embeddedObject"]["imageProperties"]["contentUri"]):
            old, new = gs._blocks(self.doc("old"), {}), gs._blocks(self.doc("new"), {})
            src = self.doc("new")
            reqs = gs.plan(old, new, src, 10)
        deletes = [r["deleteContentRange"]["range"] for r in reqs if "deleteContentRange" in r]
        self.assertEqual(deletes, [{"startIndex": 1, "endIndex": 2}])  # the image only, not its newline (2-3)
        (img,) = [r["insertInlineImage"] for r in reqs if "insertInlineImage" in r]
        self.assertEqual((img["location"]["index"], img["uri"]), (1, "new"))


class MarkdownTests(unittest.TestCase):
    """The Markdown → HTML step before the Drive import (task #58); tests/gdoc_render_live.py checks a real Doc."""

    def test_list_right_after_a_paragraph_line(self):
        html = gdoc._to_html("Intro line:\n- one\n- two\n")
        self.assertIn("<ul>", html)
        self.assertNotIn("- one", html)

    def test_two_space_nesting(self):
        self.assertEqual(gdoc._to_html("- a\n  - b\n").count("<ul>"), 2)

    def test_list_kinds(self):
        md = "- a\n  - b\n    - c\n- d\n\ntext\n\n1. x\n   - y\n2. z\n"
        self.assertEqual(gdoc.list_kinds(md), [(0, 0, False), (0, 1, False), (0, 2, False), (0, 0, False),
                                               (1, 0, True), (1, 1, True), (1, 0, True)])

    def test_code_quote_and_table_styles(self):
        html = gdoc._to_html("`x<y`\n\n```\nblock\n```\n\n> q\n\n| H |\n|---|\n| v |\n")
        self.assertIn(f'<span style="{gdoc.CODE_FONT}">x&lt;y</span>', html)  # inline code: a monospace span
        self.assertIn(f'<pre style="{gdoc.CODE_FONT}"><code>block', html)
        self.assertIn('<blockquote style="margin-left', html)
        self.assertIn("<td><b>H</b></td>", html)  # header cells as bold cells, no <th>/<thead>
        self.assertNotIn("<th", html)

    def test_section_links_keep_their_text(self):
        html = gdoc._to_html("See [the plans](#4-plans) and [site](https://x.org).")
        self.assertIn("See the plans and", html)
        self.assertIn('<a href="https://x.org">site</a>', html)

    def test_sync_does_not_copy_links_within_a_doc(self):
        from harness import gdoc_sync as gs
        self.assertEqual(gs._style({"bold": True, "link": {"bookmarkId": "id.x"}}, gs.TEXT_KEYS), '{"bold": true}')
        self.assertIn("url", gs._style({"link": {"url": "https://x.org"}}, gs.TEXT_KEYS))


class PictureIdTests(unittest.TestCase):
    """gdoc_sync: a JPEG that Google re-encodes is still the same picture (the storey report looped, #55)."""

    @staticmethod
    def jpeg(img: Image.Image, quality: int) -> bytes:
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=quality)
        return buf.getvalue()

    def test_reencoded_copy_has_the_same_id(self):
        from harness import gdoc_sync as gs
        img = Image.new("RGB", (400, 300))
        for x in range(400):
            for y in range(0, 300, 7):
                img.putpixel((x, y), (x % 256, y % 256, 128))
        cache: dict = {}
        a = gs._picture_id(self.jpeg(img, 95), cache)
        b = gs._picture_id(self.jpeg(img, 70), cache)
        self.assertEqual(a, b)
        other = img.copy()
        other.paste((255, 255, 255), (0, 0, 200, 150))  # a changed chart
        self.assertNotEqual(a, gs._picture_id(self.jpeg(other, 95), cache))
        self.assertNotEqual(a, gs._picture_id(self.jpeg(img.resize((200, 150)), 95), cache))  # other size
