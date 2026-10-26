"""Keyboard-driven, terminal-safe picker for the local buffer store."""

from __future__ import annotations

import curses
import os
import shlex
import shutil
import sqlite3
import stat
import subprocess
import tempfile
import unicodedata
from collections.abc import Callable

from buffers import MAX_BUFFER_BYTES, Buffer, BufferError, Store, validate_name


def _safe_display(text: str) -> str:
    replacements = {"\n": r"\n", "\r": r"\r", "\t": "    "}
    return "".join(
        replacements.get(char, "?")
        if unicodedata.category(char).startswith("C")
        else char
        for char in text
    )


def _cell_width(char: str) -> int:
    if unicodedata.category(char) in ("Mn", "Me"):
        return 0
    return 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1


def _clip(text: str, width: int, *, tail: bool = False) -> str:
    if width <= 0:
        return ""
    chars = []
    used = 0
    for char in reversed(text) if tail else text:
        size = _cell_width(char)
        if used + size > width:
            break
        chars.append(char)
        used += size
    if tail:
        chars.reverse()
    # A clipped combining mark must not attach to the preceding UI label.
    while chars and _cell_width(chars[0]) == 0:
        chars.pop(0)
    return "".join(chars)


def _wrap(text: str, width: int, limit: int) -> list[str]:
    if width <= 0 or limit <= 0:
        return []
    lines = []
    for source in text.split("\n"):
        line = []
        used = 0
        for char in _safe_display(source):
            size = _cell_width(char)
            if size > width:
                char, size = "?", 1
            if used + size > width:
                lines.append("".join(line))
                if len(lines) == limit:
                    return lines
                line, used = [], 0
            if not line and size == 0:
                continue
            line.append(char)
            used += size
        lines.append("".join(line))
        if len(lines) == limit:
            return lines
    return lines


def _put(window, row: int, text: str, attr: int = 0) -> None:
    height, width = window.getmaxyx()
    if not 0 <= row < height or width <= 0:
        return
    try:
        window.addstr(row, 0, _clip(_safe_display(text), width), attr)
    except curses.error:
        # Writing the bottom-right cell and concurrent resizes can both fail.
        pass


def _refresh(window) -> None:
    try:
        window.refresh()
    except curses.error:
        pass


def _footer(window, text: str) -> None:
    height, _ = window.getmaxyx()
    if height <= 0:
        return
    try:
        window.move(height - 1, 0)
        window.clrtoeol()
    except curses.error:
        pass
    _put(window, height - 1, text, curses.A_REVERSE)
    _refresh(window)


def _usable(window) -> bool:
    height, width = window.getmaxyx()
    return height >= 4 and width >= 40


