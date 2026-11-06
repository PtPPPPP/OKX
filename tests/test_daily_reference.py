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
from pydantic import ValidationError

import app.daily_reference as model
import scripts.daily_reference as runner
from app.daily_reference import Features, ReferenceConfig, analyze, calculate_features, replay
from app.domain.market import Candle
from app.market.daily_public import DailyPublicClient
from app.market.historical_data import MarketDataError
from scripts.daily_reference import render_report

D = Decimal
START = datetime(2026, 1, 1, tzinfo=UTC)
HOUR = timedelta(hours=1)


def bars(count: int = 320) -> list[Candle]:
    result = []
    for i in range(count):
        close = D("100") + D(i) / 10
        result.append(
            Candle(
                START + i * HOUR,
                close - D("0.05"),
                close + D("0.15"),
                close - D("0.2"),
                close,
                D("100"),
                True,
            )
        )
    return result


def config(**kwargs: Any) -> ReferenceConfig:
    return ReferenceConfig(instruments=["BTC-USDT"], **kwargs)


def test_breakout_excludes_signal_candle_and_uses_completed_trend() -> None:
    data = bars()
    previous_high = max(x.high for x in data[-25:-1])
    data[-1] = replace(data[-1], close=previous_high + 1, high=previous_high + 2)
    f = calculate_features(data, config(), short=False)
    assert f.prior_high == previous_high
    assert f.direction == 1 and f.trend == "up"
    assert f.decision_time == START + 320 * HOUR


def test_future_and_unconfirmed_candles_cannot_change_current_analysis() -> None:
    data = bars()
    cutoff = START + 320 * HOUR
    first = analyze("BTC-USDT", data, config(), tick=D("0.1"), asof=cutoff)
    future = replace(data[-1], timestamp=cutoff, close=D("999"), high=D("1000"), confirmed=False)
    second = analyze("BTC-USDT", [*data, future], config(), tick=D("0.1"), asof=cutoff)
    assert first == second
    third = analyze(
        "BTC-USDT", [*data, replace(future, confirmed=True)], config(), tick=D("0.1"), asof=cutoff
    )
    assert first == third


def test_spot_cannot_generate_short() -> None:
    data = [
        replace(x, open=300 - x.open, high=300 - x.low, low=300 - x.high, close=300 - x.close)
        for x in bars()
    ]
    low = min(x.low for x in data[-25:-1])
    data[-1] = replace(data[-1], close=low - 1, low=low - 2)
    assert calculate_features(data, config(), short=False).direction == 0
    assert calculate_features(data, config(), short=True).direction == -1


