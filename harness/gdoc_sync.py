"""Update a Google Doc in place so it matches another Doc, with the Google Docs API.

The new version of a report is imported (Drive HTML conversion) into a temporary Doc; this module then edits the
published Doc until its body equals the temporary Doc's. Only what changed is edited:
  * unchanged paragraphs and tables are not touched, so comments on them, the link and the sharing persist;
  * a changed paragraph gets a character-level edit (only the changed spans are deleted and inserted), then the
    paragraph and text styles of the new version;
  * new paragraphs, tables and images are inserted; removed ones are deleted; table rows are inserted or deleted.
Edits go from the end of the Doc backwards, so the indices of the parts not yet edited stay valid. Some edits need a
second pass (a new table is filled once it exists), so sync() repeats until no difference is left.
"""
from __future__ import annotations

import difflib
import hashlib
import io
import json
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field

from PIL import Image

DOCS = "https://docs.googleapis.com/v1/documents"
OBJ = "\ufffc"  # one index in the Doc: an inline image
TEXT_KEYS = ("bold", "italic", "underline", "strikethrough", "smallCaps", "baselineOffset", "link",
             "weightedFontFamily", "fontSize", "foregroundColor", "backgroundColor")
PARA_KEYS = ("namedStyleType", "alignment", "lineSpacing", "direction", "spacingMode", "spaceAbove", "spaceBelow",
             "indentFirstLine", "indentStart", "indentEnd", "keepLinesTogether", "keepWithNext", "avoidWidowAndOrphan")
INDENTS = ("indentFirstLine", "indentStart", "indentEnd")
MAX_PASSES = 8

Api = Callable[[str, str, dict | None], dict]


@dataclass
class Para:
    start: int
    end: int
    text: str  # with OBJ for each inline image, ending with "\n"
    runs: list[tuple[int, int, str]]  # (offset, end offset, text style as JSON)
    style: str
    bullet: tuple | None
    images: list[tuple[int, str, dict]] = field(default_factory=list)  # (offset, content hash, object properties)
    atomic: bool = False  # holds an element that cannot be partly deleted (an equation)

    @property
    def sig(self) -> tuple:
        return ("p", self.text, tuple(self.runs), self.style, tuple((o, h) for o, h, _ in self.images))


@dataclass
class Table:
    start: int
    end: int
    rows: list[list[list[Para]]]  # row -> cell -> paragraphs

    @property
    def sig(self) -> tuple:
        return ("t", tuple(self.row_sig(r) for r in self.rows))

    @staticmethod
    def row_sig(row: list[list[Para]]) -> tuple:
        return tuple(tuple(p.sig for p in cell) for cell in row)

    @property
    def cols(self) -> int:
        return len(self.rows[0]) if self.rows else 0


def sync(api: Api, target: str, source: str) -> int:
    images: dict[str, str] = {}
    total = 0
    for _ in range(MAX_PASSES):
        tgt, src = api("GET", f"{DOCS}/{target}", None), api("GET", f"{DOCS}/{source}", None)
        reqs = plan(_blocks(tgt, images), _blocks(src, images), src, _body_end(tgt))
        if not reqs:  # text and styles match: now the lists (which paragraphs are list items, at which level)
            reqs = list_plan(tgt, src)
        if not reqs:
            return total
        api("POST", f"{DOCS}/{target}:batchUpdate", {"requests": reqs})
        total += len(reqs)
    raise SystemExit(f"the Doc {target} still differs from the new version after {MAX_PASSES} passes; "
                     "nothing was lost, but check the Doc")


def plan(old: list, new: list, src_doc: dict, seg_end: int) -> list[dict]:
    reqs: list[dict] = []
    ops = difflib.SequenceMatcher(None, [b.sig for b in old], [b.sig for b in new], autojunk=False).get_opcodes()
    for tag, i1, i2, j1, j2 in reversed(ops):
        if tag == "equal":
            continue
        pairs, dels, ins = _pair(old[i1:i2], new[j1:j2])
        # Back to front: the inserted run sits after the paired ones, the deleted ones are after them too. New text
        # at the end of the segment would hang on a block deleted in this pass: it waits for the next pass.
        if ins and not (dels and i2 == len(old)):
            reqs += _insert_blocks(old, i2, ins, src_doc, seg_end)
        if dels:
            reqs += _delete_blocks(old, dels, seg_end)
        for o, n in reversed(pairs):
            reqs += _edit_para(o, n, src_doc) if isinstance(o, Para) else _edit_table(o, n, src_doc)
    return reqs


