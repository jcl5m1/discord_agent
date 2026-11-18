#!/usr/bin/env python3
"""Live render test for gdoc: does Markdown come out right in a real Google Doc?

Publishes a sample report (every Markdown feature reports use) into a scratch Doc in the reports folder, reads the
Doc back with the Docs API and checks each block: headings, paragraphs, bullet and numbered lists with their nesting
levels, code, quotes, tables and links. Then it updates the same Doc in place to a changed version (gdoc_sync, the
path every re-publish takes) and checks again. The scratch Doc is deleted at the end. Needs the gdoc setup
(`gdoc check`) and network access, so it is not part of the unit tests:

  .venv/bin/python tests/gdoc_render_live.py [--keep]     # exit 0 when every block renders as expected
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from harness import gdoc  # noqa: E402

# (expected kind, text the block starts with). Kinds: h1 h2 h3 p, ul<level> ol<level> (bullet / numbered list item at
# that nesting level), code, quote, table (a table whose first cell starts with the text).
SAMPLE_A = """# Render test

A paragraph with **bold**, *italic*, `inline code` and a [link](https://example.com).
A list right after a paragraph line:
- first bullet
- second bullet
  - nested bullet (2 spaces)
    - nested twice (4 spaces)
- third bullet

## Numbered

1. step one
2. step two
   - a bullet inside a numbered item
3. step three

### Code, quote and table

```
code line 1
code line 2 x=$A y=$B
```

> a quoted line

| Name | Value |
|---|---|
| alpha | 1 |
| beta | 2 |

- **Bold start:** text after a bold label, with a continuation
  line indented by two spaces.
- last item

Prices: $0.20 and $0.40/h, as `x=$A y=$B` and x=$A y=$B.
"""

# The two lines of the first paragraph are one paragraph (Markdown joins a paragraph's lines).
EXPECT_A = [("h1", "Render test"), ("p", "A paragraph with"),
            ("ul0", "first bullet"), ("ul0", "second bullet"), ("ul1", "nested bullet"), ("ul2", "nested twice"),
            ("ul0", "third bullet"), ("h2", "Numbered"), ("ol0", "step one"), ("ol0", "step two"),
            # One preset per Doc list: a bullet nested in a numbered list is numbered too (a., b.); splitting the list
            # would restart its numbering.
            ("ol1", "a bullet inside"), ("ol0", "step three"), ("h3", "Code, quote and table"), ("code", "code line 1"),
            ("quote", "a quoted line"), ("table", "Name"), ("ul0", "Bold start:"), ("ul0", "last item"),
            ("p", "Prices: $0.20 and $0.40/h, as x=$A y=$B and x=$A y=$B.")]

# Version B: items added, removed and re-nested, as a report changes between publishes.
SAMPLE_B = SAMPLE_A.replace("- second bullet\n  - nested bullet (2 spaces)\n",
                            "- second bullet, changed\n  - nested bullet (2 spaces)\n  - a new nested bullet\n") \
                   .replace("- third bullet\n", "- third bullet\n- a new fourth bullet\n") \
                   .replace("2. step two\n   - a bullet inside a numbered item\n", "2. step two\n")
EXPECT_B = [("h1", "Render test"), ("p", "A paragraph with"),
            ("ul0", "first bullet"), ("ul0", "second bullet, changed"), ("ul1", "nested bullet"),
            ("ul1", "a new nested bullet"), ("ul2", "nested twice"), ("ul0", "third bullet"),
            ("ul0", "a new fourth bullet"), ("h2", "Numbered"), ("ol0", "step one"), ("ol0", "step two"),
            ("ol0", "step three"), ("h3", "Code, quote and table"), ("code", "code line 1"), ("quote", "a quoted line"),
            ("table", "Name"), ("ul0", "Bold start:"), ("ul0", "last item"),
            ("p", "Prices: $0.20 and $0.40/h, as x=$A y=$B and x=$A y=$B.")]


EQ = "<equation>"  # the import makes "$x ... y=$" an equation unless prevented


def blocks(doc: dict) -> list[tuple[str, str]]:
    """The Doc body as (kind, text) blocks, in order."""
    lists = doc.get("lists", {})
    out = []
    for el in doc["body"]["content"]:
        if "table" in el:
            cell = el["table"]["tableRows"][0]["tableCells"][0]["content"][0]["paragraph"]
            out.append(("table", "".join(r.get("textRun", {}).get("content", "") for r in cell["elements"]).strip()))
            continue
        p = el.get("paragraph")
        if not p:
            continue
        text = "".join(EQ if "equation" in r else r.get("textRun", {}).get("content", "") for r in p["elements"]).strip()
        if not text:
            continue
        style = p.get("paragraphStyle", {})
        named = style.get("namedStyleType", "NORMAL_TEXT")
        runs = [r["textRun"] for r in p["elements"] if "textRun" in r]
        mono = runs and all("Courier" in (r.get("textStyle", {}).get("weightedFontFamily", {}) or {}).get("fontFamily", "")
                            or "Mono" in (r.get("textStyle", {}).get("weightedFontFamily", {}) or {}).get("fontFamily", "")
                            for r in runs)
        if "bullet" in p:
            level = p["bullet"].get("nestingLevel", 0)
            levels = lists.get(p["bullet"]["listId"], {}).get("listProperties", {}).get("nestingLevels", [])
            glyph = levels[level].get("glyphType", "") if level < len(levels) else ""
            ordered = bool(glyph) and glyph not in ("GLYPH_TYPE_UNSPECIFIED", "NONE")
            out.append((f"{'ol' if ordered else 'ul'}{level}", text))
        elif named.startswith("HEADING_"):
            out.append((f"h{named[-1]}", text))
        elif named == "TITLE":
            out.append(("h1", text))
        elif mono:
            out.append(("code", text))
        elif style.get("indentStart", {}).get("magnitude", 0) > 0 or style.get("borderLeft", {}).get("width", {}).get("magnitude", 0) > 0:
            out.append(("quote", text))
        else:
            out.append(("p", text))
    return out


def compare(got: list[tuple[str, str]], want: list[tuple[str, str]], label: str) -> int:
    """Each expected block must appear in order; prints every mismatch. Returns the number of problems."""
    problems, i = 0, 0
    for kind, start in want:
        j = next((k for k in range(i, len(got)) if got[k][1].startswith(start)), None)
        if j is None:
            print(f"  [{label}] MISSING {kind:5s} {start!r}")
            problems += 1
            continue
        if got[j][0] != kind:
            print(f"  [{label}] WRONG   {start!r}: expected {kind}, got {got[j][0]}")
            problems += 1
        i = j + 1
    joined = "\n".join(t for _, t in got)
    for line in joined.splitlines():
        if EQ in line:
            print(f"  [{label}] EQUATION made by the import: {line!r}")
            problems += 1
    for marker in ("- second", "- first", "1. step", "  - nested"):
        if marker in joined:
            print(f"  [{label}] RAW MARKDOWN left in the text: {marker!r}")
            problems += 1
    return problems


def page(md: str, title: str) -> str:
    return f"<html><head><meta charset='utf-8'><title>{title}</title></head><body>{gdoc._to_html(md)}</body></html>"


def main() -> int:
    keep = "--keep" in sys.argv
    title = f"gdoc render test {time.strftime('%Y%m%d-%H%M%S')}"
    meta = {"name": title, "mimeType": gdoc.DOC_MIME, "parents": [gdoc._folder_id()]}
    doc = gdoc._upload("POST", f"{gdoc.UPLOAD}/files?uploadType=multipart&fields=id", meta, page(SAMPLE_A, title))["id"]
    gdoc.gdoc_sync.normalize_lists(gdoc._docs_api, doc, gdoc.list_kinds(SAMPLE_A))  # as publish does
    print(f"scratch Doc https://docs.google.com/document/d/{doc}/edit")
    try:
        get = lambda: gdoc._docs_api("GET", f"https://docs.googleapis.com/v1/documents/{doc}", None)  # noqa: E731
        n_a = compare(blocks(get()), EXPECT_A, "new Doc")
        edits = gdoc._update_in_place(doc, title, page(SAMPLE_B, title), gdoc.list_kinds(SAMPLE_B))
        n_b = compare(blocks(get()), EXPECT_B, f"in-place update, {edits} edits")
        print(f"new Doc: {n_a} problems; in-place update: {n_b} problems")
        return 1 if n_a or n_b else 0
    finally:
        if not keep:
            gdoc._api("DELETE", f"{gdoc.API}/files/{doc}")


if __name__ == "__main__":
    sys.exit(main())
