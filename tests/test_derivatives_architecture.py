from __future__ import annotations

import ast
from pathlib import Path


def test_derivatives_collector_has_no_trading_imports() -> None:
    forbidden = ("strategy", "broker", "risk", "budget", "execution", "shadow")
    for path in (
        Path("backtest/derivatives_collector.py"),
        Path("backtest/derivatives_prospective.py"),
        Path("scripts/collect_derivatives_prospective.py"),
    ):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imports = [node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
        assert not any(term in module for module in imports for term in forbidden)


def test_collector_contains_no_strategy_metrics() -> None:
    content = Path("backtest/derivatives_collector.py").read_text(encoding="utf-8").lower()
    assert not any(term in content for term in ("sharpe", "pnl", "win_rate", "forward_return"))