def _pair(old: list, new: list) -> tuple[list, list, list]:
    # Pair changed blocks of the same kind in order (a paragraph whose text or style changed); the rest of the old
    # ones are deleted and the rest of the new ones inserted. Tables pair only with the same number of columns.
    pairs, k = [], 0
    for o, n in zip(old, new):
        same_kind = type(o) is type(n) and (isinstance(o, Para) or o.cols == n.cols)
        if not same_kind:
            break
        pairs.append((o, n))
        k += 1
    return pairs, old[k:], new[k:]


def _edit_para(o: Para, n: Para, src_doc: dict) -> list[dict]:
    if [x[1] for x in o.images] != [x[1] for x in n.images] or (o.atomic and o.text != n.text):
        # An image changed (or the paragraph holds an equation, from older imports of "$"): replace the paragraph's content and keep its newline. Deleting the whole paragraph
        # fails when a table follows it (the newline before a table cannot be deleted).
        reqs = _delete_range(o.start, o.end - 1)
        text = n.text[:-1].replace(OBJ, "")
        if text:
            reqs.append({"insertText": {"location": {"index": o.start}, "text": text}})
        reqs += [_image(o.start + off, props) for off, _, props in n.images]
        return reqs + _style_para(o.start, n)
    reqs = []
    a, b = o.text[:-1], n.text[:-1]
    for tag, i1, i2, j1, j2 in reversed(difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes()):
        x1, x2 = o.start + _u16(a[:i1]), o.start + _u16(a[:i2])
        if tag in ("replace", "insert") and b[j1:j2]:
            reqs.append({"insertText": {"location": {"index": x2}, "text": b[j1:j2]}})
        if tag in ("replace", "delete") and x2 > x1:
            reqs.append({"deleteContentRange": {"range": {"startIndex": x1, "endIndex": x2}}})
    if reqs or o.runs != n.runs or o.style != n.style:
        reqs += _style_para(o.start, n)
    return reqs


def _edit_table(o: Table, n: Table, src_doc: dict) -> list[dict]:
    reqs: list[dict] = []
    rows = difflib.SequenceMatcher(None, [Table.row_sig(r) for r in o.rows], [Table.row_sig(r) for r in n.rows],
                                   autojunk=False).get_opcodes()
    if len(o.rows) != len(n.rows):
        # Rows first; the cells of new rows are filled on the next pass, when they exist.
        for tag, i1, i2, j1, j2 in reversed(rows):
            had, want = i2 - i1, j2 - j1
            for r in range(i2 - 1, i1 + want - 1, -1):  # surplus old rows
                reqs.append({"deleteTableRow": {"tableCellLocation": _cell(o, r)}})
            for _ in range(want - had):  # missing rows, after the last kept one
                last = i1 + had - 1
                reqs.append({"insertTableRow": {"tableCellLocation": _cell(o, max(last, 0)), "insertBelow": last >= 0}})
        return reqs
    for orow, nrow in reversed(list(zip(o.rows, n.rows))):
        for ocell, ncell in reversed(list(zip(orow, nrow))):
            reqs += plan(ocell, ncell, src_doc, ocell[-1].end)
    return reqs


def _cell(t: Table, row: int) -> dict:
    return {"tableStartLocation": {"index": t.start}, "rowIndex": row, "columnIndex": 0}


def _insert_blocks(old: list, at: int, new: list, src_doc: dict, seg_end: int) -> list[dict]:
    # New blocks go before old[at] (or at the end). A table is inserted alone; the blocks after it follow on the
    # next pass, when its index is known.
    tables = [k for k, b in enumerate(new) if isinstance(b, Table)]
    if tables:
        k = tables[0]
        if k > 0:
            new = new[:k]
        else:
            t = new[0]
            prev = old[at - 1] if at > 0 else None
            if not isinstance(prev, Para):
                raise SystemExit("cannot place a new table here (no paragraph before it); publish with --replace")
            return [{"insertTable": {"rows": len(t.rows), "columns": t.cols, "location": {"index": prev.end - 1}}}]
    nxt = old[at] if at < len(old) else None
    if isinstance(nxt, Para):
        return _insert_paras(nxt.start, new, src_doc, before=True)
    prev = old[at - 1] if at > 0 else None
    if not isinstance(prev, Para):
        raise SystemExit("cannot place new text here (no paragraph before it); publish with --replace")
    return _insert_paras(prev.end - 1, new, src_doc, before=False)


