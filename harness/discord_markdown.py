"""Adapt agent Markdown to what Discord can display, and split long replies.

Discord renders a subset of Markdown: no tables, headings only down to ###, and
no horizontal rules. Agents write full GitHub-flavoured Markdown, so replies are
adapted before posting:

- tables: Discord code blocks wrap instead of scrolling, so an aligned table
  only stays aligned if it fits a phone (~32 characters): those become a code
  block. Wider tables of short values are rendered as an image when the caller
  collects them (see `adapt`), and otherwise, like tables of long prose, become
  a list with one entry per row. Tables an agent draws itself in a code block
  are rendered as images too when they're too wide for a phone.
- #### and deeper headings become bold lines; --- rules are dropped

Code blocks are left untouched. split_message() then breaks replies over the
2000-character limit at line boundaries, closing and reopening a code block
that spans two messages.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Tables at most this wide (in characters) go into an aligned code block, which
# fits a phone screen without wrapping (wider lines wrap and lose alignment).
CODE_TABLE_MAX_WIDTH = 32
# A table with any cell longer than this is prose: a list reads better on a phone
# than a scaled-down image.
PROSE_CELL = 60

_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_SEPARATOR_RE = re.compile(r"^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$")
_DEEP_HEADING_RE = re.compile(r"^\s{0,3}#{4,6}\s+(.*?)\s*#*\s*$")
_RULE_RE = re.compile(r"^\s{0,3}([-*_])(\s*\1){2,}\s*$")


def _cells(line: str) -> list[str]:
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|") and not line.endswith("\\|"):
        line = line[:-1]
    return [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", line)]


def _plain(cell: str) -> str:
    """Cell text without inline markup, for code blocks where it wouldn't render."""
    cell = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", cell)
    return re.sub(r"\*\*|__|`", "", cell)


def _alignments(separator: str) -> list[str]:
    out = []
    for cell in _cells(separator):
        cell = cell.strip()
        out.append("right" if cell.endswith(":") and not cell.startswith(":") else "left")
    return out


@dataclass
class Table:
    """A table to render as an image; `marker` is the line standing in for it."""

    header: list[str]
    align: list[str]
    rows: list[list[str]]
    marker: str = ""


def _table_width(header: list[str], rows: list[list[str]]) -> int:
    n = len(header)
    plain = [[_plain(c) for c in (r + [""] * n)[:n]] for r in [header, *rows]]
    return sum(max(len(r[i]) for r in plain) for i in range(n)) + 3 * (n - 1)


def _render_table(
    header: list[str], align: list[str], rows: list[list[str]], images: list[Table] | None = None
) -> list[str]:
    n = len(header)
    rows = [(r + [""] * n)[:n] for r in rows]
    plain = [[_plain(c) for c in r] for r in [header, *rows]]
    widths = [max(len(r[i]) for r in plain) for i in range(n)]
    prose = any(len(c) > PROSE_CELL for r in plain for c in r)
    if images is not None and not prose and sum(widths) + 3 * (n - 1) > CODE_TABLE_MAX_WIDTH:
        table = Table(header, align, rows, marker=f"📊 *table {len(images) + 1}, shown below*")
        images.append(table)
        return [table.marker]
    if sum(widths) + 3 * (n - 1) <= CODE_TABLE_MAX_WIDTH:
        def fmt(r):
            return "   ".join(
                c.rjust(w) if a == "right" else c.ljust(w)
                for c, w, a in zip(r, widths, align + ["left"] * n)
            ).rstrip()
        rule = "   ".join("─" * w for w in widths)
        return ["```", fmt(plain[0]), rule, *(fmt(r) for r in plain[1:]), "```"]

    def bold(cell: str) -> str:
        cell = cell.strip()
        if not cell:
            return "—"
        return cell if cell.startswith("**") and cell.endswith("**") else f"**{cell}**"

    lines = []
    if n == 2:  # key → value: the header becomes a caption
        lines.append(f"_{_plain(header[0])} → {_plain(header[1])}_")
        lines += [f"- {bold(r[0])}: {r[1] or '—'}" for r in rows]
    else:
        for r in rows:
            rest = " · ".join(f"{_plain(h)}: {c}" for h, c in zip(header[1:], r[1:]) if c)
            lines.append(f"- {bold(r[0])}" + (f" — {rest}" if rest else ""))
    return lines


