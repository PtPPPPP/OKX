from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.config.run_config import RunConfig
from app.domain.market import Instrument
from backtest.multi_asset import MultiAssetBacktestEngine
from tests.conftest import make_candles


def test_multi_asset_market_config_validates_shared_quote_currency() -> None:
    cfg = RunConfig.model_validate(
        {
            "market": {
                "instrument_id": "BTC-USDT",
                "instruments": ["BTC-USDT", "ETH-USDT"],
                "bar": "5m",
            }
        }
    )
    assert cfg.market.effective_instruments == ("BTC-USDT", "ETH-USDT")

    with pytest.raises(ValidationError, match="同一计价货币"):
        RunConfig.model_validate(
            {
                "market": {
                    "instrument_id": "BTC-USDT",
                    "instruments": ["BTC-USDT", "ETH-USDC"],
                    "bar": "5m",
                }
            }
        )


def test_multi_asset_portfolio_engine_shares_single_equity_pool(
    btc_instrument: Instrument,
    eth_instrument: Instrument,
) -> None:
    cfg = RunConfig.model_validate(
        {
            "mode": "backtest",
            "market": {
                "instrument_id": "BTC-USDT",
                "instruments": ["BTC-USDT", "ETH-USDT"],
                "instrument_type": "spot",
                "bar": "5m",
            },
            "strategy": {
                "name": "moving_average_cross",
                "parameters": {"fast_period": 2, "slow_period": 3},
            },
            "position_sizing": {
                "name": "percent_equity",
                "parameters": {"equity_pct": "0.20"},
            },
            "risk": {
                "max_order_notional": "3000",
                "max_total_exposure": "6000",
                "max_daily_loss": "500",
                "max_drawdown_pct": "25",
                "max_orders_per_minute": 10,
                "stale_after_seconds": 3600,
            },
            "protective_exits": {"enabled": False},
            "backtest": {
                "initial_capital": "10000",
                "fee_rate": "0.001",
                "slippage_rate": "0.0005",
            },
        }
    )
    # Both BTC and ETH cross up on bar 3 -> both execute on bar 4 sharing the same 10,000 USDT pool,
    # then cross down on bar 6 -> exit on bar 7.
    btc_candles = make_candles(["100", "98", "96", "105", "108", "110", "95", "94"])
    eth_candles = make_candles(["50", "49", "48", "54", "56", "58", "47", "46"])

    engine = MultiAssetBacktestEngine(
        config=cfg,
        instruments={"BTC-USDT": btc_instrument, "ETH-USDT": eth_instrument},
        candles_by_instrument={"BTC-USDT": btc_candles, "ETH-USDT": eth_candles},
    )
    result = engine.run()

    assert result.instruments == ("BTC-USDT", "ETH-USDT")
    assert len(result.equity_curve) == len(btc_candles)
    assert "BTC-USDT" in result.per_instrument_summary
    assert "ETH-USDT" in result.per_instrument_summary
    # Verify both instruments executed fills from the shared portfolio
    filled_instruments = {f.instrument_id for f in result.fills}
    assert filled_instruments == {"BTC-USDT", "ETH-USDT"}
    # Cash balance never goes negative
    assert all(pt.quote_balance >= Decimal("0") for pt in result.equity_curve)