def _insert_paras(index: int, paras: list[Para], src_doc: dict, before: bool) -> list[dict]:
    # before=True: index is the start of a paragraph; the new ones go in front of it ("A\nB\n").
    # before=False: index is the newline that ends a paragraph; the new ones follow it ("\nA\nB").
    text = "".join(p.text for p in paras).replace(OBJ, "")
    reqs = [{"insertText": {"location": {"index": index}, "text": text if before else "\n" + text[:-1]}}]
    pos = index if before else index + 1
    for p in paras:
        for off, _, props in p.images:
            reqs.append(_image(pos + off, props))
        reqs += _style_para(pos, p)
        pos += _u16(p.text)
    return reqs


def _image(index: int, props: dict) -> dict:
    emb = props["embeddedObject"]
    req = {"location": {"index": index}, "uri": emb["imageProperties"]["contentUri"]}
    if emb.get("size", {}).get("width"):
        req["objectSize"] = {k: emb["size"][k] for k in ("width", "height") if k in emb["size"]}
    return {"insertInlineImage": req}


def _style_para(start: int, n: Para) -> list[dict]:
    end = start + _u16(n.text)
    reqs: list[dict] = []
    # An indent left out of the request is not cleared: set no indent as 0.
    style = {k: {"magnitude": 0, "unit": "PT"} for k in INDENTS} | json.loads(n.style)
    reqs.append({"updateParagraphStyle": {"range": {"startIndex": start, "endIndex": end},
                                          "paragraphStyle": style, "fields": ",".join(PARA_KEYS)}})
    # A link is not removed by an update that clears it along with the other fields: clear links on their own first.
    reqs.append({"updateTextStyle": {"range": {"startIndex": start, "endIndex": end}, "textStyle": {},
                                     "fields": "link"}})
    for a, b, style in n.runs:
        if b > a:
            reqs.append({"updateTextStyle": {"range": {"startIndex": start + a, "endIndex": start + b},
                                             "textStyle": json.loads(style), "fields": ",".join(TEXT_KEYS)}})
    return reqs


# ------------------------------------------------------------------------------------------------ lists
# A list is rebuilt as a whole: its bullets are removed, each item gets one tab per nesting level at its start, and
# createParagraphBullets makes one list over all its items (the tabs set the nesting levels and are removed). Making
# bullets paragraph by paragraph made a separate list for each item: numbering restarted and nesting was lost.
# Docs imported from HTML carry no list types (every level reads GLYPH_TYPE_UNSPECIFIED), so a new Doc and the
# temporary Doc of an update are rebuilt the same way first (normalize_lists, with the types from the Markdown);
# then both sides carry explicit types and the list pass compares them.
PRESET = {True: "NUMBERED_DECIMAL_ALPHA_ROMAN", False: "BULLET_DISC_CIRCLE_SQUARE"}


def list_plan(tgt: dict, src: dict) -> list[dict]:
    """Requests that make the target's lists match the source's; the paragraphs match already (same order)."""
    tp, sp = _body_paras(tgt), _body_paras(src)
    if len(tp) != len(sp):
        return []
    want = _list_items(src, sp)
    have = _list_items(tgt, tp)
    if want == have:
        return []
    return _rebuild(tp, want, _body_end(tgt))


def normalize_lists(api: Api, doc: str, kinds: list[tuple[int, int, bool]]) -> int:
    """Rebuild every list of a Doc imported from HTML with explicit types. kinds: one (list number, nesting level,
    numbered) per list item, in order, from the Markdown. Nothing is changed when the Doc's list items do not match
    the Markdown's one for one."""
    d = api("GET", f"{DOCS}/{doc}", None)
    paras = _body_paras(d)
    items = [i for i, (_, p) in enumerate(paras) if "bullet" in p]
    if len(items) != len(kinds):
        return 0
    want = {i: k for i, k in zip(items, kinds)}
    reqs = _rebuild(paras, want, _body_end(d))
    if reqs:
        api("POST", f"{DOCS}/{doc}:batchUpdate", {"requests": reqs})
    return len(reqs)


