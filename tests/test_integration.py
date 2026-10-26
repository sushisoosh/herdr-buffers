"""Opt-in real Herdr 0.9.5 tests; all servers, panes and state are disposable.

Run: HERDR_BUFFERS_INTEGRATION=1 python3 -B -m unittest discover -s tests -p test_integration.py -v
"""

import json
import os
from pathlib import Path
import select
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid

if os.name == "posix":
    import fcntl
    import pty
    import struct
    import termios
    import tty


ROOT = Path(__file__).resolve().parents[1]
ENABLED = os.environ.get("HERDR_BUFFERS_INTEGRATION") == "1"


def pane_fixture(directory):
    """A bounded, raw PTY byte recorder, never a shell executing pasted text."""
    root = Path(directory)
    pane_id = os.environ["HERDR_PANE_ID"]
    tty.setraw(sys.stdin.fileno())
    (root / (pane_id + ".json")).write_text(json.dumps({
        "pane_id": pane_id, "pid": os.getpid(), "pgid": os.getpgrp(),
    }), encoding="utf-8")
    with (root / (pane_id + ".input")).open("wb", buffering=0) as received:
        os.write(1, b"\x1b[?2004h\x1b[2J\x1b[HCOPY-ONE\r\nCOPY-TWO")
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if select.select([0], [], [], 0.2)[0]:
                data = os.read(0, 65536)
                if not data:
                    break
                received.write(data)


