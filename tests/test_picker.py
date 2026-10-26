import dataclasses
import os
from pathlib import Path
import stat
import subprocess
import unicodedata
import unittest
from unittest import mock

import picker


class FakeWindow:
    def __init__(self, keys=(), size=(18, 80)):
        self.keys = iter(keys)
        self.size = size
        self.writes = []
        self.keypad_enabled = False
        self.cleared = False

    def getmaxyx(self):
        return self.size

    def addstr(self, row, column, text, attr=0):
        height, width = self.size
        assert 0 <= row < height
        assert column == 0
        assert sum(picker._cell_width(char) for char in text) <= width
        assert not any(unicodedata.category(char).startswith("C") for char in text)
        self.writes.append((row, text))

    def get_wch(self):
        key = next(self.keys)
        if isinstance(key, tuple):
            self.size = key
            return picker.curses.KEY_RESIZE
        return key

    def keypad(self, enabled):
        self.keypad_enabled = enabled

    def erase(self):
        pass

    def clear(self):
        self.cleared = True

    def refresh(self):
        pass

    def move(self, row, column):
        pass

    def clrtoeol(self):
        pass


def buffer(identifier=1, name="one", text="plain"):
    return picker.Buffer(identifier, name, text, identifier, identifier)


class FakeStore:
    def __init__(self, items=()):
        self.items = list(items)
        self.calls = []

    def list(self, query=""):
        self.calls.append(("list", query))
        return [item for item in self.items if query in item.name or query in item.text]

    def add(self, text, name=None):
        self.calls.append(("add", text, name))
        if not text:
            raise picker.BufferError("Empty text")
        item = buffer(max((item.id for item in self.items), default=0) + 1, name or "auto", text)
        self.items.insert(0, item)
        return item

    def update(self, identifier, text):
        self.calls.append(("update", identifier, text))
        if not text:
            raise picker.BufferError("Empty text")
        return self._replace(identifier, text=text)

    def rename(self, identifier, name):
        self.calls.append(("rename", identifier, name))
        if not name:
            raise picker.BufferError("Empty name")
        return self._replace(identifier, name=name)

    def _replace(self, identifier, **values):
        index = next(index for index, item in enumerate(self.items) if item.id == identifier)
        item = dataclasses.replace(self.items[index], **values)
        self.items[index] = item
        return item

    def delete(self, identifier):
        self.calls.append(("delete", identifier))
        self.items = [item for item in self.items if item.id != identifier]


class DisplayTests(unittest.TestCase):
    def test_safe_display_escapes_controls_but_keeps_unicode(self):
        value = "hello\x1b[31m\x07\x00\x9b\u202e\ud800\t\n\r cafe\u0301 \u754c"
        self.assertEqual(picker._safe_display(value), "hello?[31m?????    \\n\\r cafe\u0301 \u754c")

    def test_clip_uses_cells_and_keeps_combining_marks(self):
        self.assertEqual(picker._clip("a\u0301\u754cb", 3), "a\u0301\u754c")
        self.assertEqual(picker._clip("a\u754cb", 2), "a")
        self.assertEqual(picker._clip("a\u754cb", 3, tail=True), "\u754cb")
        self.assertEqual(picker._clip("a\u0301b", 1, tail=True), "b")
        self.assertEqual(picker._clip("a\ufe0fb", 1), "a\ufe0f")
        self.assertEqual(picker._clip("hello", 0), "")

    def test_wrap_preserves_newlines_and_limits_output(self):
        self.assertEqual(picker._wrap("ab\u754cc\n\nx\n", 3, 10), ["ab", "\u754cc", "", "x", ""])
        self.assertEqual(picker._wrap("a\u0301bc\x1b", 2, 2), ["a\u0301b", "c?"])
        self.assertEqual(picker._wrap("\u754c", 1, 1), ["?"])
        self.assertEqual(picker._wrap("hello", 0, 1), [])
        self.assertEqual(picker._wrap("hello", 2, 0), [])

    def test_put_tolerates_curses_resize_errors(self):
        window = FakeWindow()
        window.addstr = mock.Mock(side_effect=picker.curses.error())
        picker._put(window, 0, "text")
        window.refresh = mock.Mock(side_effect=picker.curses.error())
        picker._refresh(window)