def _body_paras(doc: dict) -> list[tuple[dict, dict]]:
    return [(el, el["paragraph"]) for el in doc["body"]["content"] if "paragraph" in el]


def _list_items(doc: dict, paras: list[tuple[dict, dict]]) -> dict[int, tuple[int, int, bool]]:
    """Paragraph number -> (list group, nesting level, numbered) for the list items. A list group is a run of items of
    one list; the type is that of the list's top level, as createParagraphBullets gives one preset to a whole list."""
    out: dict[int, tuple[int, int, bool]] = {}
    group, prev = -1, None
    for i, (_, p) in enumerate(paras):
        b = p.get("bullet")
        if not b:
            prev = None
            continue
        if b["listId"] != prev:
            group += 1
            prev = b["listId"]
        levels = doc.get("lists", {}).get(b["listId"], {}).get("listProperties", {}).get("nestingLevels", [{}])
        top = levels[0] if levels else {}
        numbered = top.get("glyphType", "GLYPH_TYPE_UNSPECIFIED") not in ("GLYPH_TYPE_UNSPECIFIED", "NONE")
        out[i] = (group, b.get("nestingLevel", 0), numbered)
    return out


def _rebuild(paras: list[tuple[dict, dict]], want: dict[int, tuple[int, int, bool]], body_end: int) -> list[dict]:
    """Requests that make exactly the paragraphs in want list items, list by list, back to front."""
    blocks: list[list[int]] = []
    for i in sorted(want):
        if blocks and blocks[-1][-1] == i - 1 and want[blocks[-1][-1]][0] == want[i][0]:
            blocks[-1].append(i)
        else:
            blocks.append([i])
    ops: list[tuple[int, list[dict]]] = []  # (start index, requests): applied from the end of the Doc backwards
    for blk in blocks:
        s = paras[blk[0]][0]["startIndex"]
        e = min(paras[blk[-1]][0]["endIndex"], body_end - 1)
        # Without its bullets an item keeps the list's indent, which createParagraphBullets would count as more
        # nesting levels on top of the tabs: clear the indent first.
        reqs = [{"deleteParagraphBullets": {"range": {"startIndex": s, "endIndex": e}}},
                {"updateParagraphStyle": {"range": {"startIndex": s, "endIndex": e},
                                          "paragraphStyle": {"indentStart": {"magnitude": 0, "unit": "PT"},
                                                             "indentFirstLine": {"magnitude": 0, "unit": "PT"}},
                                          "fields": "indentStart,indentFirstLine"}}]
        tabs = 0
        for i in reversed(blk):
            level = want[i][1]
            if level:
                reqs.append({"insertText": {"location": {"index": paras[i][0]["startIndex"]}, "text": "\t" * level}})
                tabs += level
        reqs.append({"createParagraphBullets": {"range": {"startIndex": s, "endIndex": e + tabs},
                                                "bulletPreset": PRESET[want[blk[0]][2]]}})
        ops.append((s, reqs))
    for i, (el, p) in enumerate(paras):  # paragraphs that are list items now but should not be
        if "bullet" in p and i not in want:
            ops.append((el["startIndex"], [{"deleteParagraphBullets": {"range": {
                "startIndex": el["startIndex"], "endIndex": min(el["endIndex"], body_end - 1)}}}]))
    reqs: list[dict] = []
    for _, r in sorted(ops, key=lambda x: -x[0]):
        reqs += r
    return reqs


def _delete_blocks(old: list, dels: list, seg_end: int) -> list[dict]:
    s, e = dels[0].start, dels[-1].end
    if e >= seg_end:
        # The segment's last newline cannot be deleted: take the newline before the range instead.
        return _delete_range(s - 1, e - 1)
    return _delete_range(s, e)


def _delete_range(s: int, e: int) -> list[dict]:
    return [{"deleteContentRange": {"range": {"startIndex": s, "endIndex": e}}}] if e > s else []


def _body_end(doc: dict) -> int:
    return doc["body"]["content"][-1]["endIndex"]


def _blocks(doc: dict, images: dict[str, str]) -> list:
    return [b for b in (_block(el, doc, images) for el in doc["body"]["content"]) if b is not None]


