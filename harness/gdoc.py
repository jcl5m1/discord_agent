"""Publish a Markdown report as a Google Doc, and read and answer its comments.

  gdoc publish report.md [--title "..."] [--agent <worker>]   # prints the Doc link
  gdoc list
  gdoc comments report.md [--all]          # open comment threads, as Markdown (or a Doc id or link)
  gdoc reply <doc> <comment id> "text" [--resolve]
  gdoc check                               # is Google Drive and the Docs API set up?

One Doc per report, for good. The first publish creates the Doc (in the Drive
folder GDOC_FOLDER); every later publish updates that same Doc in place with the
Google Docs API (gdoc_sync), so only changed text is edited and the link, the
sharing and the comments on unchanged text persist. Publish never makes a second
Doc for a report: it stops with an error if the Doc can't be read. `--doc` binds
an existing Doc to a report; `--replace` rewrites the whole body (only when the
user asks: comments lose their anchors).

Each Doc gets a header with the authors (git), the agents, and the created and
last-updated times. Images are embedded, scaled to the page width; a <video>
becomes a link to a copy on Drive. Links to other reports' .md files, and report
file names in the text, become links to their Docs. Each publish shares the Doc
with the people in GDOC_SHARE_FILE (one "email [role]" per line; no email is sent).

Access is the rclone remote GDOC_REMOTE (Google Drive, scope drive.file, with
the owner's own OAuth client). That client's Google Cloud project needs the
Drive API and the Google Docs API enabled. Settings (.env or environment):
GDOC_REMOTE, GDOC_FOLDER, GDOC_REGISTRY, GDOC_SHARE_FILE, GDOC_WEB_REGISTRY.
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import mimetypes
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from markdown_it import MarkdownIt
from markdown_it.common.utils import escapeHtml
from PIL import Image

from . import config, gdoc_sync

REGISTRY = config.GDOC_REGISTRY
REMOTE = config.GDOC_REMOTE
FOLDER = config.GDOC_FOLDER
API = "https://www.googleapis.com/drive/v3"
UPLOAD = "https://www.googleapis.com/upload/drive/v3"
DOC_MIME = "application/vnd.google-apps.document"
IMAGE = re.compile(r'<img\b[^>]*>')
SIZE_ATTR = re.compile(r'\s(?:width|height|style)="[^"]*"')
PAGE_PX = 624  # the text width of a Letter page with 1-inch margins (6.5 in at 96 px per inch)
MAX_PX = 1600  # embedded images wider than this are downscaled, to keep the Doc small
VIDEO = re.compile(r'<video\b([^>]*)>(?:.*?</video>)?', re.DOTALL)
ATTR = re.compile(r'\b(src|poster)="([^"]+)"')
DOC_ID = re.compile(r"^[\w-]{25,}$")
ANCHOR = re.compile(r'(<a\b[^>]*\bhref=")([^"]+)("[^>]*>)(.*?)(</a>)', re.DOTALL)
SEGMENT = re.compile(r'(<a\b.*?</a>|<[^>]+>)', re.DOTALL)  # anchors and tags; the text between is split off
MD_NAME = re.compile(r"[\w./-]*\b[\w-]+\.md\b")
# Optional: self-hosted report pages (/<anything>/reports/<id>/) and their registry of sources, so links to
# them become links to the Docs (GDOC_WEB_REGISTRY: a JSON object {id: {"source": path}}).
WEB_REPORT = re.compile(r"/reports/([\w-]{20,64})/?")
WEB_REGISTRY = config.GDOC_WEB_REGISTRY
# People every published Doc is shared with: one "email [role]" per line (role: writer, commenter or reader;
# default writer). Kept outside the repo, because it holds personal addresses.
DOCS_API_CONSOLE = "https://console.cloud.google.com/apis/library/docs.googleapis.com"
SHARE_FILE = config.GDOC_SHARE_FILE


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("publish")
    p.add_argument("source", type=Path)
    p.add_argument("--title")
    p.add_argument("--replace", action="store_true",
                   help="replace the whole Doc body (comments lose their anchors); only when the user asks for it")
    p.add_argument("--doc", help="bind an existing Doc (id or link) to this source; later publishes update it")
    p.add_argument("--agent", action="append", default=[],
                   help="an agent that wrote the report (repeat for several; default on a first publish: $WORKLOG_WORKER)")
    sub.add_parser("list")
    c = sub.add_parser("comments")
    c.add_argument("doc", help="a Doc id, a Doc link, or a published source file")
    c.add_argument("--all", action="store_true", help="also resolved threads")
    r = sub.add_parser("reply")
    r.add_argument("doc")
    r.add_argument("comment")
    r.add_argument("text")
    r.add_argument("--resolve", action="store_true")
    sub.add_parser("check")
    a = ap.parse_args(argv)
    if a.cmd == "check":
        problems = check()
        print("\n".join(problems) if problems else f"ok: Google Drive ({REMOTE}:{FOLDER}) and the Docs API work")
        raise SystemExit(1 if problems else 0)
    reg = json.loads(REGISTRY.read_text()) if REGISTRY.exists() else {}
    if a.cmd == "publish":
        src = a.source.resolve()
        if a.doc:
            _bind(src, _doc_id(a.doc, {}), reg)
        # A re-publish (e.g. for layout) does not credit the publisher: $WORKLOG_WORKER counts only on a first publish.
        first = not (reg.get(str(src)) or {}).get("agents")
        agents = a.agent or [x for x in [os.environ.get("WORKLOG_WORKER", "")] if x and first]
        print(publish(src, a.title, reg, agents, a.replace))
    elif a.cmd == "list":
        for src, d in sorted(reg.items(), key=lambda kv: kv[1]["updated"]):
            print(f"updated {d['updated']}  {d['url']}  {d['title']}  ({src})")
    elif a.cmd == "comments":
        print(comments_markdown(_doc_id(a.doc, reg), a.all))
    else:
        body: dict = {"content": a.text}
        if a.resolve:
            body["action"] = "resolve"
        _api("POST", f"{API}/files/{_doc_id(a.doc, reg)}/comments/{a.comment}/replies?fields=id", body)
        print("replied" + (" and resolved" if a.resolve else ""))


def publish(src: Path, title: str | None, reg: dict, agents: list[str], replace: bool = False) -> str:
    text = src.read_text()
    prev = reg.get(str(src)) or {}
    title = title or prev.get("title") or _first_heading(text) or src.stem
    videos = dict(prev.get("videos", {}))
    html = _to_html(text)
    html = VIDEO.sub(lambda m: _video_link(m, src, videos), html)
    html = IMAGE.sub(lambda m: _fit_image(src, m.group(0)), html)
    html = _link_reports(src, html, reg)
    changed = _last_change(src)
    created = _first_commit(src) or changed
    # Worker names without the machine: "placeframe-worker-3@host/sub-2" -> "placeframe-worker-3/sub-2".
    agents = list(dict.fromkeys([*prev.get("agents", []), *(re.sub(r"@[^/]+", "", x) for x in agents)]))
    header = _header(_git_authors(src), agents, created, changed)
    html = html.replace("</h1>", "</h1>" + header, 1) if "</h1>" in html else header + html
    page = f"<html><head><meta charset='utf-8'><title>{title}</title></head><body>{html}</body></html>"
    # A source with a Doc always updates that Doc: its link, sharing and comments must persist. A Doc is created only
    # on a first publish, and never when one with the same title already exists in the reports folder.
    doc = prev.get("id")
    if doc:
        _check_doc(doc, src)
        if replace:
            _upload("PATCH", f"{UPLOAD}/files/{doc}?uploadType=multipart&fields=id", {"name": title}, page)
            print(f"replaced the whole body of Doc {doc} (--replace)", file=sys.stderr)
        else:
            edits = _update_in_place(doc, title, page, list_kinds(text))
            print(f"updated Doc {doc} in place: {edits} edits", file=sys.stderr)
    else:
        folder = _folder_id()
        same = _docs_titled(title, folder)
        if same:
            raise SystemExit(f"a Doc titled {title!r} already exists in {FOLDER} ({', '.join(same)}), but no Doc is "
                             f"recorded for {src}. Bind it, then publish again: publish {src} --doc <id or link>")
        meta = {"name": title, "mimeType": DOC_MIME, "parents": [folder]}
        doc = _upload("POST", f"{UPLOAD}/files?uploadType=multipart&fields=id", meta, page)["id"]
        gdoc_sync.normalize_lists(_docs_api, doc, list_kinds(text))
        print(f"created new Doc {doc} for {src}", file=sys.stderr)
    _share(doc)
    now = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    reg[str(src)] = {"id": doc, "url": f"https://docs.google.com/document/d/{doc}/edit", "title": title,
                     "created": prev.get("created", now),
                     "updated": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(changed)),
                     "published": now, "agents": agents, "videos": videos}
    REGISTRY.parent.mkdir(parents=True, exist_ok=True)
    tmp = REGISTRY.with_suffix(".tmp")
    tmp.write_text(json.dumps(reg, indent=1))
    tmp.replace(REGISTRY)
    return reg[str(src)]["url"]


# CommonMark (as on GitHub), with tables and strikethrough: a list may follow a paragraph line without a blank line,
# and nested list items need only be indented to their parent's text (2 spaces after "- "). Python-Markdown, used
# before, needs a blank line before a list and 4-space nesting, so most report lists came out as text or flat.
# The Drive import keeps a font set on a block or a span, not on <code>, and shows a bare <blockquote> as plain text.
# Unquoted and without a fallback: with "'Courier New',monospace" some imports set every code span to Arial.
CODE_FONT = "font-family:Courier New"
DOLLAR = '<span style="vertical-align:baseline">$</span>'
MARKDOWN = MarkdownIt("commonmark").enable(["table", "strikethrough"])
MARKDOWN.add_render_rule(  # inline code as a monospace span (the Drive import drops the font of <code>)
    "code_inline", lambda self, tokens, idx, options, env:
    f'<span style="{CODE_FONT}">{escapeHtml(tokens[idx].content)}</span>')


def _to_html(text: str) -> str:
    """The report's Markdown as HTML for the Drive import (which turns it into Doc paragraphs, lists and tables)."""
    html = MARKDOWN.render(text)
    html = html.replace("<pre><code", f'<pre style="{CODE_FONT}"><code')
    # The import makes "$x ... y=$" an equation, in code too: a styled span around each $ prevents it.
    html = re.sub(r">([^<]*)<", lambda m: ">" + m.group(1).replace("$", DOLLAR) + "<", html)
    # A link to a section (#anchor) imports as a link to a bookmark that does not exist: keep only its text.
    html = re.sub(r'<a href="#[^"]*">(.*?)</a>', r"\1", html, flags=re.S)
    # A <th> row imports as a repeated header row, not bold, with an extra blank line in exports: use bold cells.
    html = re.sub(r"<th( style=\"[^\"]*\")?>(.*?)</th>", r"<td\1><b>\2</b></td>", html, flags=re.S)
    html = re.sub(r"</?thead>\n?", "", html)
    return html.replace("<blockquote>", '<blockquote style="margin-left:24pt;border-left:3pt solid #c8c8c8;'
                                        'padding-left:9pt;color:#555555">')


