# herdr-buffers

Named, tmux-style paste buffers for [Herdr](https://herdr.dev/), without using the
system clipboard. Capture a copy-mode selection, keep reusable commands or prompts,
and choose what to paste from a keyboard-driven popup.

This is an independent community plugin, not an official Herdr component.

## Features

- Save Herdr copy-mode selections with a dedicated shortcut.
- Keep multiple named buffers across sessions and restarts on the same host.
- Search, preview, create, edit, rename, and delete buffers in a popup.
- Paste into the pane that opened the picker, without appending Enter.
- Confirm multiline pastes and deletions; retain edited drafts after a rejected save.
- Use Python's standard library only, with no pip packages or build step.

## Requirements

- Herdr 0.9.5 or newer.
- Python 3.10 or newer, available as `python3`, with `curses` and `sqlite3`.
- Linux or macOS. Real Herdr integration has been verified on Linux with Herdr
  0.9.5; macOS integration has not yet been verified. Windows is not supported.
- For creating or editing buffer text, an editor configured through `VISUAL` or
  `EDITOR`, or an installed `nano` or `vi`.

Check your Python installation with:

```bash
python3 -c 'import curses, sqlite3; print("Python dependencies available")'
```

## Install

```bash
herdr plugin install sushisoosh/herdr-buffers
```

Review the installation preview before confirming. Like other Herdr plugins, this
plugin runs as your user and is not sandboxed by Herdr.

### Configure shortcuts

Installation does not edit your keybindings. Add the following entries to Herdr's
`config.toml`, normally `~/.config/herdr/config.toml`. If you use
`HERDR_CONFIG_PATH`, edit that file instead. Choose different keys if these are
already assigned in your configuration.

```toml
[[keys.command]]
key = "prefix+y"
type = "plugin_action"
command = "herdr-buffers.capture"
description = "save selection to paste buffer"

[[keys.command]]
key = "prefix+]"
type = "plugin_action"
command = "herdr-buffers.paste"
description = "paste latest buffer"

[[keys.command]]
key = "prefix+="
type = "plugin_action"
command = "herdr-buffers.open"
description = "choose or manage paste buffers"
```

These entries are also available in [keybindings.toml](keybindings.toml). Validate
the configuration, then reload it from the running Herdr session:

```bash
herdr config check
herdr server reload-config
```

With Herdr's default prefix, `prefix+y` means press **Ctrl+B**, release, then press
**y**. A custom Herdr prefix works the same way.

## Capture And Paste

1. Focus the source pane and press `prefix+[` to enter Herdr's copy mode.
2. Navigate to the text, press `v` or Space, and extend the selection.
3. Press **`prefix+y`**, not plain `y`, to save the selection as a buffer.
4. Press `q` to leave copy mode, then focus the destination pane.
5. Press `prefix+]` to paste the latest buffer, or `prefix+=` to choose one.

Captures receive automatic names such as `buffer-1`; rename them in the picker.
"Latest" means most recently created, not most recently renamed or edited.
When the latest buffer contains a newline or carriage return, `prefix+]` opens
the picker for review and confirmation instead of immediately pasting it.

### Picker controls

| Key | Action |
| --- | --- |
| Up/Down or `k`/`j` | Select a buffer |
| Page Up/Page Down | Move through the list |
| Enter | Paste the selected buffer |
| `/` | Filter names and contents; Enter applies the filter |
| `n` | Create a buffer; leave its name blank for an automatic name |
| `e` | Edit the selected buffer's text |
| `r` | Rename the selected buffer |
| `d` | Delete the selected buffer after confirmation |
| `q` or Esc | Close the picker |

Text prompts support Backspace, Ctrl+U to clear, and Esc to cancel. New and edited
text opens in `VISUAL`, then `EDITOR`, with `nano` or `vi` as a fallback. Configure
a wait/blocking option when using a GUI editor. A failed save retains the draft
inside the picker: press `n` or `e` again to retry. Closing the picker with a pending
draft asks for confirmation; drafts are not crash recovery or persistent storage.

The picker needs at least 40 columns and 4 rows. Its preview shows a wrapped top
excerpt rather than a scrollable view of the entire buffer; use `e` to inspect
longer text in your editor. Complex emoji widths may be approximate.

## Copy And Paste Limitations

This plugin reuses Herdr's selection UI, but it does not intercept ordinary copy
operations. Native `y` or Enter copies through Herdr's built-in path and clears
the selection. Use the plugin's capture shortcut **while the selection is still
active**. Automatic clipboard history and transparent mouse-copy capture are not
provided. A missing-selection error does not read the system clipboard as a fallback.

Herdr's API respects the destination application's bracketed-paste mode, and this
plugin never adds an extra Enter. However, **embedded newlines can still execute
commands when an application does not support bracketed paste**. Review the text
and destination before confirming a multiline paste. Buffers containing terminal
control characters other than tabs, newlines, and carriage returns are rejected
at paste time; edit those characters out first.

With remote machines, install the plugin and Python on each server where you want
to use it. Buffers remain local to that host; they are not synchronized across
machines. SSH and multi-client workflows have not yet been integration-tested.

## Storage And Privacy

Buffers are stored in `HERDR_PLUGIN_STATE_DIR/buffers.sqlite3`. On a default Linux
installation this is:

```text
~/.local/state/herdr/plugins/herdr-buffers/buffers.sqlite3
```

The database uses owner-only file permissions, and its directory uses owner-only
directory permissions. **The text is not encrypted.** Saved buffers remain until
deleted, and other processes running as your user can read them. Deletion is not
a secure wipe. Your external editor may also create its own backups or history.
Avoid saving passwords, tokens, or other secrets you do not want retained.

Limits are 200 buffers, 1 MiB of UTF-8 text per buffer, and 16 MiB of text in total.
Reaching a limit rejects the save instead of silently deleting older buffers.

## Update Or Remove

Update a GitHub-installed copy:

```bash
herdr plugin update herdr-buffers
```

To uninstall the plugin:

```bash
herdr plugin uninstall herdr-buffers
```

Uninstalling preserves plugin state. Remove the three custom keybindings if you
stop using the plugin. Deleting saved data is a separate, explicit operation.

For a locally linked development checkout, use `herdr plugin unlink herdr-buffers`
instead. Herdr refuses to install the GitHub version over a locally linked plugin;
unlink first if you intentionally want to switch. You can also keep the local link
for development after publishing the repository.

## Development And Tests

From a checkout of this repository:

```bash
herdr plugin link "$PWD"
python3 -B -m unittest discover -s tests -v
```

The default suite runs unit tests without starting Herdr; the two real integration
tests are skipped. GitHub Actions runs this suite on Linux and macOS using Python
3.10 and 3.14. This is unit coverage, not a claim of verified macOS Herdr integration.

With Herdr **0.9.5** installed, run the full suite:

```bash
HERDR_BUFFERS_INTEGRATION=1 python3 -B -m unittest discover -s tests -v
```

Integration tests create disposable configurations, state directories, sessions,
and terminal processes. They verify native copy-mode capture, all three shortcuts,
exact bracketed-paste bytes without an extra Enter, and popup destination pinning.
They do not use or modify your normal Herdr session. The integration contract is
currently pinned to Herdr 0.9.5 even though the plugin manifest permits newer versions.

For the optional command-line interface:

```bash
python3 plugin.py --help
```

Standalone storage commands need an explicit `--state-dir` unless launched with
Herdr's plugin environment. Paste and popup commands additionally need the correct
Herdr socket and pane context; they never guess a server to connect to.

## License

[MIT](LICENSE). Copyright (c) 2026 sushisoosh.