# The rule under a hand-drawn table's header: dashes (or box/equals lines) in
# two or more groups separated by spaces, e.g. "-----  --------  ---".
_DRAWN_RULE_RE = re.compile(r"^\s*[-─=]{2,}(\s{1,}[-─=]{2,})+\s*$")


def _drawn_table(block: list[str]) -> tuple[list[str], list[list[str]]] | None:
    """Header and rows of a space-aligned table an agent drew in a code block."""
    lines = [l.rstrip() for l in block if l.strip()]
    if len(lines) < 3 or not _DRAWN_RULE_RE.match(lines[1]):
        return None
    spans = [m.span() for m in re.finditer(r"[-─=]+", lines[1])]
    starts = [s for s, _ in spans] + [10**6]

    def split(line: str) -> list[str]:
        return [line[starts[k]:starts[k + 1]].strip() for k in range(len(spans))]

    rows = [split(l) for l in lines[2:]]
    # Text crossing a column boundary means the guess is wrong: leave it alone.
    for line in lines[2:]:
        for s in starts[1:-1]:
            if 0 < s < len(line) and line[s - 1] != " " and line[s] != " ":
                return None
    return split(lines[0]), rows


def adapt(text: str, images: list[Table] | None = None) -> str:
    """Rewrite Markdown that Discord can't display.

    With `images`, tables too wide for a phone are appended to it as Table
    objects (to render as pictures) and replaced by a marker line. Code blocks
    are untouched, except hand-drawn tables in them when `images` is given.
    """
    lines = text.split("\n")
    out: list[str] = []
    fence: str | None = None  # "```" or "~~~" while inside a code block
    opener = ""  # that block's opening line, as written
    block: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        m = _FENCE_RE.match(line)
        if fence:
            if m and m.group(1) == fence and line.strip() == fence:
                drawn = _drawn_table(block) if images is not None else None
                if drawn and _table_width(*drawn) > CODE_TABLE_MAX_WIDTH:
                    header, rows = drawn
                    table = Table(header, ["left"] * len(header), rows,
                                  marker=f"📊 *table {len(images) + 1}, shown below*")
                    images.append(table)
                    out.append(table.marker)
                else:
                    out += [opener, *block, line]
                fence, block = None, []
            else:
                block.append(line)
            i += 1
            continue
        if m:
            fence, opener = m.group(1), line
            i += 1
            continue
        if (
            "|" in line
            and i + 1 < len(lines)
            and "|" in lines[i + 1]
            and _SEPARATOR_RE.match(lines[i + 1])
            and len(_cells(lines[i + 1])) == len(_cells(line))
        ):
            header, align = _cells(line), _alignments(lines[i + 1])
            j = i + 2
            rows = []
            while j < len(lines) and "|" in lines[j] and lines[j].strip():
                rows.append(_cells(lines[j]))
                j += 1
            out += _render_table(header, align, rows, images)
            i = j
            continue
        if (h := _DEEP_HEADING_RE.match(line)):
            out.append(f"**{h.group(1)}**")
        elif not _RULE_RE.match(line):
            out.append(line)
        i += 1
    if fence:  # unclosed code block: keep it as it was
        out += [opener, *block]
    return "\n".join(out)


def split_message(text: str, limit: int = 2000) -> list[str]:
    """Split into chunks of at most limit chars at line breaks.

    A code block cut by a split is closed at the end of one chunk and reopened
    (same language) at the start of the next, so both render correctly.
    """
    if len(text) <= limit:
        return [text]
    # Lines too long for any chunk are cut first; the margin leaves room for a
    # reopened fence line and a closing fence around them.
    piece = max(limit - 100, limit // 2)
    lines = []
    for line in text.split("\n"):
        lines += [line[k : k + piece] for k in range(0, len(line), piece)] or [""]

    chunks: list[str] = []
    current: list[str] = []
    opener: str | None = None  # fence line of the code block we're inside

    def closing() -> str:
        return "\n" + _FENCE_RE.match(opener).group(1) if opener else ""

    for line in lines:
        candidate = "\n".join([*current, line])
        if current and len(candidate) + len(closing()) > limit:
            body = "\n".join(current) + closing()
            if body.strip():
                chunks.append(body)
            current = [opener] if opener else []
        current.append(line)
        m = _FENCE_RE.match(line)
        if m:
            opener = None if opener else line.strip()
    body = "\n".join(current)
    if body.strip() and body != opener:
        chunks.append(body)
    return chunks
