from __future__ import annotations

import json
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

import scripts.review_positions as review_runner
from app.domain.market import Candle
from app.market.daily_public import DailyPublicClient
from scripts.review_positions import load_thesis_summaries, render, summarize

ASOF = datetime.fromtimestamp(1793280000, UTC)


def thesis_report(status: str, label: str) -> dict[str, Any]:
    return {
        "instrument": "BTC-USDT",
        "status": status,
        "status_label": label,
        "reasons": ["synthetic_review_reason"],
        "buy_reason": "合成测试的买入理由",
        "holding_policy": "thesis_hold",
        "max_holding_hours": None,
        "action": None,
    }


def sample(count: int, interval: timedelta) -> list[Candle]:
    start = datetime(2025, 1, 1, tzinfo=UTC)
    return [
        Candle(
            start + i * interval,
            Decimal(100),
            Decimal(102),
            Decimal(99),
            Decimal(101),
            Decimal(10),
            True,
        )
        for i in range(count)
    ]


def test_legacy_market_value_is_snapshot_and_never_infers_quantity() -> None:
    result = summarize(
        sample(730, timedelta(days=1)),
        sample(360, timedelta(hours=1)),
        {"instrument": "BTC-USDT", "cost": "100", "market_value_usdt": "90"},
        {"ticker": {"last": "90", "ts": "1793280000000"}},
        asof=ASOF,
    )
    assert result["return_from_cost_pct"] == -10
    assert result["rise_to_break_even_pct"] == pytest.approx(100 / 9)
    assert result["quantity"] is None
    assert result["quantity_status"] == "UNKNOWN"
    assert result["valuation_status"] == "UNKNOWN_QUANTITY"
    assert result["cost_basis_usdt"] is None
    assert result["market_value_usdt"] is None
    assert result["unrealized_pnl_usdt"] is None
    assert result["historical_snapshot"]["market_value_usdt"] == "90"
    assert "estimated_quantity" not in result
    assert result["historical_3d"]["sample_count"] == 243
    assert result["historical_3d"]["q95_pct"] == 0


def test_actual_quantity_uses_new_quote_instead_of_historical_market_value() -> None:
    result = summarize(
        sample(100, timedelta(days=1)),
        sample(360, timedelta(hours=1)),
        {
            "instrument": "BTC-USDT",
            "cost": "100.1",
            "quantity": "0.03",
            "market_value_usdt": "90",
            "updated_at": "2026-10-29T00:00:00+00:00",
            "source": "synthetic_manual_record",
        },
        {"ticker": {"last": "90.2", "ts": "1793280000000"}},
        asof=ASOF,
    )
    assert result["quantity"] == "0.03"
    assert result["market_value_usdt"] == "2.706"
    assert result["cost_basis_usdt"] == "3.003"
    assert result["unrealized_pnl_usdt"] == "-0.297"
    assert result["quote_status"] == "FRESH"
    assert result["valuation_status"] == "READY"
    assert result["source"] == "synthetic_manual_record"


@pytest.mark.parametrize(
    ("ts", "status"),
    [
        ("1793279699000", "STALE"),
        ("1793280001000", "FUTURE"),
        (None, "MISSING_TIMESTAMP"),
        ("invalid", "INVALID_TIMESTAMP"),
    ],
)
def test_invalid_or_stale_quote_never_values_position(ts: str | None, status: str) -> None:
    result = summarize(
        sample(100, timedelta(days=1)),
        sample(360, timedelta(hours=1)),
        {"instrument": "BTC-USDT", "cost": "100", "quantity": "2"},
        {"ticker": {"last": "90", "ts": ts}},
        asof=ASOF,
    )
    assert result["quote_status"] == status
    assert result["valuation_status"] == "UNAVAILABLE_QUOTE"
    assert result["quantity"] == "2"
    assert result["market_value_usdt"] is None
    assert result["unrealized_pnl_usdt"] is None


def test_quote_age_boundary_and_zero_quantity_are_explicit() -> None:
    result = summarize(
        sample(100, timedelta(days=1)),
        sample(360, timedelta(hours=1)),
        {"instrument": "BTC-USDT", "cost": "100", "quantity": "0"},
        {"ticker": {"last": "90", "ts": "1793279700000"}},
        asof=ASOF,
    )
    assert result["quote_status"] == "FRESH"
    assert result["quantity_status"] == "KNOWN"
    assert result["market_value_usdt"] == "0"