def _prompt(window, label: str, initial: str = "") -> str | None:
    value = initial
    while True:
        if not _usable(window):
            _footer(window, "Resize to 40x4; Esc cancels.")
            if window.get_wch() == "\x1b":
                return None
            continue
        _, width = window.getmaxyx()
        prefix = _clip(_safe_display(label), max(0, width // 2))
        available = width - sum(_cell_width(char) for char in prefix) - 1
        _footer(window, prefix + _clip(_safe_display(value), available, tail=True) + "_")
        key = window.get_wch()
        if key in ("\n", "\r", curses.KEY_ENTER):
            return value
        if key == "\x1b":
            return None
        if key in ("\b", "\x7f", curses.KEY_BACKSPACE):
            value = value[:-1]
        elif key == "\x15":  # Ctrl-U clears the field, including a prefilled name.
            value = ""
        elif isinstance(key, str) and not unicodedata.category(key).startswith("C"):
            value += key


def _confirm(window, message: str) -> bool:
    while True:
        if not _usable(window):
            _footer(window, "Resize to 40x4; Esc cancels.")
            if window.get_wch() == "\x1b":
                return False
            continue
        _, width = window.getmaxyx()
        suffix = " [y/N]"
        _footer(window, _clip(_safe_display(message), max(0, width - len(suffix))) + suffix)
        key = window.get_wch()
        if key != curses.KEY_RESIZE:
            return key in ("y", "Y")


def _editor_args() -> list[str]:
    configured = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    if configured:
        try:
            args = shlex.split(configured)
        except ValueError as exc:
            raise BufferError(f"Invalid editor command: {exc}") from exc
        if not args:
            raise BufferError("The editor command is empty.")
        return args
    executable = shutil.which("nano") or shutil.which("vi")
    if executable is None:
        raise BufferError("No editor found; set VISUAL or EDITOR.")
    return [executable]


def _edit_text(text: str) -> str:
    args = _editor_args()
    try:
        with tempfile.TemporaryDirectory(prefix="herdr-buffer-") as directory:
            path = os.path.join(directory, "buffer.txt")
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
                stream.write(text)
            result = subprocess.run([*args, path], check=False)
            if result.returncode != 0:
                raise BufferError(f"Editor exited with status {result.returncode}; buffer unchanged.")
            # Check size before reading, and bound the read in case the file grows.
            info = os.stat(path)
            if not stat.S_ISREG(info.st_mode):
                raise BufferError("Editor did not leave a regular text file.")
            if info.st_size > MAX_BUFFER_BYTES:
                raise BufferError(f"Edited text exceeds {MAX_BUFFER_BYTES} bytes; buffer unchanged.")
            with open(path, "rb") as stream:
                data = stream.read(MAX_BUFFER_BYTES + 1)
            if len(data) > MAX_BUFFER_BYTES:
                raise BufferError(f"Edited text exceeds {MAX_BUFFER_BYTES} bytes; buffer unchanged.")
            return data.decode("utf-8")
    except (OSError, UnicodeError, ValueError) as exc:
        raise BufferError(f"Cannot edit buffer: {exc}") from exc


def _edit_in_terminal(window, text: str) -> str:
    curses.def_prog_mode()
    try:
        curses.endwin()
        return _edit_text(text)
    finally:
        curses.reset_prog_mode()
        window.keypad(True)
        window.clear()
        _refresh(window)


def _draw(window, items: list[Buffer], selected: int, query: str, status: str) -> int:
    height, width = window.getmaxyx()
    window.erase()
    if not _usable(window):
        _footer(window, "Resize to 40x4; q/Esc quits.")
        return 1
    title = f"Herdr Buffers ({len(items)})"
    if query:
        title += f"  /{query}"
    _put(window, 0, title, curses.A_BOLD)
    if height >= 4:
        _put(window, 1, "Enter paste  / search  n new  e edit  r rename  d delete  q/Esc quit")
    rows = max(0, (height - 5) // 2 if height >= 8 else height - 3)
    top = max(0, selected - rows + 1)
    for offset, item in enumerate(items[top : top + rows]):
        active = top + offset == selected
        name = _clip(_safe_display(item.name), max(4, min(24, width // 3)))
        snippet = _safe_display(item.text[: max(0, width * 2)])
        _put(window, 2 + offset, ("> " if active else "  ") + name + "  " + snippet,
             curses.A_REVERSE if active else 0)
    if not items and rows:
        _put(window, 2, "No matching buffers. Press n to create one.")
    if height >= 8 and items:
        item = items[selected]
        preview_row = 2 + rows
        _put(window, preview_row, f"Preview: {item.name}", curses.A_BOLD)
        for offset, line in enumerate(_wrap(item.text, width, height - preview_row - 2)):
            _put(window, preview_row + 1 + offset, line)
    _put(window, height - 1, status or "Arrows/j/k select; PgUp/PgDn page. Prompts: Esc cancel, Ctrl-U clear.",
         curses.A_REVERSE)
    _refresh(window)
    return max(1, rows)


def _run(window, store: Store, on_paste: Callable[[Buffer], None]) -> None:
    window.keypad(True)
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    items: list[Buffer] = []
    selected = 0
    query = ""
    status = ""
    drafts: dict[int | None, str] = {}
    try:
        items = store.list()
    except (BufferError, sqlite3.Error, OSError) as exc:
        status = str(exc)
    while True:
        page_size = _draw(window, items, selected, query, status)
        key = window.get_wch()
        if key in ("q", "\x1b"):
            if drafts and not _confirm(window, "Discard unsaved drafts and quit?"):
                continue
            return
        if key == curses.KEY_RESIZE:
            continue
        if not _usable(window):
            continue
        status = ""
        item = items[selected] if items else None
        try:
            if key in ("j", curses.KEY_DOWN):
                selected = min(len(items) - 1, selected + 1) if items else 0
                continue
            if key in ("k", curses.KEY_UP):
                selected = max(0, selected - 1)
                continue
            if key in (curses.KEY_NPAGE, curses.KEY_PPAGE):
                direction = 1 if key == curses.KEY_NPAGE else -1
                selected = max(0, min(len(items) - 1, selected + direction * page_size))
                continue
            if key in ("\n", "\r", curses.KEY_ENTER) and item:
                if drafts and not _confirm(window, "Discard unsaved drafts and paste saved buffer?"):
                    status = "Paste cancelled; drafts kept."
                    continue
                if ("\n" in item.text or "\r" in item.text) and not _confirm(
                    window, "Multiline paste may execute commands. Paste?"
                ):
                    status = "Paste cancelled."
                    continue
                on_paste(item)
                return
            if key == "/":
                value = _prompt(window, "Filter: ", query)
                if value is None:
                    continue
                query = value
                item = None
            elif key == "n":
                name = _prompt(window, "New name (blank auto): ")
                if name is None:
                    continue
                if name:
                    validate_name(name)
                    if any(existing.name == name for existing in store.list()):
                        raise BufferError("That buffer name already exists; choose another name.")
                drafts[None] = _edit_in_terminal(window, drafts.get(None, ""))
                item = store.add(drafts[None], name or None)
                del drafts[None]
                query = ""
                status = "Buffer created."
            elif key == "e" and item:
                drafts[item.id] = _edit_in_terminal(window, drafts.get(item.id, item.text))
                item = store.update(item.id, drafts[item.id])
                del drafts[item.id]
                status = "Buffer updated."
            elif key == "r" and item:
                name = _prompt(window, "Name: ", item.name)
                if name is None:
                    continue
                item = store.rename(item.id, name)
                status = "Buffer renamed."
            elif key == "d" and item:
                suffix = " and its unsaved draft" if item.id in drafts else ""
                if not _confirm(window, f"Delete {item.name}{suffix}?"):
                    status = "Delete cancelled."
                    continue
                store.delete(item.id)
                drafts.pop(item.id, None)
                status = "Buffer deleted."
            else:
                continue
            items = store.list(query)
            selected = next((index for index, current in enumerate(items)
                             if item is not None and current.id == item.id),
                            min(selected, max(0, len(items) - 1)) if item else 0)
        except (BufferError, sqlite3.Error, OSError) as exc:
            status = str(exc)
            if key == "n" and None in drafts:
                status += " Draft kept; n retries."
            elif key == "e" and item is not None and item.id in drafts:
                status += " Draft kept; e retries."


def run(store: Store, on_paste: Callable[[Buffer], None]) -> None:
    """Open the picker; pass the original buffer to on_paste without adding Enter."""
    try:
        curses.wrapper(_run, store, on_paste)
    except KeyboardInterrupt:
        pass