def list_kinds(text: str) -> list[tuple[int, int, bool]]:
    """One (list number, nesting level, numbered) per list item of the Markdown, in order. The number counts
    top-level lists; numbered is the type of the item's top-level list (one preset per Doc list). The Drive import
    drops list types, so gdoc_sync.normalize_lists rebuilds the lists with these."""
    out, stack, top = [], [], -1
    for t in MARKDOWN.parse(text):
        if t.type in ("bullet_list_open", "ordered_list_open"):
            if not stack:
                top += 1
            stack.append(t.type == "ordered_list_open")
        elif t.type in ("bullet_list_close", "ordered_list_close"):
            stack.pop()
        elif t.type == "list_item_open":
            out.append((top, len(stack) - 1, stack[0]))
    return out


def _header(people: list[str], agents: list[str], created: float, changed: float) -> str:
    fmt = "%Y-%m-%d %H:%M %Z"
    lines = []
    if people:
        lines.append(f"<b>Authors:</b> {', '.join(people)}")
    if agents:
        lines.append(f"<b>Written by agents:</b> {', '.join(agents)}")
    lines.append(f"<b>Created:</b> {time.strftime(fmt, time.localtime(created))} · "
                 f"<b>Last updated:</b> {time.strftime(fmt, time.localtime(changed))}")
    return "<p>" + "<br>".join(lines) + "</p>"


