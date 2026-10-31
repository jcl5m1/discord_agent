"""Per-instance shared files: one persistent folder per channel where its agent
keeps charts, logs, reports and data for that discussion, served read-only at a
stable public URL.

    data/files/<instance-slug>/<path>
        -> {PUBLIC_BASE_URL}/agents/<instance-slug>/<token>/files/<path>

The token is the instance's web token (the same one that guards its live web
pages), so links are public but unguessable. Image links posted in Discord show
inline. A directory URL (including the root) returns an index page.
"""

from __future__ import annotations

import html
import re
import time
from dataclasses import dataclass
from pathlib import Path

from . import config

FILES_DIR = config.DATA_DIR / "files"


def slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9-_]", "-", name.lower()).strip("-")
    return s or "agent"


def files_dir(instance_slug: str) -> Path:
    return FILES_DIR / instance_slug


def files_url(instance_slug: str, token: str) -> str | None:
    if not config.PUBLIC_BASE_URL:
        return None
    return f"{config.PUBLIC_BASE_URL}/agents/{instance_slug}/{token}/files/"


def resolve(instance_slug: str, rel: str) -> Path | None:
    """Path inside the instance's folder, or None if it would escape it."""
    root = files_dir(instance_slug).resolve()
    target = (root / rel.lstrip("/")).resolve()
    if target != root and not target.is_relative_to(root):
        return None
    return target


@dataclass
class Entry:
    path: str  # relative to the instance folder, '/'-separated
    size: int
    mtime: float


def listing(instance_slug: str, sub: str = "") -> list[Entry]:
    """Files under the folder (or a subfolder), newest first."""
    base = resolve(instance_slug, sub)
    root = files_dir(instance_slug).resolve()
    if base is None or not base.is_dir():
        return []
    out = []
    for p in base.rglob("*"):
        if p.is_file() and not any(part.startswith(".") for part in p.relative_to(root).parts):
            st = p.stat()
            out.append(Entry(p.relative_to(root).as_posix(), st.st_size, st.st_mtime))
    return sorted(out, key=lambda e: e.mtime, reverse=True)


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def index_html(instance_slug: str, sub: str, base_url: str) -> str:
    """Directory index: base_url is the .../files/ URL prefix."""
    entries = listing(instance_slug, sub)
    rows = "\n".join(
        f'<tr><td><a href="{html.escape(base_url + e.path)}">{html.escape(e.path)}</a></td>'
        f"<td>{human_size(e.size)}</td>"
        f"<td>{time.strftime('%Y-%m-%d %H:%M', time.localtime(e.mtime))}</td></tr>"
        for e in entries
    ) or '<tr><td colspan="3">(empty)</td></tr>'
    title = html.escape(f"{instance_slug} files" + (f" / {sub}" if sub else ""))
    return (
        f"<!doctype html><meta charset=utf-8><title>{title}</title>"
        "<style>body{font:14px system-ui;margin:2em}td{padding:2px 12px 2px 0}"
        "a{text-decoration:none}</style>"
        f"<h2>{title}</h2><table><tr><th align=left>file</th><th align=left>size</th>"
        f"<th align=left>modified</th></tr>{rows}</table>"
    )