@pytest.mark.parametrize("failure", ["gap", "stale", "nan", "naive", "off_hour"])
def test_bad_data_rejected(failure: str) -> None:
    data = bars()
    cutoff = START + 320 * HOUR
    if failure == "gap":
        del data[50]
    elif failure == "stale":
        cutoff += 3 * HOUR
    elif failure == "nan":
        data[-1] = replace(data[-1], close=D("NaN"))
    elif failure == "naive":
        data[0] = replace(data[0], timestamp=data[0].timestamp.replace(tzinfo=None))
    else:
        data = [replace(x, timestamp=x.timestamp + timedelta(minutes=1)) for x in data]
        cutoff += HOUR
    with pytest.raises(ValueError):
        analyze("BTC-USDT", data, config(), tick=D("0.1"), asof=cutoff)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_holding_hours": 73},
        {"stop_atr": "NaN"},
        {"fee_bps_per_side": -1},
        {"trend_fast_4h": 50},
        {"unknown_field": True},
    ],
)
def test_invalid_parameters_rejected(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        config(**kwargs)


def inject_one_signal(monkeypatch: pytest.MonkeyPatch, *, direction: int = 1) -> None:
    def feature(data: list[Candle], cfg: ReferenceConfig, *, short: bool) -> Features:
        return Features(
            data[-1].timestamp + HOUR,
            D("100"),
            D("2"),
            D("2"),
            D("101"),
            D("99"),
            "up",
            D("1"),
            D("0"),
            direction if len(data) == 216 else 0,
        )

    monkeypatch.setattr(model, "calculate_features", feature)


@pytest.mark.parametrize("direction", [1, -1])
def test_next_open_entry_and_ambiguous_bar_stop_first(
    monkeypatch: pytest.MonkeyPatch, direction: int
) -> None:
    inject_one_signal(monkeypatch, direction=direction)
    data = bars()
    data[216] = replace(data[216], open=D("100"), high=D("110"), low=D("90"), close=D("100"))
    r = replay(
        data,
        config(fee_bps_per_side=0, slippage_bps_per_side=0),
        short=True,
        tick=D("0.1"),
        decision_hour=0,
    )
    trade = r["trades"][0]
    assert trade["entry_time"] == data[216].timestamp.isoformat()
    assert trade["exit_reason"] == "stop"
    assert D(trade["exit"]) == (D("97") if direction == 1 else D("103"))
    assert trade["return_after_assumed_fees_slippage_pct"] == -3


def test_gap_stop_uses_worse_open(monkeypatch: pytest.MonkeyPatch) -> None:
    inject_one_signal(monkeypatch)
    data = bars()
    data[216] = replace(data[216], open=D("100"), high=D("101"), low=D("99"), close=D("100"))
    data[217] = replace(data[217], open=D("90"), high=D("91"), low=D("89"), close=D("90"))
    r = replay(
        data,
        config(fee_bps_per_side=0, slippage_bps_per_side=0),
        short=False,
        tick=D("0.1"),
        decision_hour=0,
    )
    assert r["trades"][0]["exit_reason"] == "stop_gap"
    assert D(r["trades"][0]["exit"]) == 90


def test_72_hour_timeout_costs_and_unfinished_trade(monkeypatch: pytest.MonkeyPatch) -> None:
    inject_one_signal(monkeypatch)
    data = [replace(x, open=D("100"), high=D("101"), low=D("99"), close=D("100")) for x in bars()]
    r = replay(data, config(), short=False, tick=D("0.1"), decision_hour=0)
    trade = r["trades"][0]
    assert trade["exit_reason"] == "time_limit"
    assert (
        datetime.fromisoformat(trade["exit_bar_open"])
        + HOUR
        - datetime.fromisoformat(trade["entry_time"])
        == 72 * HOUR
    )
    assert trade["return_after_assumed_fees_slippage_pct"] < 0
    partial = replay(data[:230], config(), short=False, tick=D("0.1"), decision_hour=0)
    assert partial["right_censored_trades"] == 1
    assert partial["closed_trades"] == 0


def test_entry_gap_beyond_stop_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    inject_one_signal(monkeypatch)
    data = bars()
    data[216] = replace(data[216], open=D("90"))
    assert replay(data, config(), short=False, tick=D("0.1"), decision_hour=0)["closed_trades"] == 0


def test_public_client_allowlist_retry_and_no_auth() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.method == "GET"
        assert not any(key.lower().startswith("ok-access") for key in request.headers)
        return httpx.Response(429 if len(calls) < 3 else 200, json={"code": "0", "data": []})

    client = DailyPublicClient(
        httpx.Client(transport=httpx.MockTransport(handler)), sleep=lambda _: None
    )
    try:
        assert client.get("/api/v5/public/instruments", {"instType": "SPOT"}) == []
        assert len(calls) == 3
        with pytest.raises(ValueError, match="allowlisted"):
            client.get("/api/v5/trade/order", {})
        assert len(calls) == 3
    finally:
        client.close()


def test_public_candles_drop_unfinished_use_base_volume_and_check_gaps() -> None:
    cutoff = START + 4 * HOUR
    rows = [
        [
            str(int((START + i * HOUR).timestamp() * 1000)),
            "10",
            "12",
            "9",
            "11",
            "999",
            "2",
            "22",
            "1" if i < 4 else "0",
        ]
        for i in range(5)
    ]
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"code": "0", "data": rows if calls % 2 else []})

    client = DailyPublicClient(
        httpx.Client(transport=httpx.MockTransport(handler)), sleep=lambda _: None
    )
    try:
        result = client.candles("BTC-USDT-SWAP", cutoff=cutoff, days=15)
        assert len(result) == 4 and all(x.volume == 2 for x in result)
        del rows[1]
        with pytest.raises(MarketDataError, match="缺失"):
            client.candles("BTC-USDT-SWAP", cutoff=cutoff, days=15)
    finally:
        client.close()


def test_default_watchlist_and_report_cannot_invent_probabilities() -> None:
    cfg = ReferenceConfig.model_validate_json(Path("configs/daily_reference.json").read_text())
    assert "XAUT-USDT" in cfg.instruments and "XAU-USDT-SWAP" in cfg.instruments
    assert "XINTC-USDT" in cfg.instruments and "XQQQ-USDT" in cfg.instruments
    assert "INTC-USDT" not in cfg.instruments and "QQQ-USDT" not in cfg.instruments
    item = analyze("BTC-USDT", bars(), cfg, tick=D("0.1"), asof=START + 320 * HOUR)
    assert item["prediction"]["up_probability"] is None
    report = {
        "asof": item["asof"],
        "asof_beijing": "fixture",
        "config": cfg.model_dump(mode="json"),
        "instruments": [
            item,
            {"instrument": "QQQ-USDT", "status": "UNAVAILABLE", "reason": "未上市"},
        ],
    }
    text = render_report(report)
    assert "UNAVAILABLE" in text and "未上市" in text and "未模拟" in text
    assert "样本外" in text and "预测概率和预测区间留空" in text
    json.dumps(item, allow_nan=False)


