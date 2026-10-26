import concurrent.futures
import io
import json
import os
from pathlib import Path
import socket
import sqlite3
import stat
import tempfile
import threading
import unittest
from unittest.mock import patch

import buffers


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "state"
        self.store = buffers.Store(self.path)
        self.addCleanup(self.store.close)

    def test_round_trip_whitespace_unicode_and_persistence(self):
        text = "  \u03b1\t\U0001f40d\nsecond line\n"
        entry = self.store.add(text, "Named buffer")
        second = buffers.Store(self.path)
        try:
            self.assertEqual(second.get(entry.id).text, text)
            self.assertEqual(second.get().name, "Named buffer")
        finally:
            second.close()
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((self.path / "buffers.sqlite3").stat().st_mode), 0o600)

    def test_edit_rename_search_delete_and_order(self):
        first = self.store.add("Stra\u00dfe", "First")
        second = self.store.add("latest")
        self.store.update(first.id, "Stra\u00dfe edited")
        self.store.rename(first.id, "Renamed")
        self.assertEqual(self.store.get().id, second.id)
        self.assertEqual(self.store.list("STRASSE")[0].name, "Renamed")
        self.assertEqual(self.store.list("NAMED")[0].text, "Stra\u00dfe edited")
        self.store.delete(second.id)
        self.assertEqual(self.store.get().id, first.id)
        with self.assertRaises(buffers.BufferError):
            self.store.get(second.id)

    def test_conflicts_do_not_overwrite_or_break_transaction(self):
        first = self.store.add("original", "taken")
        second = self.store.add("second")
        with self.assertRaises(buffers.BufferError):
            self.store.add("replacement", "taken")
        with self.assertRaises(buffers.BufferError):
            self.store.rename(second.id, "taken")
        self.assertEqual(self.store.get(first.id).text, "original")
        self.store.update(second.id, "still usable")
        self.assertEqual(self.store.get(second.id).text, "still usable")

    def test_generated_name_collision_and_deleted_ids(self):
        first = self.store.add("custom name", "buffer-2")
        second = self.store.add("automatic name")
        self.assertNotEqual(first.name, second.name)
        self.store.delete(second.id)
        third = self.store.add("another")
        self.assertGreater(third.id, second.id)

    def test_empty_and_invalid_names(self):
        for text in ("", None):
            with self.subTest(text=text), self.assertRaises(buffers.BufferError):
                self.store.add(text)
        for name in ("", "  ", "a\nb", "\x1b[31m", "x" * 81):
            with self.subTest(name=name), self.assertRaises(buffers.BufferError):
                self.store.add("text", name)
        self.assertEqual(self.store.add(" \n\t ").text, " \n\t ")

    def test_utf8_byte_limits_and_no_silent_eviction(self):
        with patch.object(buffers, "MAX_BUFFER_BYTES", 8), patch.object(buffers, "MAX_TOTAL_BYTES", 10):
            entry = self.store.add("\u00e9" * 4)
            with self.assertRaises(buffers.BufferError):
                self.store.add("\u00e9" * 5)
            with self.assertRaises(buffers.BufferError):
                self.store.add("abc")
            self.store.update(entry.id, "small")
            self.store.add("12345")
            with self.assertRaises(buffers.BufferError):
                self.store.update(entry.id, "too big")
            self.assertEqual(self.store.get(entry.id).text, "small")
        with patch.object(buffers, "MAX_BUFFERS", 2), self.assertRaises(buffers.BufferError):
            self.store.add("third")
        self.assertEqual(len(self.store.list()), 2)

    def test_missing_mutations_do_not_create_rows(self):
        for action in (lambda: self.store.update(99, "text"),
                       lambda: self.store.rename(99, "name"),
                       lambda: self.store.delete(99)):
            with self.assertRaises(buffers.BufferError):
                action()
        self.assertEqual(self.store.list(), [])

    def test_invalid_id_range_is_a_user_error(self):
        for buffer_id in (0, -1, 2**63, True, "1"):
            with self.subTest(buffer_id=buffer_id), self.assertRaises(buffers.BufferError):
                self.store.get(buffer_id)

    def test_concurrent_capture_limit_is_transactional(self):
        def capture(number):
            store = buffers.Store(self.path)
            try:
                store.add(str(number))
                return True
            except buffers.BufferError:
                return False
            finally:
                store.close()

        with patch.object(buffers, "MAX_BUFFERS", 2):
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                results = list(pool.map(capture, range(4)))
        self.assertEqual(sum(results), 2)
        self.assertEqual(len(self.store.list()), 2)

    def test_database_symlink_is_rejected(self):
        self.store.close()
        self.path.joinpath("buffers.sqlite3").unlink()
        self.path.joinpath("buffers.sqlite3").symlink_to(self.path / "elsewhere")
        with self.assertRaises(OSError):
            buffers.Store(self.path)