def _git_authors(src: Path) -> list[str]:
    # Commit authors, then co-authors from the Co-Authored-By trailers (names only, no email addresses).
    out = subprocess.run(["git", "-C", str(src.parent), "log", "--reverse",
                          "--format=%an%n%(trailers:key=Co-Authored-By,valueonly,separator=%x0A)", "--", src.name],
                         capture_output=True, text=True, check=False).stdout
    names = [re.sub(r"\s*<[^>]*>", "", x).strip() for x in out.splitlines()]
    return list(dict.fromkeys(x for x in names if x))


def _first_commit(src: Path) -> float | None:
    out = subprocess.run(["git", "-C", str(src.parent), "log", "--follow", "--format=%ct", "--", src.name],
                         capture_output=True, text=True, check=False).stdout.split()
    return float(out[-1]) if out else None


def _last_change(src: Path) -> float:
    # The report's own last change: its last commit, or the file's save time while it has uncommitted edits.
    # A re-publish for layout alone does not move it.
    git = ["git", "-C", str(src.parent)]
    dirty = subprocess.run([*git, "status", "--porcelain", "--", src.name], capture_output=True, text=True, check=False)
    if dirty.returncode == 0 and not dirty.stdout.strip():
        log = subprocess.run([*git, "log", "-1", "--format=%ct", "--", src.name], capture_output=True, text=True, check=False)
        if log.stdout.strip():
            return float(log.stdout.strip())
    return src.stat().st_mtime