def _block(el: dict, doc: dict, images: dict[str, str]):
    if "paragraph" in el:
        return _para(el, doc, images)
    if "table" in el:
        rows = [[[_para(c, doc, images) for c in cell["content"] if "paragraph" in c]
                 for cell in row["tableCells"]] for row in el["table"]["tableRows"]]
        return Table(el["startIndex"], el["endIndex"], rows)
    return None  # section breaks and tables of contents are left alone


def _para(el: dict, doc: dict, images: dict[str, str]) -> Para:
    p = el["paragraph"]
    text, runs, imgs, atomic = "", [], [], False
    for e in p.get("elements", []):
        if "textRun" in e:
            t = e["textRun"]["content"]
            runs.append((_u16(text), _u16(text) + _u16(t), _style(e["textRun"].get("textStyle", {}), TEXT_KEYS)))
            text += t
        elif "inlineObjectElement" in e:
            props = doc.get("inlineObjects", {})[e["inlineObjectElement"]["inlineObjectId"]]["inlineObjectProperties"]
            imgs.append((_u16(text), _image_hash(props, images), props))
            text += OBJ
        else:  # page breaks, footnote refs, auto text: one index each; an equation spans its content
            text += OBJ * max(1, e.get("endIndex", 0) - e.get("startIndex", 0))
            atomic = atomic or "equation" in e
    bullet = None
    if "bullet" in p:
        level = p["bullet"].get("nestingLevel", 0)
        lst = doc.get("lists", {}).get(p["bullet"]["listId"], {})
        glyph = lst.get("listProperties", {}).get("nestingLevels", [{}] * (level + 1))[level]
        bullet = (level, bool(glyph.get("glyphType")) and glyph.get("glyphType") != "GLYPH_TYPE_UNSPECIFIED")
    return Para(el["startIndex"], el["endIndex"], text, _merge(runs), _style(p.get("paragraphStyle", {}), PARA_KEYS),
                bullet, imgs, atomic)


def _merge(runs: list[tuple[int, int, str]]) -> list[tuple[int, int, str]]:
    out: list[tuple[int, int, str]] = []  # offsets in UTF-16 code units, as the Docs API counts
    for a, b, s in runs:
        if out and out[-1][2] == s and out[-1][1] == a:
            out[-1] = (out[-1][0], b, s)
        else:
            out.append((a, b, s))
    return out


def _style(style: dict, keys: tuple[str, ...]) -> str:
    out = {k: style[k] for k in keys if k in style}
    for k in INDENTS:  # a zero indent reads as {"unit": "PT"} or not at all, depending on how it was set
        if k in out and not out[k].get("magnitude"):
            del out[k]
    if out.get("baselineOffset") == "NONE":
        del out["baselineOffset"]
    font = out.get("weightedFontFamily", {}).get("fontFamily", "")
    if font.islower():  # older imports wrote "courier new"; the API keeps it when asked for "Courier New"
        out["weightedFontFamily"] = out["weightedFontFamily"] | {"fontFamily": font.title()}
    if out.get("weightedFontFamily") == {"fontFamily": "Arial", "weight": 400}:  # the default font, set or not
        del out["weightedFontFamily"]
    if "url" not in out.get("link", {"url": ""}):  # a link within the Doc: its bookmark or heading id is the source
        del out["link"]                              # Doc's own and invalid in the target, so it is not copied
    return json.dumps(out, sort_keys=True)


def _image_hash(props: dict, cache: dict[str, str]) -> str:
    emb = props.get("embeddedObject", {})
    uri = emb.get("imageProperties", {}).get("contentUri", "")
    if uri and uri not in cache:
        with urllib.request.urlopen(uri, timeout=60) as r:
            data = r.read()
        # Hash the decoded pixels, not the file: Google re-encodes an image that insertInlineImage adds, so its
        # bytes differ from the temporary Doc's copy even when the picture is the same.
        try:
            with Image.open(io.BytesIO(data)) as im:
                data = im.convert("RGBA").tobytes()
        except OSError:
            pass
        cache[uri] = hashlib.sha1(data).hexdigest()[:16]
    size = emb.get("size", {})
    dims = tuple(round(size.get(k, {}).get("magnitude", 0)) for k in ("width", "height"))
    return f"{cache.get(uri, '')}:{dims}"


def _u16(text: str) -> int:
    # The Docs API counts indices in UTF-16 code units: a character outside the BMP (an emoji) takes two.
    return len(text.encode("utf-16-le")) // 2
