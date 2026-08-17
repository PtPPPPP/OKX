"""Offline, deterministic persistence profiling harness (Phase 2A).

This package is benchmark tooling only. It never changes production behavior:
all instrumentation happens by wrapping ``sqlite3.connect`` inside the
benchmark process and is fully removed afterwards.
"""