def _share(doc: str) -> None:
    try:
        lines = [x.split() for x in SHARE_FILE.read_text().splitlines() if x.strip() and not x.startswith("#")]
    except FileNotFoundError:
        return
    have = {p.get("emailAddress", "").lower()
            for p in _api("GET", f"{API}/files/{doc}/permissions?fields=permissions(emailAddress)")["permissions"]}
    for email, *role in lines:
        if email.lower() not in have:
            _api("POST", f"{API}/files/{doc}/permissions?sendNotificationEmail=false&fields=id",
                 {"type": "user", "role": role[0] if role else "writer", "emailAddress": email})


def comments_markdown(doc: str, include_resolved: bool) -> str:
    fields = "comments(id,author/displayName,content,createdTime,resolved,quotedFileContent/value," \
             "replies(author/displayName,content,createdTime,action)),nextPageToken"
    threads, token = [], ""
    while True:
        page = _api("GET", f"{API}/files/{doc}/comments?pageSize=100&fields={fields}"
                    + (f"&pageToken={token}" if token else ""))
        threads += page.get("comments", [])
        token = page.get("nextPageToken", "")
        if not token:
            break
    meta = _api("GET", f"{API}/files/{doc}?fields=name,webViewLink")
    lines = [f"# Comments on: {meta['name']}", f"Doc: {meta['webViewLink']}", ""]
    shown = [t for t in threads if include_resolved or not t.get("resolved")]
    for t in shown:
        quote = (t.get("quotedFileContent") or {}).get("value", "").strip()
        lines += [f"## Comment {t['id']}" + (" (resolved)" if t.get("resolved") else ""),
                  f"> {quote}" if quote else "> (no quoted text)", "",
                  f"- **{t['author']['displayName']}** ({t['createdTime']}): {t['content']}"]
        for r in t.get("replies", []):
            act = f" [{r['action']}]" if r.get("action") else ""
            lines.append(f"- **{r['author']['displayName']}** ({r['createdTime']}){act}: {r.get('content', '')}")
        lines.append("")
    if not shown:
        lines.append("No open comments." if threads else "No comments yet.")
    return "\n".join(lines)


def _doc_id(doc: str, reg: dict) -> str:
    m = re.search(r"/document/d/([\w-]+)", doc)
    if m:
        return m.group(1)
    path = str(Path(doc).resolve())
    if path in reg:
        return reg[path]["id"]
    if DOC_ID.match(doc):
        return doc
    raise SystemExit(f"not a Doc id, Doc link or published source: {doc}")


