#!/usr/bin/env python3
"""Command entrypoint; keeps core and picker imports in the same module namespace."""

from buffers import main


if __name__ == "__main__":
    raise SystemExit(main())
