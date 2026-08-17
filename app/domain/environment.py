from __future__ import annotations

from enum import StrEnum


class TradingEnvironment(StrEnum):
    """Exchange-side environment bound to credentials, requests, and write grants."""

    DEMO = "demo"
    LIVE = "live"
