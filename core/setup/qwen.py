#!/usr/bin/env python3
"""Arize setup wizard shim for Qwen Code tracing.

Delegates to tracing.qwen.install for the actual install/uninstall logic.
"""

from __future__ import annotations

import sys

from tracing.qwen import install as _install_mod


def install() -> None:
    """Delegate to tracing.qwen.install.install()."""
    _install_mod.install()


def uninstall() -> None:
    """Delegate to tracing.qwen.install.uninstall()."""
    _install_mod.uninstall()


def main() -> None:
    """Entry point for arize-setup-qwen."""
    try:
        _run()
    except (KeyboardInterrupt, EOFError):
        print("\nSetup cancelled.")
        sys.exit(1)


def _run() -> None:
    """Run the installer."""
    _install_mod.install()


if __name__ == "__main__":
    main()
