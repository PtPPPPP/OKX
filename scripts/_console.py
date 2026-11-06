"""Consistent Unicode output for offline personal CLI entry points."""

from __future__ import annotations

import sys
from io import TextIOWrapper


def configure_utf8_output() -> None:
    """Configure output at invocation, leaving imports and StringIO untouched.

    Windows pipes can default to a legacy code page even when a parent process
    decodes the captured bytes as UTF-8. Keep the existing streams and their error
    policies; do not wrap, close, or replace buffers owned by the caller.
    """
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, TextIOWrapper):
            stream.reconfigure(encoding="utf-8", errors=stream.errors)
