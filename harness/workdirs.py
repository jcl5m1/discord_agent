"""Folder-by-folder suggestions for /new's workdir option.

Discord re-runs autocomplete on every keystroke, so suggesting folders that end
in "/" turns the option into a browser: pick ~/github/, and the next suggestions
are that folder itself plus its subfolders. Typed text after the last "/"
filters the current level.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

MAX_CHOICES = 25  # Discord's limit per autocomplete response
MAX_LEN = 100  # Discord's limit for a choice's name and value
MAX_SCAN = 5000  # entries read per folder, so a huge folder can't stall autocomplete


def _display(path: Path, home: Path) -> str:
    """~/a/b for paths under home, otherwise the absolute path; always with /."""
    try:
        rel = path.relative_to(home)
    except ValueError:
        return path.as_posix()
    return "~" if rel == Path(".") else "~/" + rel.as_posix()


def _subfolders(folder: Path, prefix: str) -> list[Path]:
    show_hidden = prefix.startswith(".")
    found = []
    try:
        with os.scandir(folder) as entries:
            for i, entry in enumerate(entries):
                if i >= MAX_SCAN:
                    break
                name = entry.name
                if name.startswith(".") and not show_hidden:
                    continue
                if prefix and not name.lower().startswith(prefix.lower()):
                    continue
                try:
                    if entry.is_dir():
                        found.append(Path(entry.path))
                except OSError:
                    continue
    except OSError:  # missing, not a folder, or no permission
        return []
    return sorted(found, key=lambda p: p.name.lower())


# Labels older versions put on choices, which mobile Discord can send back
# as typed text: leading emoji/symbols, and a trailing "(…)" note.
_LEAD = re.compile(r"^[^\w~/.\\-]+")
_TRAIL = re.compile(r"\s*\((?:use this folder|used before|new folder)\)\s*$")


def clean(text: str) -> str:
    """A typed or picked workdir with any label decoration removed."""
    text = _TRAIL.sub("", text.strip())
    return _LEAD.sub("", text).strip().replace("\\", "/")


def suggest(current: str, home: Path, recent: list[str] | None = None) -> list[tuple[str, str]]:
    """(name, value) choices for what's typed so far.

    Folders are offered with a trailing "/" so picking one lists its contents
    next; the folder being browsed is offered first ("use this folder").
    recent workdirs (e.g. existing instances') lead the list before anything is typed.

    A choice's name is exactly its path: on mobile, picking a choice puts its
    name in the field, and typing on sends that text instead of the value, so
    any label (emoji, "(use this folder)") would end up in the path.
    """
    current = clean(current)
    choices: list[tuple[str, str]] = []

    def add(name: str, value: str) -> None:
        if len(value) <= MAX_LEN and value not in {v for _, v in choices}:
            choices.append((name[:MAX_LEN], value))

    if not current:
        for path in recent or []:
            shown = _display(Path(path), home)
            add(shown, shown)
        current = "~/"
    elif current == "~":
        current = "~/"

    folder_text, _, prefix = current.rpartition("/")
    folder_text += "/"
    if folder_text.startswith("~/"):
        folder = home / folder_text[2:]
    else:
        folder = Path(folder_text)
        if not folder.is_absolute():
            folder = home / folder

    if not prefix:
        shown = _display(folder, home)
        add(shown, shown)
    for sub in _subfolders(folder, prefix):
        shown = _display(sub, home) + "/"
        add(shown, shown)
        if len(choices) >= MAX_CHOICES:
            break
    if prefix and not choices:  # nothing matches: let them create a new folder there
        shown = _display(folder / prefix, home)
        add(shown, shown)
    return choices[:MAX_CHOICES]