def _link_reports(src: Path, html: str, reg: dict) -> str:
    # In a Doc, other reports are Google Docs: a link to a report's .md file (or its self-hosted page) goes to its
    # Doc, a link text that is a file name becomes the Doc title, and a report file named in plain text becomes a
    # link to its Doc. The report's own file name becomes its title.
    def anchor(m: re.Match) -> str:
        href = _doc_link(src, m.group(2), reg)
        inner = m.group(4)
        target = next((d for d in reg.values() if d["url"] == href), None)
        if target and MD_NAME.fullmatch(re.sub(r"<[^>]+>", "", inner).strip()):
            inner = target["title"]
        return m.group(1) + href + m.group(3) + inner + m.group(5)

    def mention(m: re.Match) -> str:
        target = _report_for(src, m.group(0), reg)
        if target is None:
            if (src.parent / m.group(0)).is_file():
                print(f"note: {m.group(0)} is named in the text but has no Google Doc yet: run publish on it first")
            return m.group(0)
        if target == str(src):
            return reg.get(target, {}).get("title") or m.group(0)
        return f'<a href="{reg[target]["url"]}">{reg[target]["title"]}</a>'

    html = ANCHOR.sub(anchor, html)
    html = "".join(part if SEGMENT.fullmatch(part) else MD_NAME.sub(mention, part) for part in SEGMENT.split(html))
    return re.sub(r'<code>(<a href="https://docs\.google\.com/[^"]+">[^<]*</a>)</code>', r"\1", html)


def _report_for(src: Path, name: str, reg: dict) -> str | None:
    # A path relative to the report, else a unique match on the file name among published reports.
    path = str(Path(os.path.normpath(src.parent / name)))
    if path in reg or path == str(src):
        return path
    matches = [k for k in reg if Path(k).name == Path(name).name and k.endswith(name.lstrip("./"))]
    if len(matches) == 1:
        return matches[0]
    if Path(name).name == src.name:
        return str(src)
    return None


def _doc_link(src: Path, href: str, reg: dict) -> str:
    # A link to another report goes to that report's Google Doc: a relative link to its Markdown file, or a link to
    # its self-hosted web page. Other links are unchanged. A linked report with no Doc yet keeps its link (warned).
    web = WEB_REPORT.search(href) if WEB_REGISTRY else None
    if web:
        try:
            target = (json.loads(WEB_REGISTRY.read_text()).get(web.group(1)) or {}).get("source")
        except (FileNotFoundError, ValueError):
            target = None
    elif "://" in href or href.startswith(("#", "mailto:")):
        return href
    else:
        path = urllib.parse.unquote(href.split("#")[0])
        target = str(Path(os.path.normpath(src.parent / path))) if path.endswith(".md") else None
    if not target:
        return href
    if target in reg:
        return reg[target]["url"]
    if Path(target) != src:
        print(f"note: {href} links to a report with no Google Doc yet: run publish {target}")
    return href


def _video_link(m: re.Match, src: Path, videos: dict) -> str:
    attrs = dict(ATTR.findall(m.group(1)))
    ref = attrs.get("src")
    if not ref:
        return ""
    url = ref if "://" in ref else _drive_copy(src, ref, videos)
    poster = f'<p><img src="{attrs["poster"]}"></p>' if attrs.get("poster") else ""
    return f'{poster}<p>▶ <a href="{url}">Video: {Path(ref).name}</a></p>'


def _drive_copy(src: Path, ref: str, videos: dict) -> str:
    asset = Path(os.path.normpath(src.parent / ref))
    if not asset.is_file():
        return ref
    st = asset.stat()
    version = f"{st.st_size}:{int(st.st_mtime)}"
    if (videos.get(ref) or {}).get("version") != version:
        dest = f"{REMOTE}:{FOLDER}/media/{src.stem}/{Path(ref).name}"
        subprocess.run(["rclone", "copyto", str(asset.resolve()), dest], check=True)
        url = subprocess.run(["rclone", "link", dest], check=True, capture_output=True, text=True).stdout.strip()
        videos[ref] = {"version": version, "url": url}
    return videos[ref]["url"]


def _fit_image(src: Path, tag: str) -> str:
    m = ATTR.search(tag)
    if not m or m.group(1) != "src":
        return tag
    uri, size = _data_uri(src, m.group(2))
    tag = SIZE_ATTR.sub("", tag.replace(m.group(0), f'src="{uri}"'))
    if size is None:
        return tag.replace("<img", f'<img width="{PAGE_PX}"', 1)
    w, h = size
    scale = min(1.0, PAGE_PX / w)
    return tag.replace("<img", f'<img width="{round(w * scale)}" height="{round(h * scale)}"', 1)


