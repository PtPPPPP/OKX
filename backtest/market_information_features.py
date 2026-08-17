"""Causal 1H alignment and transparent derivatives-information features."""

from __future__ import annotations

import csv
import hashlib
import json
from bisect import bisect_right
from collections.abc import Sequence
from datetime import UTC, timedelta
from pathlib import Path
from statistics import mean, pstdev
from typing import Any

from backtest.vwap_signal_edge import _volatility_regime
from backtest.vwap_signal_edge_data import load_normalized_candles

ONE_HOUR = timedelta(hours=1)


def build_market_information_features(
    spot_cache: Path, data_root: Path, output: Path
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    spot = load_normalized_candles(spot_cache / "normalized" / "candles.csv", bar="1h")
    perp = {int(row["timestamp"]): row for row in _read_csv(data_root / "normalized" / "perp.csv")}
    funding_rows = sorted(
        _read_csv(data_root / "normalized" / "funding.csv"), key=lambda row: int(row["fundingTime"])
    )
    oi_rows = sorted(
        _read_csv(data_root / "normalized" / "open_interest.csv"),
        key=lambda row: int(row["timestamp"]),
    )
    funding_times = [int(row["fundingTime"]) for row in funding_rows]
    oi_times = [int(row["timestamp"]) for row in oi_rows]
    basis_history: list[float] = []
    funding_history: list[float] = []
    last_funding_timestamp: int | None = None
    rows: list[dict[str, Any]] = []
    for index, candle in enumerate(spot):
        stamp_ms = int(candle.timestamp.timestamp() * 1000)
        decision = candle.timestamp + ONE_HOUR
        decision_ms = int(decision.timestamp() * 1000)
        perp_row = perp.get(stamp_ms)
        perp_close = float(perp_row["close"]) if perp_row is not None else None
        basis = perp_close / float(candle.close) - 1 if perp_close is not None else None
        if basis is not None:
            basis_history.append(basis)
        funding_row, funding_age = _asof(funding_rows, funding_times, decision_ms)
        oi_row, oi_age = _asof(oi_rows, oi_times, decision_ms)
        funding = (
            float(funding_row["realizedRate"] or funding_row["fundingRate"])
            if funding_row
            else None
        )
        funding_timestamp = int(funding_row["fundingTime"]) if funding_row else None
        if funding is not None and funding_timestamp != last_funding_timestamp:
            funding_history.append(funding)
            last_funding_timestamp = funding_timestamp
        oi = float(oi_row["oi_btc"]) if oi_row else None
        feature = {
            "timestamp": candle.timestamp.astimezone(UTC).isoformat(),
            "decision_time": decision.astimezone(UTC).isoformat(),
            "spot_close": float(candle.close),
            "spot_return_1h": float(candle.close) / float(spot[index - 1].close) - 1
            if index
            else None,
            "perp_reference_price": perp_close,
            "basis_pct": basis,
            "basis_change_1h": _change(basis_history, 1),
            "basis_change_4h": _change(basis_history, 4),
            "basis_change_24h": _change(basis_history, 24),
            "basis_rolling_zscore": _zscore(basis_history, 720),
            "basis_state": _causal_bucket(
                basis_history,
                ("deep_discount", "discount", "neutral", "premium", "high_premium"),
                720,
            ),
            "basis_source_age_seconds": 0 if basis is not None else None,
            "basis_quality": "READY" if basis is not None else "MISSING",
            "funding_rate": funding,
            "funding_change": _change(funding_history, 1),
            "funding_zscore_rolling": _zscore(funding_history, 90),
            "funding_percentile_rolling": _percentile(funding_history, 90),
            "funding_sign": "positive"
            if funding is not None and funding > 0
            else "negative"
            if funding is not None and funding < 0
            else "zero"
            if funding is not None
            else None,
            "funding_state": _causal_bucket(
                funding_history,
                ("very_negative", "negative", "neutral", "positive", "very_positive"),
                90,
            ),
            "funding_source_age_seconds": funding_age,
            "funding_quality": "READY"
            if funding is not None and funding_age is not None and funding_age <= 36_000
            else "STALE"
            if funding is not None
            else "MISSING",
            "open_interest_contracts": float(oi_row["oi_contracts"]) if oi_row else None,
            "open_interest_btc": oi,
            "open_interest_usd": float(oi_row["oi_usd"]) if oi_row else None,
            "oi_change_1h": _lag_change(oi, rows, 1, absolute=True),
            "oi_change_4h": _lag_change(oi, rows, 4, absolute=True),
            "oi_change_24h": _lag_change(oi, rows, 24, absolute=True),
            "oi_pct_change_1h": _lag_change(oi, rows, 1),
            "oi_pct_change_4h": _lag_change(oi, rows, 4),
            "oi_pct_change_24h": _lag_change(oi, rows, 24),
            "oi_source_age_seconds": oi_age,
            "oi_quality": "READY"
            if oi is not None and oi_age is not None and oi_age <= 7_200
            else "STALE"
            if oi is not None
            else "MISSING",
            "price_oi_quadrant": _quadrant(rows, float(candle.close), oi),
            "price_direction": "up"
            if index and candle.close >= spot[index - 1].close
            else "down"
            if index
            else None,
            "volume": float(candle.volume),
            "volatility_context": _volatility_regime(spot, index),
            "market_context": _market_context(spot, index),
        }
        rows.append(feature)
    output.parent.mkdir(parents=True, exist_ok=True)
    _write_csv(output, rows)
    canonical = json.dumps(rows, sort_keys=True, separators=(",", ":"), allow_nan=False)
    manifest = {
        "rows": len(rows),
        "first_timestamp": rows[0]["timestamp"] if rows else None,
        "last_timestamp": rows[-1]["timestamp"] if rows else None,
        "dataset_hash": hashlib.sha256(canonical.encode()).hexdigest(),
        "funding_lookahead_bias": False,
        "oi_lookahead_bias": False,
        "basis_lookahead_bias": False,
        "alignment": "bounded backward as-of at confirmed 1H decision time; basis exact-bar close",
        "prospective_excluded": True,
    }
    return rows, manifest


def _asof(rows: list[Any], times: list[int], decision_ms: int) -> tuple[Any | None, int | None]:
    position = bisect_right(times, decision_ms) - 1
    if position < 0:
        return None, None
    return rows[position], (decision_ms - times[position]) // 1000


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as file:
        return list(csv.DictReader(file))


def _change(values: Sequence[float], lag: int) -> float | None:
    return values[-1] - values[-lag - 1] if len(values) > lag else None


def _lag_change(
    value: float | None, rows: list[dict[str, Any]], lag: int, *, absolute: bool = False
) -> float | None:
    if value is None or len(rows) < lag:
        return None
    previous = rows[-lag].get("open_interest_btc")
    if previous is None or float(previous) == 0:
        return None
    return value - float(previous) if absolute else value / float(previous) - 1


def _zscore(values: Sequence[float], window: int) -> float | None:
    if len(values) < window:
        return None
    sample = values[-window:]
    deviation = pstdev(sample)
    return (sample[-1] - mean(sample)) / deviation if deviation else 0.0


def _percentile(values: Sequence[float], window: int) -> float | None:
    if len(values) < window:
        return None
    sample = values[-window:]
    return sum(value <= sample[-1] for value in sample) / len(sample)


def _causal_bucket(values: Sequence[float], labels: tuple[str, ...], window: int) -> str | None:
    if len(values) < window:
        return None
    reference = sorted(values[-window:-1])
    value = values[-1]
    cuts = [reference[int((len(reference) - 1) * quantile)] for quantile in (0.1, 0.3, 0.7, 0.9)]
    return labels[sum(value > cut for cut in cuts)]


def _quadrant(rows: list[dict[str, Any]], price: float, oi: float | None) -> str | None:
    if not rows or oi is None or rows[-1].get("open_interest_btc") is None:
        return None
    return f"price_{'up' if price >= float(rows[-1]['spot_close']) else 'down'}_oi_{'up' if oi >= float(rows[-1]['open_interest_btc']) else 'down'}"


def _market_context(spot: Sequence[Any], index: int) -> str | None:
    if index < 720:
        return None
    reference = mean(float(item.close) for item in spot[index - 720 : index])
    ratio = float(spot[index].close) / reference - 1
    return "bull" if ratio > 0.02 else "bear" if ratio < -0.02 else "sideways"


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