@pytest.mark.parametrize("bad_value", ["NaN", "-1", "not-a-number"])
def test_malformed_context_is_partial_not_a_valid_quote(bad_value: str) -> None:
    now = datetime.now(UTC)

    def handler(request: httpx.Request) -> httpx.Response:
        row = {
            "instId": "BTC-USDT",
            "ts": str(int(now.timestamp() * 1000)),
            "bidPx": bad_value,
            "askPx": "100",
            "last": "99",
        }
        return httpx.Response(200, json={"code": "0", "data": [row]})

    client = DailyPublicClient(
        httpx.Client(transport=httpx.MockTransport(handler)), sleep=lambda _: None
    )
    try:
        context = client.context("BTC-USDT", cutoff=now)
        assert context["quality"] == "PARTIAL"
        assert "ticker" not in context
    finally:
        client.close()


def test_saved_run_replays_without_network_and_rejects_tampering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime.now(UTC)
    cutoff = now.replace(minute=0, second=0, microsecond=0)
    sample = [replace(c, timestamp=cutoff - (320 - i) * HOUR) for i, c in enumerate(bars())]

    class FakePublic:
        def server_time(self) -> datetime:
            return now

        def instruments(self) -> dict[str, Any]:
            return {"BTC-USDT": {"instId": "BTC-USDT", "state": "live", "tickSz": "0.1"}}

        def candles(self, instrument: str, *, cutoff: datetime, days: int) -> list[Candle]:
            return sample

        def context(self, instrument: str, *, cutoff: datetime) -> dict[str, Any]:
            return {"quality": "READY", "warnings": []}

        def close(self) -> None:
            pass

    monkeypatch.setattr(runner, "DailyPublicClient", FakePublic)
    original = tmp_path / "original"
    assert runner.run(config(), original)[1] == 0

    def forbidden_network() -> None:
        raise AssertionError("offline replay must never initialize a network client")

    monkeypatch.setattr(runner, "DailyPublicClient", forbidden_network)
    restored = tmp_path / "restored"
    assert runner.run(config(), restored, source=original)[1] == 0
    a = json.loads((original / "report.json").read_text(encoding="utf-8"))
    b = json.loads((restored / "report.json").read_text(encoding="utf-8"))
    assert a["instruments"] == b["instruments"]
    assert a["data_hashes"] == b["data_hashes"]
    assert "离线复现" in (restored / "report.md").read_text(encoding="utf-8")
    path = original / "BTC-USDT.csv"
    path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    assert runner.run(config(), tmp_path / "tampered", source=original)[1] == 2


def test_long_term_spot_never_uses_tactical_exit_or_replay(monkeypatch: pytest.MonkeyPatch) -> None:
    data = bars()
    high = max(x.high for x in data[-25:-1])
    data[-1] = replace(data[-1], close=high + 1, high=high + 2)

    def forbidden(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError("long-term spot must not invoke tactical replay")

    monkeypatch.setattr(model, "replay", forbidden)
    item = analyze("BTC-USDT", data, config(), tick=D("0.1"), asof=START + 320 * HOUR)
    assert item["plan"]["status"] == "OBSERVE"
    assert "stop" not in item["plan"] and "target" not in item["plan"]
    assert item["max_holding_hours"] is None
    assert item["replay"]["status"] == "NOT_APPLICABLE_LONG_TERM"


def test_contract_keeps_tactical_plan_and_72_hour_limit() -> None:
    data = bars()
    high = max(x.high for x in data[-25:-1])
    data[-1] = replace(data[-1], close=high + 1, high=high + 2)
    item = analyze("BTC-USDT-SWAP", data, config(), tick=D("0.1"), asof=START + 320 * HOUR)
    assert item["plan"]["status"] == "LONG"
    assert "stop" in item["plan"] and "target" in item["plan"]
    assert item["max_holding_hours"] == 72
    assert item["replay"]["status"] == "UNVALIDATED_PRICE_PATH_REPLAY"


def test_old_config_replay_preserves_historical_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "legacy"
    source.mkdir()
    legacy = config().model_dump(mode="json")
    del legacy["spot_policy"]
    (source / "config.json").write_text(json.dumps(legacy), encoding="utf-8")

    def capture(
        cfg: ReferenceConfig, output: Path, *, source: Path | None = None
    ) -> tuple[Path, int]:
        assert cfg.spot_policy == "tactical_research"
        assert source is not None
        return output / "report.md", 0

    monkeypatch.setattr(runner, "run", capture)
    monkeypatch.setattr(
        sys,
        "argv",
        ["daily_reference", "--replay-run", str(source), "--output-root", str(tmp_path / "out")],
    )
    with pytest.raises(SystemExit) as caught:
        runner.main()
    assert caught.value.code == 0