def _data_uri(src: Path, ref: str) -> tuple[str, tuple[int, int] | None]:
    if "://" in ref or ref.startswith("data:"):
        return ref, None
    asset = Path(os.path.normpath(src.parent / urllib.parse.unquote(ref)))
    if not asset.is_file():
        return ref, None
    mime = mimetypes.guess_type(asset.name)[0] or "image/png"
    data = asset.read_bytes()
    try:
        with Image.open(io.BytesIO(data)) as im:
            size = im.size
            if im.width > MAX_PX and im.format in ("PNG", "JPEG"):
                small = im.resize((MAX_PX, round(im.height * MAX_PX / im.width)), Image.Resampling.LANCZOS)
                buf = io.BytesIO()
                small.save(buf, format=im.format, **({"quality": 90} if im.format == "JPEG" else {}))
                data = buf.getvalue()
    except (OSError, ValueError):
        size = None
    return f"data:{mime};base64,{base64.b64encode(data).decode()}", size


def _folder_id() -> str:
    parent = "root"
    for name in FOLDER.split("/"):
        q = urllib.parse.quote(f"name = '{name}' and '{parent}' in parents and trashed = false "
                               "and mimeType = 'application/vnd.google-apps.folder'")
        found = _api("GET", f"{API}/files?q={q}&fields=files(id)").get("files", [])
        parent = found[0]["id"] if found else _api(
            "POST", f"{API}/files?fields=id",
            {"name": name, "mimeType": "application/vnd.google-apps.folder", "parents": [parent]})["id"]
    return parent


def _update_in_place(doc: str, title: str, page: str, kinds: list | None = None) -> int:
    # The new version goes into a temporary Doc (Drive's HTML import does the formatting); gdoc_sync then edits the
    # published Doc to match it, touching only what changed. The temporary Doc is deleted in every case.
    _docs_api("GET", f"{gdoc_sync.DOCS}/{doc}?fields=documentId", None)
    temp = _upload("POST", f"{UPLOAD}/files?uploadType=multipart&fields=id",
                   {"name": f"(temporary) {title}", "mimeType": DOC_MIME}, page)["id"]
    try:
        if kinds is not None:
            gdoc_sync.normalize_lists(_docs_api, temp, kinds)
        edits = gdoc_sync.sync(_docs_api, doc, temp)
    finally:
        _request("DELETE", f"{API}/files/{temp}", None, "application/json")
    if _api("GET", f"{API}/files/{doc}?fields=name")["name"] != title:
        _api("PATCH", f"{API}/files/{doc}?fields=id", {"name": title})
    return edits


def _docs_api(method: str, url: str, body: dict | None) -> dict:
    try:
        return _api(method, url, body)
    except SystemExit as e:
        if _docs_disabled(str(e)):
            raise SystemExit("the Google Docs API is not enabled for the rclone client, so the Doc cannot be updated "
                             f"in place. Enable it here, wait a few minutes, then publish again: {_console(str(e))}")
        raise


def _docs_disabled(err: str) -> bool:
    return "docs.googleapis.com" in err and ("SERVICE_DISABLED" in err or "has not been used" in err)


def _console(err: str) -> str:
    # Google's error names the exact console page (with the project); else the generic one.
    m = re.search(r"https://console\.developers\.google\.com/apis/api/docs\.googleapis\.com/overview\?project=\d+", err)
    return m.group(0) if m else DOCS_API_CONSOLE