@pytest.mark.parametrize("quantity", ["NaN", "-0.01", "Infinity"])
def test_invalid_actual_quantity_rejected(quantity: str) -> None:
    with pytest.raises(ValueError):
        summarize(
            sample(100, timedelta(days=1)),
            sample(360, timedelta(hours=1)),
            {"instrument": "BTC-USDT", "cost": "100", "quantity": quantity},
            {"ticker": {"last": "90", "ts": "1793280000000"}},
            asof=ASOF,
        )


def test_render_separates_unknown_quantity_and_historical_snapshot() -> None:
    result = summarize(
        sample(100, timedelta(days=1)),
        sample(360, timedelta(hours=1)),
        {"instrument": "BTC-USDT", "cost": "100", "market_value_usdt": "90"},
        {"ticker": {"last": "90", "ts": "1793279699000"}},
        asof=ASOF,
    )
    output = render(
        {"started_at": ASOF.isoformat(), "requested_daily_days": 100, "positions": [result]}
    )
    assert "实际数量：未知" in output
    assert "当前持仓市值：不可用" in output
    assert "历史市值快照：90" in output
    assert "STALE" in output
    assert "反推数量" not in output
    assert "近似浮亏" not in output


def test_missing_thesis_is_pending_input_and_ignores_untrusted_position_and_ticker_fields() -> None:
    result = summarize(
        sample(100, timedelta(days=1)),
        sample(360, timedelta(hours=1)),
        {
            "instrument": "BTC-USDT",
            "cost": "100",
            "quantity": "2",
            "thesis_validation": "SUPPORTED",
            "thesis": {"status": "SUPPORTED"},
        },
        {"ticker": {"last": "90", "ts": "1793280000000"}, "thesis": {"status": "SUPPORTED"}},
        asof=ASOF,
    )
    assert result["thesis_validation"] == "MISSING_INPUT"
    assert result["thesis"]["buy_reason"] is None
    assert result["holding_policy"] == "thesis_hold"
    assert result["max_holding_hours"] is None
    assert result["action"] is None
    output = render(
        {"started_at": ASOF.isoformat(), "requested_daily_days": 100, "positions": [result]}
    )
    assert "长期逻辑状态：待补充" in output
    assert "已登记买入理由：待补充" in output
    assert "买卖动作：无" in output


@pytest.mark.parametrize(
    ("status", "label"),
    [("SUPPORTED", "材料支持"), ("CONFLICT", "存在冲突"), ("INVALIDATED", "逻辑失效")],
)
def test_registered_thesis_status_is_rendered_without_generating_trade_actions(
    status: str,
    label: str,
) -> None:
    thesis = thesis_report(status, label)
    result = summarize(
        sample(100, timedelta(days=1)),
        sample(360, timedelta(hours=1)),
        {"instrument": "BTC-USDT", "cost": "100", "quantity": "2"},
        {"ticker": {"last": "90", "ts": "1793280000000"}},
        asof=ASOF,
        thesis=thesis,
    )
    assert result["thesis_validation"] == status
    assert result["thesis"] == thesis
    assert result["action"] is None
    assert result["max_holding_hours"] is None
    output = render(
        {"started_at": ASOF.isoformat(), "requested_daily_days": 100, "positions": [result]}
    )
    assert f"长期逻辑状态：{label}" in output
    assert "具体投资逻辑尚未登记" not in output
    assert "买卖动作：无" in output


