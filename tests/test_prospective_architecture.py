from __future__ import annotations

import hashlib
from pathlib import Path


def test_collector_path_has_no_private_or_trading_imports() -> None:
    files = (
        Path("backtest/prospective_oos.py"),
        Path("backtest/prospective_collector.py"),
        Path("scripts/collect_prospective_oos.py"),
    )
    source = "\n".join(path.read_text(encoding="utf-8") for path in files)
    forbidden = (
        "app.market.private_websocket",
        "app.exchange.okx_client",
        "app.execution.demo_broker",
        "place_order",
        "cancel_order",
        "bounded_demo",
    )
    assert all(value not in source for value in forbidden)


def test_production_strategy_is_untouched() -> None:
    expected = {
        "configs/btc_vwap_shadow.yaml": "6688baa20b6eeb79ec45a899bef7c487c16da0ff4541355afaa763247ea365a6",
        "app/strategies/vwap_shadow.py": "fcffd5ddcaeb3196a8c04272214fc1c3c47857fd8e30ca783a84aa43044b6e09",
    }
    actual = {name: hashlib.sha256(Path(name).read_bytes()).hexdigest() for name in expected}
    assert actual == expected
