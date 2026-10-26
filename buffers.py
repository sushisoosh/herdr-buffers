#!/usr/bin/env python3
"""Named paste buffers for Herdr. Run plugin.py --help for the command interface."""

import argparse
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import socket
import sqlite3
import sys
import time
import uuid


PLUGIN_ID = "herdr-buffers"
VERSION = "0.1.0"
MAX_BUFFER_BYTES = 1024 * 1024
MAX_TOTAL_BYTES = 16 * MAX_BUFFER_BYTES
MAX_BUFFERS = 200


class BufferError(Exception):
    """An actionable storage, input, or Herdr integration error."""


@dataclass(frozen=True)
class Buffer:
    id: int
    name: str
    text: str
    created_at: int
    updated_at: int


def validate_text(text):
    if not isinstance(text, str) or not text:
        raise BufferError("The buffer is empty; nothing was saved.")
    if len(text.encode("utf-8")) > MAX_BUFFER_BYTES:
        raise BufferError("A buffer cannot exceed 1 MiB of UTF-8 text.")


def validate_name(name):
    if not name or not name.strip() or len(name) > 80 or not name.isprintable():
        raise BufferError("Use a non-blank name of at most 80 printable characters.")


class Store:
    def __init__(self, state_dir):
        state_dir = Path(state_dir)
        state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        state_dir.chmod(0o700)
        path = state_dir / "buffers.sqlite3"
        # Create privately before SQLite opens the file, also rejecting symlinks.
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
        self.db = sqlite3.connect(path, timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS buffers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                text TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            )
        """)
        self.db.commit()

    def close(self):
        self.db.close()

    def list(self, query=""):
        rows = self.db.execute("SELECT * FROM buffers ORDER BY id DESC")
        query = query.casefold()
        return [
            Buffer(**dict(row)) for row in rows
            if query in row["name"].casefold() or query in row["text"].casefold()
        ]

    def get(self, buffer_id=None):
        if buffer_id is not None and (type(buffer_id) is not int or not 1 <= buffer_id < 2**63):
            raise BufferError("Buffer IDs must be positive signed 64-bit integers.")
        if buffer_id is None:
            row = self.db.execute("SELECT * FROM buffers ORDER BY id DESC LIMIT 1").fetchone()
        else:
            row = self.db.execute("SELECT * FROM buffers WHERE id = ?", (buffer_id,)).fetchone()
        if row is None:
            raise BufferError("No such buffer. Capture a selection or create one in the picker.")
        return Buffer(**dict(row))

    def _check_capacity(self, text, replacing=None):
        count, total = self.db.execute(
            "SELECT count(*), coalesce(sum(length(cast(text AS BLOB))), 0) FROM buffers"
        ).fetchone()
        if replacing is None and count >= MAX_BUFFERS:
            raise BufferError("Buffer limit reached (200). Delete an old buffer first.")
        previous = len(self.get(replacing).text.encode("utf-8")) if replacing else 0
        if total - previous + len(text.encode("utf-8")) > MAX_TOTAL_BYTES:
            raise BufferError("Buffer storage limit reached (16 MiB). Delete an old buffer first.")

    def add(self, text, name=None):
        validate_text(text)
        if name is not None:
            validate_name(name)
        try:
            with self.db:
                self.db.execute("BEGIN IMMEDIATE")
                self._check_capacity(text)
                if name is None:
                    # AUTOINCREMENT keeps generated names stable after deletions.
                    row = self.db.execute(
                        "SELECT seq FROM sqlite_sequence WHERE name = 'buffers'"
                    ).fetchone()
                    sequence = (row[0] if row else 0) + 1
                    name = f"buffer-{sequence}"
                    while self.db.execute(
                        "SELECT 1 FROM buffers WHERE name = ?", (name,)
                    ).fetchone():
                        sequence += 1
                        name = f"buffer-{sequence}"
                now = time.time_ns()
                cursor = self.db.execute(
                    "INSERT INTO buffers (name, text, created_at, updated_at) VALUES (?, ?, ?, ?)",
                    (name, text, now, now),
                )
                return self.get(cursor.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise BufferError("That buffer name already exists; choose another name.") from exc

    def update(self, buffer_id, text):
        validate_text(text)
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            self._check_capacity(text, replacing=buffer_id)
            self.db.execute(
                "UPDATE buffers SET text = ?, updated_at = ? WHERE id = ?",
                (text, time.time_ns(), buffer_id),
            )
            return self.get(buffer_id)

    def rename(self, buffer_id, name):
        validate_name(name)
        try:
            with self.db:
                self.db.execute("BEGIN IMMEDIATE")
                self.get(buffer_id)
                self.db.execute(
                    "UPDATE buffers SET name = ?, updated_at = ? WHERE id = ?",
                    (name, time.time_ns(), buffer_id),
                )
                return self.get(buffer_id)
        except sqlite3.IntegrityError as exc:
            raise BufferError("That buffer name already exists; choose another name.") from exc

    def delete(self, buffer_id):
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            self.get(buffer_id)
            self.db.execute("DELETE FROM buffers WHERE id = ?", (buffer_id,))


class Herdr:
    def __init__(self, socket_path=None):
        self.socket_path = socket_path or os.environ.get("HERDR_SOCKET_PATH")
        if not self.socket_path:
            raise BufferError("No Herdr socket context. Invoke this action from Herdr.")

    def call(self, method, params):
        request_id = uuid.uuid4().hex
        request = {"id": request_id, "method": method, "params": params}
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(5)
                connection.connect(self.socket_path)
                connection.sendall(json.dumps(request, ensure_ascii=False).encode("utf-8") + b"\n")
                with connection.makefile("rb") as stream:
                    line = stream.readline(2 * MAX_BUFFER_BYTES + 1)
            if len(line) > 2 * MAX_BUFFER_BYTES or not line.endswith(b"\n"):
                raise BufferError("Herdr returned an incomplete or oversized response.")
            response = json.loads(line)
        except (OSError, ValueError) as exc:
            raise BufferError(f"Herdr request failed: {exc}. No automatic retry was attempted.") from exc
        if not isinstance(response, dict) or response.get("id") != request_id:
            raise BufferError("Herdr returned a mismatched response; do not blindly retry a paste.")
        if "error" in response:
            error = response["error"]
            message = error.get("message", "Request rejected") if isinstance(error, dict) else str(error)
            raise BufferError(f"Herdr: {message}")
        if "result" not in response:
            raise BufferError("Herdr returned a response without a result.")
        return response["result"]

    def paste(self, pane_id, text):
        validate_text(text)
        # Escape/control bytes can break paste framing or act as terminal keys.
        if any((ord(char) < 32 and char not in "\t\n\r") or 127 <= ord(char) < 160 for char in text):
            raise BufferError("Buffer contains terminal control characters. Edit them out before pasting.")
        return self.call("pane.send_input", {"pane_id": pane_id, "text": text, "keys": []})

    def open_picker(self, pane_id, state_dir=None):
        env = {"BUFFER_TARGET_PANE_ID": pane_id}
        if state_dir is not None:
            env["BUFFER_STATE_DIR"] = str(Path(state_dir).resolve())
        return self.call("plugin.pane.open", {
            "plugin_id": PLUGIN_ID,
            "entrypoint": "picker",
            "placement": "popup",
            # Popups reject target_pane_id. Pin the destination across focus changes.
            "env": env,
        })


def notify(message):
    if not os.environ.get("HERDR_SOCKET_PATH"):
        return
    try:
        Herdr().call("notification.show", {
            "title": "Paste Buffers",
            "body": "".join(char for char in message if char.isprintable())[:240],
            "sound": "none",
        })
    except BufferError:
        # Notifications are best-effort; never retry an already successful save/paste.
        pass


def context():
    try:
        value = json.loads(os.environ.get("HERDR_PLUGIN_CONTEXT_JSON", "{}"))
    except ValueError as exc:
        raise BufferError("Invalid Herdr plugin context.") from exc
    if not isinstance(value, dict):
        raise BufferError("Invalid Herdr plugin context.")
    return value


def target_pane(explicit=None):
    pane_id = explicit or os.environ.get("BUFFER_TARGET_PANE_ID")
    if not pane_id:
        pane_id = context().get("focused_pane_id") or os.environ.get("HERDR_PANE_ID")
    if not isinstance(pane_id, str) or not pane_id:
        raise BufferError("No destination pane in the invocation context; refusing to guess.")
    return pane_id


def parser():
    result = argparse.ArgumentParser(
        description="Named paste buffers for Herdr (Python 3.10+, Linux/macOS).",
        epilog=(
            "Link with: herdr plugin link /path/to/herdr-buffers. "
            "Copy mode: select text, invoke capture BEFORE normal y, then q to leave. "
            "Use keybindings.toml for shortcut examples. Buffers persist locally in "
            "HERDR_PLUGIN_STATE_DIR/buffers.sqlite3 until deleted; do not save secrets "
            "you do not want retained. No system clipboard is used."
        ),
    )
    result.add_argument("--version", action="version", version=VERSION)
    result.add_argument("--state-dir", type=Path, help="override storage (required outside plugin context)")
    commands = result.add_subparsers(dest="command", required=True)
    capture = commands.add_parser("capture", help="save the live copy-mode selection")
    capture.add_argument("--name")
    add = commands.add_parser("add", help="save UTF-8 text from stdin")
    add.add_argument("--name")
    commands.add_parser("list", help="list buffers as JSON metadata, without their text")
    show = commands.add_parser("show", help="export a buffer as JSON, including its text")
    show.add_argument("id", type=int)
    rename = commands.add_parser("rename", help="rename a buffer")
    rename.add_argument("id", type=int)
    rename.add_argument("name")
    delete = commands.add_parser("delete", help="delete a buffer permanently")
    delete.add_argument("id", type=int)
    paste = commands.add_parser("paste", help="paste a buffer; latest multiline text opens the picker")
    paste.add_argument("id", type=int, nargs="?")
    paste.add_argument("--pane", help="explicit destination on the inherited Herdr socket")
    paste.add_argument(
        "--allow-multiline", action="store_true",
        help="confirm multiline paste, which may execute in apps without bracketed paste",
    )
    open_command = commands.add_parser("open", help="open the buffer picker popup")
    open_command.add_argument("--pane")
    commands.add_parser("pick", help="run the picker UI (launched by the popup entrypoint)")
    return result


def dispatch(args):
    state_dir = args.state_dir or os.environ.get("BUFFER_STATE_DIR") or os.environ.get("HERDR_PLUGIN_STATE_DIR")
    if args.command == "open":
        Herdr().open_picker(target_pane(args.pane), state_dir)
        return
    if not state_dir:
        raise BufferError("Run through Herdr, or provide --state-dir for standalone buffer commands.")
    store = Store(state_dir)
    try:
        if args.command == "capture":
            text = context().get("selected_text")
            if not isinstance(text, str) or not text:
                raise BufferError("No selection. In copy mode, select text and use capture BEFORE normal y.")
            entry = store.add(text, args.name)
            notify(f"Saved {entry.name}. Press q to leave copy mode; open the picker to rename or paste.")
        elif args.command == "add":
            text = sys.stdin.buffer.read(MAX_BUFFER_BYTES + 1).decode("utf-8")
            entry = store.add(text, args.name)
            try:
                print(json.dumps({"id": entry.id, "name": entry.name}))
            except OSError as exc:
                raise BufferError(f"Buffer {entry.id} was saved, but command output failed: {exc}") from exc
        elif args.command == "list":
            print(json.dumps([
                {"id": entry.id, "name": entry.name, "bytes": len(entry.text.encode("utf-8"))}
                for entry in store.list()
            ]))
        elif args.command == "show":
            print(json.dumps(asdict(store.get(args.id))))
        elif args.command == "rename":
            store.rename(args.id, args.name)
        elif args.command == "delete":
            store.delete(args.id)
        elif args.command == "paste":
            entry = store.get(args.id)
            pane = target_pane(args.pane)
            herdr = Herdr()
            if ("\n" in entry.text or "\r" in entry.text) and not args.allow_multiline:
                if args.id is not None:
                    raise BufferError("Review this buffer in the picker, or confirm with --allow-multiline.")
                herdr.open_picker(pane, state_dir)
            else:
                herdr.paste(pane, entry.text)
        elif args.command == "pick":
            import picker

            pane = target_pane()
            herdr = Herdr()
            picker.run(store, lambda entry: herdr.paste(pane, entry.text))
    finally:
        store.close()


def main(argv=None):
    os.umask(0o077)
    args = parser().parse_args(argv)
    try:
        dispatch(args)
        return 0
    except (BufferError, OSError, sqlite3.Error, UnicodeError) as exc:
        message = f"Paste Buffers: {exc}"
        print(message, file=sys.stderr)
        if os.environ.get("HERDR_PLUGIN_ID") == PLUGIN_ID:
            notify(message)
        return 1
    except KeyboardInterrupt:
        return 130