def test_hourly_break_and_72_hours_do_not_change_thesis_status() -> None:
    bullish = [
        replace(
            candle,
            open=candle.open + i,
            high=candle.high + i,
            low=candle.low + i,
            close=candle.close + i,
        )
        for i, candle in enumerate(sample(360, timedelta(hours=1)))
    ]
    bullish[-1] = replace(bullish[-1], close=bullish[-1].close + 5, high=bullish[-1].high + 5)
    bearish = [
        replace(
            candle,
            open=500 - candle.open,
            high=500 - candle.low,
            low=500 - candle.high,
            close=500 - candle.close,
        )
        for candle in bullish
    ]
    thesis = thesis_report("SUPPORTED", "材料支持")
    results = [
        summarize(
            sample(100, timedelta(days=1)),
            hourly,
            {"instrument": "BTC-USDT", "cost": "100", "quantity": "2"},
            {"ticker": {"last": "90", "ts": str(int(at.timestamp() * 1000))}},
            asof=at,
            thesis=thesis,
        )
        for hourly, at in ((bullish, ASOF), (bearish, ASOF + timedelta(hours=73)))
    ]
    assert results[0]["hourly_signal"] != results[1]["hourly_signal"]
    assert results[0]["thesis"] == results[1]["thesis"] == thesis
    assert all(
        result["action"] is None and result["max_holding_hours"] is None for result in results
    )


def test_thesis_prose_cannot_inject_markdown_structure() -> None:
    thesis = thesis_report("CONFLICT", "存在冲突")
    thesis["buy_reason"] = "理由\n# 伪造标题\n|额外|表格|<script>bad</script>"
    thesis["reasons"] = ["[危险链接](https://example.invalid)\n## 第二标题"]
    result = summarize(
        sample(100, timedelta(days=1)),
        sample(360, timedelta(hours=1)),
        {"instrument": "BTC-USDT", "cost": "100", "quantity": "2"},
        {"ticker": {"last": "90", "ts": "1793280000000"}},
        asof=ASOF,
        thesis=thesis,
    )
    output = render(
        {"started_at": ASOF.isoformat(), "requested_daily_days": 100, "positions": [result]}
    )
    assert "\n# 伪造标题" not in output
    assert "\n## 第二标题" not in output
    assert "[危险链接](" not in output
    assert "<script>" not in output
    assert r"\|额外\|表格\|" in output


def test_missing_thesis_database_fails_before_network_and_never_creates_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing = tmp_path / "missing.db"
    with pytest.raises(ValueError, match="does not exist"):
        load_thesis_summaries(missing, now=ASOF)
    positions = tmp_path / "positions.json"
    positions.write_text('[{"instrument":"BTC-USDT","cost":"100"}]', encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        ["review_positions", "--positions", str(positions), "--thesis-database", str(missing)],
    )

    def forbidden_client() -> None:
        raise AssertionError("must fail before public network is constructed")

    monkeypatch.setattr(review_runner, "DailyPublicClient", forbidden_client)
    with pytest.raises(SystemExit) as exc:
        review_runner.main()
    assert exc.value.code == 2
    assert not missing.exists()


def test_explicit_thesis_store_is_evaluated_read_only_and_supplied_to_review(
    tmp_path: Path,
) -> None:
    from app.portfolio.spot_thesis import Condition, ThesisRevision
    from app.portfolio.thesis_storage import ThesisStore

    database = tmp_path / "theses.db"
    store = ThesisStore(database)
    store.initialize()
    store.append_revision(
        ThesisRevision(
            id="synthetic-revision",
            instrument="BTC-USDT",
            recorded_at=ASOF - timedelta(hours=1),
            source="synthetic-test",
            buy_reason="合成的可检查理由",
            check_conditions=[Condition(id="check-1", text="合成支持条件", kind="fundamental")],
            invalidation_conditions=[
                Condition(id="invalidate-1", text="合成失效条件", kind="fundamental")
            ],
            review_interval_days=30,
        )
    )
    before = database.read_bytes()
    summaries = load_thesis_summaries(database, now=ASOF)
    assert database.read_bytes() == before
    assert summaries["BTC-USDT"]["status"] == "PENDING_REVIEW"
    assert summaries["ARB-USDT"]["status"] == "MISSING_INPUT"
    result = summarize(
        sample(100, timedelta(days=1)),
        sample(360, timedelta(hours=1)),
        {"instrument": "BTC-USDT", "cost": "100", "quantity": "2"},
        {"ticker": {"last": "90", "ts": "1793280000000"}},
        asof=ASOF,
        thesis=summaries["BTC-USDT"],
    )
    output = render(
        {"started_at": ASOF.isoformat(), "requested_daily_days": 100, "positions": [result]}
    )
    assert "长期逻辑状态：待复核" in output
    assert "合成的可检查理由" in output
    assert result["thesis"]["evaluated_at"] == ASOF.isoformat()
    assert result["action"] is None


