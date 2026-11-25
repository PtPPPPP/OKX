from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

import scripts.personal_cockpit as cockpit_cli
from app.daily_reference import calculate_ma_bollinger_snapshot
from app.domain.market import Candle
from app.market.historical_data import save_candles_csv
from app.portfolio.ledger_storage import LedgerStore
from app.portfolio.personal_ledger import Opening, OpeningPosition
from app.portfolio.spot_thesis import ThesisRevision
from app.portfolio.thesis_storage import ThesisStore

START = datetime(2026, 1, 1, tzinfo=UTC)
HOUR = timedelta(hours=1)


def _sample_candles(count: int = 240, *, dip_last: bool = False) -> list[Candle]:
    bars: list[Candle] = []
    for i in range(count):
        # Smooth upward trend with mild oscillation to build non-zero bandwidth
        wave = Decimal((i % 6) - 3) * Decimal("1.2")
        close = Decimal("100") + Decimal(i) * Decimal("0.5") + wave
        if dip_last and i == count - 1:
            # Sharp dip to the lower band while remaining near EMA200 tolerance floor
            close = Decimal("100") + Decimal(i) * Decimal("0.5") - Decimal("10")
        bars.append(
            Candle(
                timestamp=START + i * HOUR,
                open=close - Decimal("0.5"),
                high=close + Decimal("2.0"),
                low=close - Decimal("2.0"),
                close=close,
                volume=Decimal("50"),
                confirmed=True,
            )
        )
    return bars


def test_ma_bollinger_snapshot_computes_bands_atr_and_risk_sizing() -> None:
    candles = _sample_candles(240)
    snap = calculate_ma_bollinger_snapshot(
        candles,
        reference_equity_usdt=Decimal("20000"),
        risk_pct=Decimal("0.01"),
    )
    assert snap["trend_ma_period"] == 200
    assert snap["bollinger_period"] == 48
    assert snap["atr_period"] == 24
    assert Decimal(snap["bb_upper"]) > Decimal(snap["bb_middle"]) > Decimal(snap["bb_lower"])
    assert snap["bandwidth_pct"] > 0
    assert snap["stop_distance_pct"] > 0
    # With 20,000 equity and 1% risk (200 USDT budget), notional scales proportionally
    assert snap["risk_sizing_notional_per_10k_usdt"] > 0
    assert snap["signal_zone"] in {
        "PULLBACK_BUY_ZONE",
        "UPPER_BAND_EXTENSION",
        "UPTREND_ABOVE_MID",
        "UPTREND_PULLBACK_WAIT",
        "BELOW_TREND_WAIT",
    }


def test_personal_cockpit_cli_integrates_ledger_thesis_and_offline_candles(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    candles = _sample_candles(240)
    last_bar = candles[-1]
    asof = last_bar.timestamp

    # 1. Create local ledger with 1 BTC at cost 150 and 15,000 USDT cash
    ledger_db = tmp_path / "ledger.db"
    LedgerStore(ledger_db).initialize(
        Opening(
            as_of=START,
            source="unit_test",
            cash=Decimal("15000"),
            positions=[
                OpeningPosition(
                    instrument="BTC-USDT",
                    quantity=Decimal("1"),
                    average_cost=Decimal("150"),
                )
            ],
        )
    )

    # 2. Create local thesis database and register a BTC-USDT thesis
    thesis_db = tmp_path / "theses.db"
    t_store = ThesisStore(thesis_db)
    t_store.initialize()
    t_store.append_revision(
        ThesisRevision.model_validate(
            {
                "id": "rev-btc-1",
                "instrument": "BTC-USDT",
                "recorded_at": START.isoformat(),
                "source": "unit_test",
                "buy_reason": "核心加密资产长期配置与流动性溢价",
                "check_conditions": [
                    {"id": "c-1", "text": "链上与宏观流动性保持稳定", "kind": "fundamental"}
                ],
                "invalidation_conditions": [
                    {"id": "inv-1", "text": "核心共识或底层安全假设破坏", "kind": "fundamental"}
                ],
                "review_interval_days": 30,
            }
        )
    )

    # 3. Save offline 1H candles directory
    candles_dir = tmp_path / "candles"
    candles_dir.mkdir()
    save_candles_csv(candles, candles_dir / "BTC-USDT.csv")

    # 4. Run cockpit in JSON mode
    code = cockpit_cli.main(
        [
            "--ledger",
            str(ledger_db),
            "--thesis-db",
            str(thesis_db),
            "--candles-dir",
            str(candles_dir),
            "--watchlist",
            "BTC-USDT,ARB-USDT",
            "--at",
            asof.isoformat(),
            "--format",
            "json",
        ]
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["sizing_equity_source"] == "valued_ledger_equity"
    assert Decimal(payload["sizing_equity_usdt"]) > Decimal("15000")
    assert len(payload["positions"]) == 1
    btc_pos = payload["positions"][0]
    assert btc_pos["instrument"] == "BTC-USDT"
    assert btc_pos["buy_reason"] == "核心加密资产长期配置与流动性溢价"
    assert btc_pos["unrealized_return_pct"] > 0

    signals = {item["instrument"]: item for item in payload["quant_signals"]}
    assert signals["BTC-USDT"]["status"] == "READY"
    assert signals["ARB-USDT"]["status"] == "INSUFFICIENT_CANDLES"

    # 5. Run cockpit in Markdown mode and verify clean tables
    out_md = tmp_path / "cockpit.md"
    code_md = cockpit_cli.main(
        [
            "--ledger",
            str(ledger_db),
            "--thesis-db",
            str(thesis_db),
            "--candles-dir",
            str(candles_dir),
            "--watchlist",
            "BTC-USDT",
            "--at",
            asof.isoformat(),
            "--format",
            "markdown",
            "--output",
            str(out_md),
        ]
    )
    assert code_md == 0
    md_text = out_md.read_text(encoding="utf-8")
    assert "# 个人投研与量化风控驾驶舱" in md_text
    assert "## 一、账户估值与组合风险总览" in md_text
    assert "## 二、现货持仓台账与投资逻辑对齐" in md_text
    assert "## 三、均线 + 布林带 + ATR 量化模型与以损定仓面板" in md_text
    assert "核心加密资产长期配置与流动性溢价" in md_text


def test_personal_cockpit_public_fetch_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    candles = _sample_candles(240)
    now = candles[-1].timestamp

    def fake_fetch(
        instruments: list[str], *, history_days: int
    ) -> tuple[datetime, list[Any], dict[str, list[Candle]]]:
        from app.portfolio.personal_ledger import Quote

        return (
            now,
            [
                Quote(
                    instrument="BTC-USDT",
                    price=candles[-1].close,
                    at=now,
                    source="okx_public_ticker",
                )
            ],
            {"BTC-USDT": candles},
        )

    monkeypatch.setattr(cockpit_cli, "_fetch_public_market_data", fake_fetch)
    code = cockpit_cli.main(
        [
            "--fetch-public",
            "--watchlist",
            "BTC-USDT",
            "--format",
            "json",
        ]
    )
    assert code == 0
    report = json.loads(capsys.readouterr().out)
    assert report["quote_source"] == "okx_public_api"
    assert report["quant_signals"][0]["instrument"] == "BTC-USDT"
    assert report["quant_signals"][0]["status"] == "READY"
