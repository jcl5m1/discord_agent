"""Adapt agent Markdown to what Discord can display, and split long replies.

Discord renders a subset of Markdown: no tables, headings only down to ###, and
no horizontal rules. Agents write full GitHub-flavoured Markdown, so replies are
adapted before posting:

- tables: narrow ones become an aligned code block; wider ones become a list,
  one entry per row, because code blocks wrap and lose alignment on phones
- #### and deeper headings become bold lines; --- rules are dropped

Code blocks are left untouched. split_message() then breaks replies over the
2000-character limit at line boundaries, closing and reopening a code block
that spans two messages.
"""

from __future__ import annotations

import re

# Tables at most this wide (in characters) go into an aligned code block, which
# fits a phone screen without wrapping.
CODE_TABLE_MAX_WIDTH = 48

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


def _render_table(header: list[str], align: list[str], rows: list[list[str]]) -> list[str]:
    n = len(header)
    rows = [(r + [""] * n)[:n] for r in rows]
    plain = [[_plain(c) for c in r] for r in [header, *rows]]
    widths = [max(len(r[i]) for r in plain) for i in range(n)]
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


def adapt(text: str) -> str:
    """Rewrite Markdown that Discord can't display; code blocks are untouched."""
    lines = text.split("\n")
    out: list[str] = []
    fence: str | None = None
    i = 0
    while i < len(lines):
        line = lines[i]
        m = _FENCE_RE.match(line)
        if fence:
            out.append(line)
            if m and m.group(1) == fence:
                fence = None
            i += 1
            continue
        if m:
            fence = m.group(1)
            out.append(line)
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
            out += _render_table(header, align, rows)
            i = j
            continue
        if (h := _DEEP_HEADING_RE.match(line)):
            out.append(f"**{h.group(1)}**")
        elif not _RULE_RE.match(line):
            out.append(line)
        i += 1
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