def test_default_review_does_not_load_personal_thesis_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    positions = tmp_path / "positions.json"
    positions.write_text(
        '[{"instrument":"BTC-USDT","cost":"100","quantity":"1"}]', encoding="utf-8"
    )
    now = datetime.now(UTC)

    class PublicFixture:
        def server_time(self) -> datetime:
            return now

        def instruments(self) -> dict[str, dict[str, str]]:
            return {"BTC-USDT": {"state": "live"}}

        def candles(self, instrument: str, **kwargs: Any) -> list[Candle]:
            return (
                sample(100, timedelta(days=1))
                if kwargs.get("bar") == "1d"
                else sample(360, timedelta(hours=1))
            )

        def context(self, instrument: str, **kwargs: Any) -> dict[str, Any]:
            return {"ticker": {"last": "90", "ts": str(int(now.timestamp() * 1000))}}

        def close(self) -> None:
            pass

    def forbidden_load(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("default command must not read personal thesis files")

    monkeypatch.setattr(review_runner, "DailyPublicClient", PublicFixture)
    monkeypatch.setattr(review_runner, "load_thesis_summaries", forbidden_load)
    output = tmp_path / "output"
    monkeypatch.setattr(
        sys,
        "argv",
        ["review_positions", "--positions", str(positions), "--output-root", str(output)],
    )
    review_runner.main()
    reports = list(output.glob("*/analysis.json"))
    assert len(reports) == 1
    result = json.loads(reports[0].read_text(encoding="utf-8"))
    assert result["positions"][0]["thesis_validation"] == "MISSING_INPUT"


def test_short_listing_history_does_not_invent_ma200() -> None:
    result = summarize(
        sample(100, timedelta(days=1)),
        sample(360, timedelta(hours=1)),
        {"instrument": "XQQQ-USDT", "cost": "100", "market_value_usdt": "90"},
        {"ticker": {"last": "90", "ts": "1793280000000"}},
    )
    assert result["daily_sma"]["200"] is None
    assert result["daily_return_pct"]["365"] is None
    assert result["daily_bars"] == 100


@pytest.mark.parametrize("value", ["NaN", "0", "-1"])
def test_invalid_position_rejected(value: str) -> None:
    with pytest.raises(ValueError):
        summarize(
            sample(100, timedelta(days=1)),
            sample(360, timedelta(hours=1)),
            {"instrument": "BTC-USDT", "cost": value, "market_value_usdt": "90"},
            {"ticker": {"last": "90", "ts": "1793280000000"}},
        )


def test_daily_history_uses_utc_calendar_and_excludes_unfinished_bar() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    rows = [
        [
            str(int((start + timedelta(days=i)).timestamp() * 1000)),
            "100",
            "102",
            "99",
            "101",
            "999",
            "10",
            "1000",
            "1" if i < 3 else "0",
        ]
        for i in range(4)
    ]
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.params["bar"] == "1Dutc"
        return httpx.Response(200, json={"code": "0", "data": rows if len(requests) == 1 else []})

    client = DailyPublicClient(
        httpx.Client(transport=httpx.MockTransport(handler)), sleep=lambda _: None
    )
    try:
        result = client.candles("XQQQ-USDT", cutoff=start + timedelta(days=3), days=730, bar="1d")
        assert len(result) == 3
        assert all(x.timestamp.hour == 0 and x.confirmed for x in result)
        assert all(x.volume == 10 for x in result)
        assert len(requests) == 2
    finally:
        client.close()


@pytest.mark.parametrize("kwargs", [{"days": 0}, {"days": 3651}, {"bar": "invalid"}])
def test_bad_history_request_rejected_before_network(kwargs: dict[str, Any]) -> None:
    def forbidden(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not request invalid history")

    client = DailyPublicClient(
        httpx.Client(transport=httpx.MockTransport(forbidden)), sleep=lambda _: None
    )
    try:
        args: dict[str, Any] = {"cutoff": datetime.now(UTC), "days": 30, **kwargs}
        with pytest.raises(ValueError):
            client.candles("BTC-USDT", **args)
    finally:
        client.close()
