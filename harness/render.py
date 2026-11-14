"""Render HTML snippets to PNG for Discord attachments.

Discord messages can't render HTML, so when an agent produces a fenced
```html block the harness screenshots it with headless Chromium (Playwright)
and posts the image inline. The browser is kept repo-local via the
PLAYWRIGHT_BROWSERS_PATH override below.

One-time setup:
    PLAYWRIGHT_BROWSERS_PATH=.pw-browsers .venv/bin/playwright install chromium
"""

from __future__ import annotations

import os
from pathlib import Path

# Keep the browser inside the repo instead of ~/.cache/ms-playwright.
os.environ.setdefault(
    "PLAYWRIGHT_BROWSERS_PATH", str(Path(__file__).resolve().parent.parent / ".pw-browsers")
)

_TEMPLATE = """<!doctype html>
<html><head><meta charset="utf-8"><style>
body {{ margin: 0; padding: 16px; font-family: -apple-system, "Segoe UI", sans-serif;
       background: #ffffff; color: #1a1a1a; line-height: 1.4; }}
table {{ border-collapse: collapse; }} th, td {{ border: 1px solid #ccc; padding: 4px 8px; }}
</style></head><body>{body}</body></html>"""

_MAX_WIDTH = 1600
_MAX_HEIGHT = 4000


async def render_html_to_png(html: str) -> bytes:
    """Render an HTML snippet to PNG bytes, auto-sized to its content."""
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        try:
            # Start small so scrollHeight reflects content, not the viewport.
            page = await browser.new_page(viewport={"width": 400, "height": 200})
            await page.set_content(_TEMPLATE.format(body=html), wait_until="load")
            size = await page.evaluate(
                "() => ({w: document.body.scrollWidth, h: document.body.scrollHeight})"
            )
            w = min(max(size["w"] + 32, 240), _MAX_WIDTH)
            h = min(max(size["h"] + 32, 120), _MAX_HEIGHT)
            await page.set_viewport_size({"width": w, "height": h})
            try:
                return await page.screenshot(full_page=True)
            except Exception:
                return await page.screenshot(clip={"x": 0, "y": 0, "width": w, "height": h})
        finally:
            await browser.close()


_TABLE_STYLE = """
body { background: #2b2d31; padding: 12px; }
table { border-collapse: collapse; font: 15px/1.35 -apple-system, "Segoe UI", Roboto, sans-serif;
        color: #dbdee1; }
th { background: #1e1f22; color: #f2f3f5; font-weight: 600; text-align: left; }
th, td { border: 1px solid #3f4147; padding: 6px 10px; max-width: 420px; vertical-align: top; }
td.right, th.right { text-align: right; }
tr:nth-child(even) td { background: #313338; }
code { font: 13px ui-monospace, Menlo, Consolas, monospace; background: #1e1f22; padding: 1px 4px;
       border-radius: 3px; }
"""


def _cell_html(cell: str) -> str:
    """A table cell's inline Markdown (bold, code, links) as HTML."""
    import html
    import re

    text = html.escape(cell.strip())
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    return re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)


def table_html(header: list[str], align: list[str], rows: list[list[str]]) -> str:
    """An HTML table styled like Discord's dark theme, for render_html_to_png."""
    def row(cells: list[str], tag: str) -> str:
        return "<tr>" + "".join(
            f'<{tag} class="{a}">{_cell_html(c)}</{tag}>'
            for c, a in zip(cells, align + ["left"] * len(cells))
        ) + "</tr>"

    return (f"<style>{_TABLE_STYLE}</style><table>{row(header, 'th')}"
            + "".join(row(r, "td") for r in rows) + "</table>")


async def render_table_png(html: str) -> bytes:
    """Screenshot just the table in `html` (from table_html), at double resolution.

    Laid out on a wide page so columns take their natural width (cells wrap at
    420px), then cropped to the table, so there's no empty margin; 2x keeps the
    text sharp when a phone scales the image down.
    """
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        try:
            page = await browser.new_page(
                viewport={"width": _MAX_WIDTH, "height": 400}, device_scale_factor=2
            )
            await page.set_content(_TEMPLATE.format(body=html), wait_until="load")
            box = await page.locator("table").bounding_box()
            pad = 10
            clip = {"x": max(box["x"] - pad, 0), "y": max(box["y"] - pad, 0),
                    "width": min(box["width"] + 2 * pad, _MAX_WIDTH),
                    "height": min(box["height"] + 2 * pad, _MAX_HEIGHT)}
            return await page.screenshot(clip=clip, full_page=True)
        finally:
            await browser.close()