class HerdrTests(unittest.TestCase):
    def exchange(self, response, invoke=None):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "api.sock")
            received = []
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
                listener.bind(path)
                listener.listen(1)
                listener.settimeout(2)

                def serve():
                    with listener.accept()[0] as client:
                        client.settimeout(2)
                        with client.makefile("rb") as stream:
                            request = json.loads(stream.readline())
                        received.append(request)
                        payload = response(request)
                        if isinstance(payload, dict):
                            payload = json.dumps(payload).encode() + b"\n"
                        for start in range(0, len(payload), 3):
                            client.sendall(payload[start:start + 3])

                thread = threading.Thread(target=serve)
                thread.start()
                try:
                    herdr = buffers.Herdr(path)
                    if invoke is None:
                        herdr.paste("w9:p2", "\u03b1\nexact\ttext\n")
                    else:
                        invoke(herdr)
                finally:
                    thread.join(timeout=3)
                    self.assertFalse(thread.is_alive())
            return received[0]

    def test_paste_framing_unicode_exact_target_and_no_enter(self):
        request = self.exchange(lambda req: {"id": req["id"], "result": {"type": "ok"}})
        self.assertEqual(request["method"], "pane.send_input")
        self.assertEqual(request["params"], {
            "pane_id": "w9:p2", "text": "\u03b1\nexact\ttext\n", "keys": [],
        })

    def test_popup_pins_target_without_invalid_target_parameter(self):
        request = self.exchange(
            lambda req: {"id": req["id"], "result": {"type": "ok"}},
            lambda api: api.open_picker("w8:p7"),
        )
        self.assertEqual(request["method"], "plugin.pane.open")
        self.assertEqual(request["params"]["env"]["BUFFER_TARGET_PANE_ID"], "w8:p7")
        self.assertNotIn("target_pane_id", request["params"])

    def test_popup_preserves_explicit_storage_override(self):
        request = self.exchange(
            lambda req: {"id": req["id"], "result": {"type": "ok"}},
            lambda api: api.open_picker("w8:p7", "/tmp/custom-buffers"),
        )
        self.assertEqual(request["params"]["env"]["BUFFER_STATE_DIR"], str(Path("/tmp/custom-buffers").resolve()))

    def test_popup_resolves_symlinked_storage_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            actual = Path(directory) / "state"
            actual.mkdir()
            alias = Path(directory) / "linked-state"
            alias.symlink_to(actual, target_is_directory=True)
            request = self.exchange(
                lambda req: {"id": req["id"], "result": {"type": "ok"}},
                lambda api: api.open_picker("w8:p7", alias),
            )
            self.assertEqual(request["params"]["env"]["BUFFER_STATE_DIR"], str(actual.resolve()))

    def test_errors_malformed_eof_and_wrong_correlation(self):
        for response in (
            lambda req: {"id": req["id"], "error": {"message": "pane not found"}},
            lambda req: {"id": "another-request", "result": {}},
            lambda req: {"id": req["id"]},
            lambda req: b"not-json\n",
            lambda req: b"{",
            lambda req: b"",
        ):
            with self.subTest(response=response), self.assertRaises(buffers.BufferError):
                self.exchange(response)

    def test_control_characters_rejected_before_sending(self):
        with patch.object(buffers.Herdr, "call") as call:
            for text in ("\x1b[201~malicious\n", "\x00", "\x7f", "\x85"):
                with self.subTest(text=text), self.assertRaises(buffers.BufferError):
                    buffers.Herdr("unused").paste("pane", text)
            call.assert_not_called()

    def test_missing_socket_fails_closed(self):
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(buffers.BufferError):
            buffers.Herdr()


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = patch.dict(os.environ, {"HERDR_PLUGIN_STATE_DIR": self.temp.name}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(os.umask, os.umask(0o077))

    def test_capture_uses_live_context_without_clipboard(self):
        text = " selected\ntext\n"
        os.environ["HERDR_PLUGIN_CONTEXT_JSON"] = json.dumps({"selected_text": text})
        with patch.object(buffers, "notify") as notify:
            self.assertEqual(buffers.main(["capture"]), 0)
            notify.assert_called_once()
        store = buffers.Store(self.temp.name)
        try:
            self.assertEqual(store.get().text, text)
        finally:
            store.close()

    def test_missing_or_bad_selection_is_reported(self):
        for value in ("{}", "[]", "not json", '{"selected_text":42}'):
            os.environ["HERDR_PLUGIN_CONTEXT_JSON"] = value
            with patch("sys.stderr", new_callable=io.StringIO) as stderr:
                self.assertEqual(buffers.main(["capture"]), 1)
                self.assertIn("Paste Buffers:", stderr.getvalue())

    def test_target_context_is_explicit_and_popup_target_survives_focus(self):
        os.environ["HERDR_PLUGIN_CONTEXT_JSON"] = '{"focused_pane_id":"w1:p1"}'
        self.assertEqual(buffers.target_pane(), "w1:p1")
        os.environ["BUFFER_TARGET_PANE_ID"] = "w1:p2"
        self.assertEqual(buffers.target_pane(), "w1:p2")
        self.assertEqual(buffers.target_pane("w1:p3"), "w1:p3")
        os.environ.clear()
        with self.assertRaises(buffers.BufferError):
            buffers.target_pane()

    def test_multiline_latest_opens_picker_instead_of_pasting(self):
        store = buffers.Store(self.temp.name)
        entry = store.add("first\nsecond")
        store.close()
        os.environ["HERDR_PLUGIN_CONTEXT_JSON"] = '{"focused_pane_id":"w1:p2"}'
        with patch.object(buffers, "Herdr") as api:
            self.assertEqual(buffers.main(["paste"]), 0)
            api.return_value.open_picker.assert_called_once_with("w1:p2", self.temp.name)
            api.return_value.paste.assert_not_called()
            self.assertEqual(buffers.main(["paste", str(entry.id), "--allow-multiline"]), 0)
            api.return_value.paste.assert_called_once_with("w1:p2", "first\nsecond")

    def test_open_and_multiline_paste_keep_alternate_state_directory(self):
        alternate = Path(self.temp.name) / "alternate"
        store = buffers.Store(alternate)
        store.add("different\nstore")
        store.close()
        os.environ["HERDR_PLUGIN_CONTEXT_JSON"] = '{"focused_pane_id":"w1:p2"}'
        for command in ("open", "paste"):
            with patch.object(buffers, "Herdr") as api:
                self.assertEqual(buffers.main(["--state-dir", str(alternate), command]), 0)
                api.return_value.open_picker.assert_called_once_with("w1:p2", alternate)
        os.environ["BUFFER_STATE_DIR"] = str(alternate)
        with patch("sys.stdout", new_callable=io.StringIO) as stdout:
            self.assertEqual(buffers.main(["list"]), 0)
            self.assertEqual(len(json.loads(stdout.getvalue())), 1)

    def test_add_unicode_name_works_with_ascii_stdout(self):
        output = io.BytesIO()
        stdout = io.TextIOWrapper(output, encoding="ascii", write_through=True)
        stdin = io.TextIOWrapper(io.BytesIO(b"contents"), encoding="utf-8")
        with patch("sys.stdin", stdin), patch("sys.stdout", stdout):
            self.assertEqual(buffers.main(["add", "--name", "\u03b1"]), 0)
        self.assertEqual(json.loads(output.getvalue())["name"], "\u03b1")

    def test_out_of_range_command_ids_fail_without_traceback(self):
        for command in (["show"], ["delete"], ["paste"], ["rename"]):
            args = command + [str(2**63)] + (["name"] if command == ["rename"] else [])
            with patch("sys.stderr", new_callable=io.StringIO) as stderr:
                self.assertEqual(buffers.main(args), 1)
                self.assertIn("64-bit", stderr.getvalue())

    def test_list_never_exposes_buffer_contents(self):
        store = buffers.Store(self.temp.name)
        store.add("private contents", "test")
        store.close()
        with patch("sys.stdout", new_callable=io.StringIO) as stdout:
            self.assertEqual(buffers.main(["list"]), 0)
            self.assertNotIn("private contents", stdout.getvalue())
            self.assertEqual(json.loads(stdout.getvalue())[0]["name"], "test")


if __name__ == "__main__":
    unittest.main()