@unittest.skipUnless(ENABLED and os.name == "posix", "set HERDR_BUFFERS_INTEGRATION=1 on Unix")
class RealHerdrTests(unittest.TestCase):
    def setUp(self):
        self.binary = shutil.which("herdr")
        if not self.binary:
            self.skipTest("Herdr executable is not installed")
        self.temporary = tempfile.TemporaryDirectory(prefix="hb-it-")
        self.root = Path(self.temporary.name)
        self.processes = []
        self.handles = []
        self.master = None
        self.socket_path = None
        self.client_output = bytearray()
        self.fixture_groups = set()
        self.addCleanup(self.cleanup)
        self.watchdog = threading.Timer(70, self.terminate_owned)
        self.watchdog.daemon = True
        self.watchdog.start()
        for name in ("home", "config", "state", "cache", "data", "runtime", "tmp", "plugin", "fixtures"):
            (self.root / name).mkdir(mode=0o700)
        self.plugin = self.root / "plugin"
        for name in ("buffers.py", "plugin.py", "picker.py", "herdr-plugin.toml"):
            shutil.copyfile(ROOT / name, self.plugin / name)
        self.fixture_script = self.root / "fixture.py"
        shutil.copyfile(__file__, self.fixture_script)
        self.config = self.root / "config.toml"
        self.config.write_text('''onboarding = false
[update]
version_check = false
manifest_check = false
[terminal]
default_shell = "/bin/sh"
shell_mode = "non_login"
[ui]
sidebar_start_collapsed = true
sidebar_collapsed_mode = "hidden"
confirm_close = false
[ui.sound]
enabled = false
[ui.toast]
delivery = "off"
''' + (ROOT / "keybindings.toml").read_text(encoding="utf-8"), encoding="utf-8")
        # An allow-list, rather than inheriting and overriding, removes all caller
        # HERDR_*, shell startup hooks, agent markers and user configuration paths.
        self.env = {
            "PATH": os.pathsep.join((str(Path(sys.executable).parent), os.defpath)),
            "HOME": str(self.root / "home"), "SHELL": "/bin/sh",
            "XDG_CONFIG_HOME": str(self.root / "config"),
            "XDG_STATE_HOME": str(self.root / "state"),
            "XDG_CACHE_HOME": str(self.root / "cache"),
            "XDG_DATA_HOME": str(self.root / "data"),
            "XDG_RUNTIME_DIR": str(self.root / "runtime"),
            "TMPDIR": str(self.root / "tmp"),
            "HERDR_CONFIG_PATH": str(self.config), "HERDR_DISABLE_SOUND": "1",
            "TERM": "xterm-256color", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
            "PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1",
        }
        version = self.run_process([self.binary, "--version"]).stdout.strip()
        self.assertEqual(version, "herdr 0.9.5", "This integration contract is pinned to 0.9.5")
        self.assertEqual(self.run_process([self.binary, "config", "check"]).stdout.strip(), "config: ok")
        self.session = "buffers-integration-" + uuid.uuid4().hex[:8]
        self.server_log = self.root / "server.log"
        log = self.server_log.open("wb")
        self.handles.append(log)
        self.server = subprocess.Popen(
            [self.binary, "--session", self.session, "server"],
            env=self.env, cwd=self.root, stdin=subprocess.DEVNULL,
            stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
        )
        self.processes.append(self.server)

        def server_ready():
            self.assertIsNone(self.server.poll(), self.server_log.read_text(errors="replace"))
            paths = list((self.root / "config").rglob("herdr.sock"))
            if not paths:
                return False
            self.assertEqual(len(paths), 1)
            self.assertIn(self.session, paths[0].parts)
            self.socket_path = paths[0]
            try:
                return self.call("ping", {})["version"] == "0.9.5"
            except OSError:
                return False

        self.wait_for(server_ready, "isolated server socket", timeout=12)
        linked = self.call("plugin.link", {"path": str(self.plugin), "enabled": True})
        self.assertEqual(linked["plugin"]["plugin_id"], "herdr-buffers")
        self.assertFalse(linked["plugin"].get("warnings"), linked)
        workspace = self.call("workspace.create", {"cwd": str(self.root), "label": "test", "focus": True})
        self.workspace_id = workspace["workspace"]["workspace_id"]
        # Replace the initial shell tab with two argv-launched, bounded recorders.
        node = {"type": "pane", "cwd": str(self.root), "command": [
            sys.executable, str(self.fixture_script), "--pane-fixture", str(self.root / "fixtures"),
        ]}
        self.call("layout.apply", {
            "tab_id": workspace["tab"]["tab_id"],
            "focus": True, "root": {"type": "split", "direction": "right", "ratio": 0.5,
                                    "first": dict(node, label="target"), "second": dict(node, label="decoy")},
        })
        panes = self.call("pane.list", {"workspace_id": self.workspace_id})["panes"]
        self.assertEqual(len(panes), 2, panes)
        self.target, self.decoy = [pane["pane_id"] for pane in panes]
        self.wait_for(lambda: self.register_fixture_groups() == 2, "raw pane receivers")
        self.state_dir = self.root / "state" / "herdr" / "plugins" / "herdr-buffers"
        self.call("pane.focus", {"pane_id": self.target})

    def run_process(self, argv, extra_env=None):
        result = subprocess.run(argv, env=dict(self.env, **(extra_env or {})), cwd=self.root,
                                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=8)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        return result

    def call(self, method, params):
        # Never discover or fall back to a user socket.
        self.assertIsNotNone(self.socket_path)
        self.assertTrue(self.socket_path.is_relative_to(self.root))
        request_id = uuid.uuid4().hex
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(2)
            connection.connect(str(self.socket_path))
            connection.sendall(json.dumps({"id": request_id, "method": method, "params": params}).encode() + b"\n")
            with connection.makefile("rb") as stream:
                response = json.loads(stream.readline(4 * 1024 * 1024))
        self.assertEqual(response["id"], request_id)
        self.assertNotIn("error", response, response)
        return response["result"]

    def wait_for(self, predicate, description, timeout=6):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.drain_client()
            value = predicate()
            if value:
                return value
            time.sleep(0.05)
        self.fail(f"Timed out waiting for {description}; client tail: {bytes(self.client_output[-2500:])!r}")

    def drain_client(self):
        if self.master is None:
            return
        while select.select([self.master], [], [], 0)[0]:
            try:
                data = os.read(self.master, 65536)
            except OSError:
                break
            if not data:
                break
            self.client_output.extend(data)
            del self.client_output[:-262144]

    def start_client(self):
        self.master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 36, 120, 0, 0))
        try:
            client = subprocess.Popen([self.binary, "--session", self.session], cwd=self.root,
                                      env=self.env, stdin=slave, stdout=slave, stderr=slave,
                                      start_new_session=True)
            self.processes.append(client)
        finally:
            os.close(slave)
        self.wait_for(lambda: b"COPY-ONE" in self.client_output, "attached client screen", timeout=12)
        self.client_output.clear()

    def send_client(self, data):
        self.assertIsNotNone(self.master)
        os.write(self.master, data)
        # Let the terminal parser separate Escape/prefix transitions from later keys.
        until = time.monotonic() + 0.15
        while time.monotonic() < until:
            self.drain_client()
            time.sleep(0.01)

    def stored(self):
        path = self.state_dir / "buffers.sqlite3"
        if not path.exists():
            return []
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1) as db:
            try:
                return db.execute("SELECT id, text FROM buffers ORDER BY id").fetchall()
            except sqlite3.OperationalError as exc:
                if "no such table" not in str(exc):
                    raise
                return []  # The first action may still be creating the schema.

    def received(self, pane):
        path = self.root / "fixtures" / (pane + ".input")
        return path.read_bytes() if path.exists() else b""

    def test_context_capture_targeted_paste_and_popup(self):
        text = "  caf\u00e9 \u754c\nsecond\tline\n"
        invoked = self.call("plugin.action.invoke", {
            "action_id": "herdr-buffers.capture",
            "context": {"focused_pane_id": self.target, "selected_text": text},
        })
        self.assertEqual(invoked["context"]["selected_text"], text)
        self.wait_for(lambda: self.stored(), "capture persisted Unicode multiline text")
        self.assertEqual(self.stored(), [(1, text)])
        self.call("pane.focus", {"pane_id": self.decoy})
        self.run_process([sys.executable, str(self.plugin / "plugin.py"), "paste", "1", "--allow-multiline"], {
            "HERDR_SOCKET_PATH": str(self.socket_path), "HERDR_PLUGIN_STATE_DIR": str(self.state_dir),
            "HERDR_PLUGIN_CONTEXT_JSON": json.dumps({"focused_pane_id": self.target}),
        })
        expected = b"\x1b[200~" + text.encode("utf-8") + b"\x1b[201~"
        self.wait_for(lambda: self.received(self.target) == expected, "exact bracketed paste")
        self.assertEqual(self.received(self.decoy), b"")
        self.start_client()
        self.run_process([sys.executable, str(self.plugin / "plugin.py"), "open", "--pane", self.target], {
            "HERDR_SOCKET_PATH": str(self.socket_path),
        })
        self.wait_for(lambda: b"Herdr Buffers" in self.client_output, "real curses popup")
        self.send_client(b"q")
        # Reopening proves q really closed the singleton; a live popup returns ui_busy.
        self.client_output.clear()
        self.run_process([sys.executable, str(self.plugin / "plugin.py"), "open", "--pane", self.target], {
            "HERDR_SOCKET_PATH": str(self.socket_path),
        })
        self.wait_for(lambda: b"Herdr Buffers" in self.client_output, "reopened curses popup")
        self.send_client(b"\r")
        self.wait_for(lambda: b"[y/N]" in self.client_output, "multiline confirmation")
        self.send_client(b"y")
        self.wait_for(lambda: self.received(self.target) == expected * 2, "popup paste to pinned, unfocused target")
        time.sleep(0.2)
        self.assertEqual(self.received(self.target), expected * 2, "paste must not append Enter or popup keys")
        self.assertEqual(self.received(self.decoy), b"", "focused decoy must receive no paste or quit keys")

    def test_native_copy_mode_prefix_capture(self):
        self.start_client()
        self.send_client(b"\x02[")
        self.send_client(b"g0vlllllll")
        self.send_client(b"\x02y")
        self.wait_for(lambda: self.stored(), "native Copy selection captured by prefix+y")
        self.assertEqual(self.stored(), [(1, "COPY-ONE")])
        # Unlike the tagged docs' ui_busy claim, 0.9.5 popup transitions dismiss
        # client-owned Copy mode. Exercise the actual bundled open shortcut too.
        self.send_client(b"\x02=")
        self.wait_for(lambda: b"Herdr Buffers" in self.client_output, "picker opened from Copy mode")
        self.send_client(b"q")
        self.assertEqual(self.received(self.target), b"", "Copy keys must not reach the pane application")
        self.assertEqual(self.received(self.decoy), b"")
        self.send_client(b"Z")
        self.wait_for(lambda: self.received(self.target) == b"Z", "ordinary input after popup dismissed Copy")
        self.send_client(b"\x02]")
        expected = b"Z\x1b[200~COPY-ONE\x1b[201~"
        self.wait_for(lambda: self.received(self.target) == expected, "prefix+] non-submitting paste")
        self.assertEqual(self.received(self.decoy), b"")

    def register_fixture_groups(self):
        for path in (self.root / "fixtures").glob("*.json"):
            try:
                metadata = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            group = metadata.get("pgid")
            if isinstance(group, int) and group > 1 and group == metadata.get("pid") and group != os.getpgrp():
                self.fixture_groups.add(group)
        return len(self.fixture_groups)

    def terminate_owned(self):
        self.register_fixture_groups()
        for process in reversed(self.processes):
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
        for group in self.fixture_groups:
            if group != os.getpgrp():
                try:
                    os.killpg(group, signal.SIGTERM)
                except ProcessLookupError:
                    pass

    def cleanup(self):
        self.watchdog.cancel()
        self.watchdog.join(timeout=2)
        self.register_fixture_groups()
        if self.socket_path is not None and getattr(self, "server", None) is not None and self.server.poll() is None:
            try:
                self.call("server.stop", {})
                self.server.wait(timeout=4)
            except (OSError, ValueError, AssertionError, subprocess.TimeoutExpired):
                pass
        self.terminate_owned()
        for process in reversed(self.processes):
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=3)
        if self.master is not None:
            os.close(self.master)
        for handle in self.handles:
            handle.close()
        self.temporary.cleanup()


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--pane-fixture":
        pane_fixture(sys.argv[2])
    else:
        unittest.main()