def check() -> list[str]:
    """What is missing for publishing, as sentences (empty when all works)."""
    import shutil
    if not shutil.which("rclone"):
        return ["rclone is not installed (https://rclone.org/install/). See README: Reports as Google Docs."]
    remotes = subprocess.run(["rclone", "listremotes"], capture_output=True, text=True).stdout.split()
    if f"{REMOTE}:" not in remotes:
        return [f"no rclone remote {REMOTE!r}: run `rclone config` and make a Google Drive remote with your own "
                "OAuth client (scope drive.file). See README: Reports as Google Docs."]
    try:
        _token()
        _api("GET", f"{API}/about?fields=user(displayName)")
    except (SystemExit, subprocess.CalledProcessError, StopIteration, ValueError) as e:
        return [f"Google Drive does not answer through {REMOTE!r} (enable the Drive API for the OAuth client's "
                f"project, or run `rclone config reconnect {REMOTE}:`): {e}"]
    try:  # a missing Doc gives 404 when the Docs API is on, 403 SERVICE_DISABLED when it is off
        _api("GET", f"{gdoc_sync.DOCS}/{'0' * 44}?fields=documentId")
    except SystemExit as e:
        if _docs_disabled(str(e)):
            return [f"the Google Docs API is off for the OAuth client's project: enable it at {_console(str(e))}"]
        if "HTTP 404" not in str(e) and "HTTP 400" not in str(e):
            return [f"the Google Docs API check failed: {e}"]
    return []


def _bind(src: Path, doc: str, reg: dict) -> None:
    _check_doc(doc, src)
    prev = reg.get(str(src)) or {}
    if prev.get("id") and prev["id"] != doc:
        raise SystemExit(f"{src} is already bound to Doc {prev['id']}; edit {REGISTRY} to change it")
    reg[str(src)] = {**prev, "id": doc, "url": f"https://docs.google.com/document/d/{doc}/edit"}


def _check_doc(doc: str, src: Path) -> None:
    try:
        meta = _api("GET", f"{API}/files/{doc}?fields=trashed,mimeType")
    except (SystemExit, OSError) as e:
        raise SystemExit(f"cannot read Doc {doc} for {src}: {e}. Not publishing: a new Doc would lose the link, "
                         "the sharing and the comments. Fix access to the Doc (or restore it), then publish again.")
    if meta.get("trashed") or meta.get("mimeType") != DOC_MIME:
        raise SystemExit(f"Doc {doc} for {src} is in the trash or is not a Google Doc. Restore it, then publish again.")


def _docs_titled(title: str, folder: str) -> list[str]:
    name = title.replace("\\", "\\\\").replace("'", "\\'")
    q = urllib.parse.quote(f"name = '{name}' and '{folder}' in parents and trashed = false and mimeType = '{DOC_MIME}'")
    return [f["id"] for f in _api("GET", f"{API}/files?q={q}&fields=files(id)").get("files", [])]


def _upload(method: str, url: str, meta: dict, html: str) -> dict:
    boundary = uuid.uuid4().hex
    body = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{json.dumps(meta)}\r\n"
            f"--{boundary}\r\nContent-Type: text/html; charset=UTF-8\r\n\r\n{html}\r\n--{boundary}--\r\n").encode()
    return _request(method, url, body, f"multipart/related; boundary={boundary}")


def _api(method: str, url: str, body: dict | None = None) -> dict:
    return _request(method, url, json.dumps(body).encode() if body is not None else None, "application/json")


def _request(method: str, url: str, data: bytes | None, content_type: str) -> dict:
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Authorization": f"Bearer {_token()}", "Content-Type": content_type})
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        raise SystemExit(f"Drive API {method} {url.split('?')[0]}: HTTP {e.code} {e.read()[:300]!r}") from e
    return json.loads(raw) if raw else {}


_cached: dict = {}


def _token() -> str:
    # rclone refreshes the OAuth token in its config when it is near expiry; a cheap call makes it do so.
    if _cached.get("exp", 0) > time.time() + 60:
        return _cached["tok"]
    subprocess.run(["rclone", "about", f"{REMOTE}:"], check=True, capture_output=True)
    out = subprocess.run(["rclone", "config", "show", REMOTE], check=True, capture_output=True, text=True).stdout
    line = next(x for x in out.splitlines() if x.startswith("token = "))
    tok = json.loads(line.removeprefix("token = "))
    expiry = tok.get("expiry", "")[:19]
    exp = time.mktime(time.strptime(expiry, "%Y-%m-%dT%H:%M:%S")) if expiry else time.time() + 300
    _cached.update(tok=tok["access_token"], exp=min(exp, time.time() + 30 * 60))
    return tok["access_token"]


def _first_heading(text: str) -> str | None:
    m = re.search(r"^#\s+(.+)$", text, re.MULTILINE)
    return m.group(1).strip() if m else None


if __name__ == "__main__":
    main()