class EditorTests(unittest.TestCase):
    def test_editor_precedence_and_quoted_arguments(self):
        with mock.patch.dict(os.environ, {"VISUAL": "'/path with space/editor' --wait", "EDITOR": "ignored"}, clear=True):
            self.assertEqual(picker._editor_args(), ["/path with space/editor", "--wait"])
        with mock.patch.dict(os.environ, {"EDITOR": "ed -p '$'"}, clear=True):
            self.assertEqual(picker._editor_args(), ["ed", "-p", "$"])

    def test_default_editor_and_invalid_configuration(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch.object(picker.shutil, "which", side_effect=[None, "/usr/bin/vi"]):
                self.assertEqual(picker._editor_args(), ["/usr/bin/vi"])
            with mock.patch.object(picker.shutil, "which", return_value="/bin/nano") as which:
                self.assertEqual(picker._editor_args(), ["/bin/nano"])
                which.assert_called_once_with("nano")
            with mock.patch.object(picker.shutil, "which", return_value=None):
                with self.assertRaises(picker.BufferError):
                    picker._editor_args()
        for invalid in ("'unclosed", "   "):
            with self.subTest(invalid=invalid), mock.patch.dict(os.environ, {"VISUAL": invalid}, clear=True):
                with self.assertRaises(picker.BufferError):
                    picker._editor_args()

    def test_editor_uses_private_file_argv_and_exact_text_then_cleans_up(self):
        paths = []

        def edit(argv, **kwargs):
            self.assertEqual(argv[:-1], ["editor", "--wait", "$(not-a-shell)"])
            self.assertEqual(kwargs, {"check": False})
            path = Path(argv[-1])
            paths.append(path)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
            self.assertEqual(path.read_bytes(), b"original\r\ntext")
            path.write_bytes("edited\r\n\u754c\n".encode("utf-8"))
            return subprocess.CompletedProcess(argv, 0)

        with mock.patch.object(picker, "_editor_args", return_value=["editor", "--wait", "$(not-a-shell)"]):
            with mock.patch.object(picker.subprocess, "run", side_effect=edit):
                self.assertEqual(picker._edit_text("original\r\ntext"), "edited\r\n\u754c\n")
        self.assertFalse(paths[0].parent.exists())

    def test_editor_failures_cleanup(self):
        for failure in ("status", "missing", "invalid_utf8", "oversized", "deleted", "interrupt"):
            paths = []

            def edit(argv, **kwargs):
                path = Path(argv[-1])
                paths.append(path)
                if failure == "missing":
                    raise FileNotFoundError("missing editor")
                if failure == "interrupt":
                    raise KeyboardInterrupt
                if failure == "invalid_utf8":
                    path.write_bytes(b"\xff")
                if failure == "oversized":
                    path.write_bytes(b"x" * 9)
                if failure == "deleted":
                    path.unlink()
                return subprocess.CompletedProcess(argv, 2 if failure == "status" else 0)

            with self.subTest(failure=failure), mock.patch.object(picker, "_editor_args", return_value=["editor"]):
                with mock.patch.object(picker, "MAX_BUFFER_BYTES", 8):
                    with mock.patch.object(picker.subprocess, "run", side_effect=edit):
                        error = KeyboardInterrupt if failure == "interrupt" else picker.BufferError
                        with self.assertRaises(error):
                            picker._edit_text("before")
            self.assertFalse(paths[0].parent.exists())

    def test_size_is_checked_before_file_read(self):
        def edit(argv, **kwargs):
            Path(argv[-1]).write_bytes(b"12345")
            return subprocess.CompletedProcess(argv, 0)

        with mock.patch.object(picker, "_editor_args", return_value=["editor"]):
            with mock.patch.object(picker, "MAX_BUFFER_BYTES", 4):
                with mock.patch.object(picker.subprocess, "run", side_effect=edit):
                    with mock.patch("builtins.open", side_effect=AssertionError("must not read oversized file")):
                        with self.assertRaises(picker.BufferError):
                            picker._edit_text("")

    def test_curses_restored_after_editor_failure(self):
        window = FakeWindow()
        with mock.patch.multiple(picker.curses, def_prog_mode=mock.DEFAULT, endwin=mock.DEFAULT,
                                 reset_prog_mode=mock.DEFAULT) as calls:
            with mock.patch.object(picker, "_edit_text", side_effect=picker.BufferError("failed")):
                with self.assertRaises(picker.BufferError):
                    picker._edit_in_terminal(window, "original")
        for call in calls.values():
            call.assert_called_once_with()
        self.assertTrue(window.keypad_enabled)
        self.assertTrue(window.cleared)


class PickerTests(unittest.TestCase):
    def run_picker(self, keys, items=(), callback=None, size=(18, 80), store=None):
        window = FakeWindow(keys, size)
        store = FakeStore(items) if store is None else store
        callback = mock.Mock() if callback is None else callback
        with mock.patch.object(picker.curses, "curs_set"):
            picker._run(window, store, callback)
        return window, store, callback

    def test_quit_and_escape(self):
        for key in ("q", "\x1b"):
            _, _, callback = self.run_picker([key], [buffer()])
            callback.assert_not_called()

    def test_navigation_pages_and_exact_paste(self):
        items = [buffer(index, str(index), f"payload {index}") for index in range(10)]
        scenarios = [
            (["j", picker.curses.KEY_DOWN, "k", picker.curses.KEY_NPAGE], 7),
            ([picker.curses.KEY_NPAGE, picker.curses.KEY_NPAGE, "j"], 9),
            ([picker.curses.KEY_NPAGE, picker.curses.KEY_PPAGE, "k"], 0),
            (["j", "j", picker.curses.KEY_UP], 1),
        ]
        for keys, expected in scenarios:
            with self.subTest(keys=keys):
                _, _, callback = self.run_picker([*keys, "\n"], items)
                callback.assert_called_once_with(items[expected])
                self.assertEqual(callback.call_args.args[0].text, f"payload {expected}")

    def test_multiline_requires_explicit_confirmation(self):
        for text in ("one\ntwo", "one\r", "one\n"):
            with self.subTest(text=text):
                item = buffer(text=text)
                _, _, callback = self.run_picker(["\n", "\n", "\n", "n", "\n", "y"], [item])
                callback.assert_called_once_with(item)
                self.assertEqual(callback.call_args.args[0].text, text)

    def test_callback_failure_stays_open_and_sanitizes_error(self):
        callback = mock.Mock(side_effect=[picker.BufferError("API \x1b[31mfailed"), None])
        window, _, _ = self.run_picker(["\n", "\n"], [buffer()], callback)
        self.assertEqual(callback.call_count, 2)
        self.assertTrue(any("API ?[31mfailed" in text for _, text in window.writes))

    def test_filter_selects_matching_name_or_content(self):
        matching = buffer(2, "two", "needle")
        _, store, callback = self.run_picker(["/", *"needle", "\n", "\n"], [buffer(), matching])
        self.assertIn(("list", "needle"), store.calls)
        callback.assert_called_once_with(matching)

    def test_cancelled_filter_and_prompt_clear(self):
        _, store, callback = self.run_picker(["/", "x", "\x1b", "\n"], [buffer()])
        self.assertEqual(store.calls, [("list", "")])
        callback.assert_called_once()
        window = FakeWindow(["\x15", "\u754c", "x", picker.curses.KEY_BACKSPACE, "\n"])
        self.assertEqual(picker._prompt(window, "Name: ", "old"), "\u754c")

    def test_delete_requires_y(self):
        _, store, _ = self.run_picker(["d", "\n", "d", "n", "d", "\x1b", "d", "Y", "q"], [buffer()])
        self.assertEqual([call for call in store.calls if call[0] == "delete"], [("delete", 1)])
        self.assertEqual(store.items, [])

    def test_create_edit_rename_and_paste(self):
        with mock.patch.object(picker, "_edit_in_terminal", side_effect=["new\ntext", "edited"]):
            _, store, callback = self.run_picker(["n", *"named", "\n", "e", "r", "\x15", *"renamed", "\n", "\n"])
        self.assertIn(("add", "new\ntext", "named"), store.calls)
        self.assertIn(("update", 1, "edited"), store.calls)
        self.assertIn(("rename", 1, "renamed"), store.calls)
        self.assertEqual(callback.call_args.args[0].text, "edited")
        self.assertEqual(callback.call_args.args[0].name, "renamed")

    def test_editor_failure_and_invalid_edit_preserve_existing_buffer(self):
        item = buffer()
        with mock.patch.object(picker, "_edit_in_terminal", side_effect=[picker.BufferError("Editor failed"), ""]):
            window, store, _ = self.run_picker(["e", "e", "q", "y"], [item])
        self.assertEqual(store.items, [item])
        self.assertTrue(any("Editor failed" in text for _, text in window.writes))
        self.assertTrue(any("Empty text" in text for _, text in window.writes))

    def test_duplicate_new_name_is_rejected_before_editor_opens(self):
        with mock.patch.object(picker, "_edit_in_terminal") as editor:
            window, _, _ = self.run_picker(["n", *"taken", "\n", "q"], [buffer(name="taken")])
        editor.assert_not_called()
        self.assertTrue(any("already exists" in text for _, text in window.writes))

    def test_failed_new_save_retains_draft_for_retry(self):
        store = FakeStore()
        store.add = mock.Mock(side_effect=[picker.BufferError("Store full"), buffer(text="revised")])
        with mock.patch.object(picker, "_edit_in_terminal", side_effect=["composed draft", "revised"]) as editor:
            self.run_picker(["n", "\n", "n", "\n", "q"], store=store)
        self.assertEqual([call.args[1] for call in editor.call_args_list], ["", "composed draft"])
        self.assertEqual(store.add.call_args_list, [mock.call("composed draft", None), mock.call("revised", None)])

    def test_failed_edit_retains_draft_and_quit_requires_confirmation(self):
        item = buffer(text="original")
        store = FakeStore([item])
        store.update = mock.Mock(side_effect=[picker.BufferError("Store full"), picker.BufferError("Still full")])
        with mock.patch.object(picker, "_edit_in_terminal", side_effect=["first draft", "second draft"]) as editor:
            window, _, _ = self.run_picker(["e", "q", "n", "e", "q", "y"], store=store)
        self.assertEqual([call.args[1] for call in editor.call_args_list], ["original", "first draft"])
        self.assertEqual(store.items, [item])
        self.assertTrue(any("Discard unsaved drafts" in text for _, text in window.writes))

    def test_storage_failure_draft_is_not_discarded_by_paste_without_confirmation(self):
        store = FakeStore([buffer()])
        store.update = mock.Mock(side_effect=picker.sqlite3.OperationalError("disk is full"))
        with mock.patch.object(picker, "_edit_in_terminal", return_value="unsaved"):
            window, _, callback = self.run_picker(["e", "\n", "n", "q", "y"], store=store)
        callback.assert_not_called()
        self.assertTrue(any("drafts kept" in text for _, text in window.writes))

    def test_cancelled_new_and_rename_do_not_mutate(self):
        with mock.patch.object(picker, "_edit_in_terminal") as editor:
            _, store, _ = self.run_picker(["n", "\x1b", "r", "\x1b", "q"], [buffer()])
        self.assertEqual(store.calls, [("list", "")])
        editor.assert_not_called()

    def test_empty_store_and_tiny_resized_windows(self):
        keys = [(1, 1), picker.curses.KEY_NPAGE, "j", "k", (0, 0), "\n", (8, 4), "q"]
        self.run_picker(keys)
        self.run_picker([(1, 1), (8, 4), (18, 80), "q"], [buffer(name="\x1bname", text="\u754c\ntext")])

    def test_initial_store_error_is_visible(self):
        store = FakeStore()
        store.list = mock.Mock(side_effect=picker.BufferError("Store unavailable"))
        window, _, _ = self.run_picker(["q"], store=store)
        self.assertTrue(any("Store unavailable" in text for _, text in window.writes))

    def test_tiny_window_does_not_allow_invisible_actions(self):
        _, store, callback = self.run_picker(["\n", "d", "y", "n", "e", "r", "q"], [buffer()], size=(3, 10))
        self.assertEqual(store.calls, [("list", "")])
        callback.assert_not_called()

    def test_resize_during_confirmation_cannot_accept_hidden_warning(self):
        window = FakeWindow([(2, 10), "y", (18, 80), "n"])
        self.assertFalse(picker._confirm(window, "Delete buffer?"))
        window = FakeWindow([(2, 10), "\n", "x", (18, 80), "\n"])
        self.assertEqual(picker._prompt(window, "Name: ", "original"), "original")

    def test_store_mutation_error_remains_in_ui(self):
        store = FakeStore([buffer()])
        store.delete = mock.Mock(side_effect=picker.BufferError("Delete failed"))
        window, _, callback = self.run_picker(["d", "y", "\n"], store=store)
        self.assertTrue(any("Delete failed" in text for _, text in window.writes))
        callback.assert_called_once_with(store.items[0])

    def test_public_entrypoint_uses_wrapper(self):
        store, callback = object(), mock.Mock()
        with mock.patch.object(picker.curses, "wrapper") as wrapper:
            self.assertIsNone(picker.run(store, callback))
        wrapper.assert_called_once_with(picker._run, store, callback)


if __name__ == "__main__":
    unittest.main()
