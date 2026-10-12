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
