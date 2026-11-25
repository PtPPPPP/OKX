"""Transparent daily observations and an unvalidated, configurable baseline."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from statistics import mean
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.domain.market import Candle
from app.market.historical_data import aggregate_completed, normalize_candles

HOUR = timedelta(hours=1)
ZERO = Decimal("0")


class ReferenceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    instruments: list[str] = Field(min_length=1, max_length=30)
    spot_policy: Literal["thesis_hold", "tactical_research"] = "thesis_hold"
    history_days: int = Field(default=90, ge=15, le=365)
    breakout_hours: int = Field(default=24, ge=2, le=168)
    atr_hours: int = Field(default=14, ge=2, le=100)
    trend_fast_4h: int = Field(default=20, ge=2, le=100)
    trend_slow_4h: int = Field(default=50, ge=3, le=200)
    stop_atr: Decimal = Field(default=Decimal("1.5"), gt=0, le=10, allow_inf_nan=False)
    target_atr: Decimal = Field(default=Decimal("3"), gt=0, le=20, allow_inf_nan=False)
    max_holding_hours: int = Field(default=72, ge=1, le=72)
    fee_bps_per_side: Decimal = Field(default=Decimal("10"), ge=0, le=100, allow_inf_nan=False)
    slippage_bps_per_side: Decimal = Field(default=Decimal("5"), ge=0, le=100, allow_inf_nan=False)

    @field_validator("instruments")
    @classmethod
    def validate_instruments(cls, values: list[str]) -> list[str]:
        if len(set(values)) != len(values) or any(
            not re.fullmatch(r"[A-Z0-9]+-USDT(?:-SWAP)?", x) for x in values
        ):
            raise ValueError("use unique USDT SPOT/SWAP instrument IDs")
        return values

    @model_validator(mode="after")
    def validate_windows(self) -> ReferenceConfig:
        if self.trend_fast_4h >= self.trend_slow_4h:
            raise ValueError("fast trend period must be smaller than slow period")
        if self.history_days * 24 <= self.trend_slow_4h * 4 + 4:
            raise ValueError("history must cover the trend warmup")
        return self


@dataclass(frozen=True)
class Features:
    decision_time: datetime
    close: Decimal
    atr: Decimal
    atr_pct: Decimal
    prior_high: Decimal
    prior_low: Decimal
    trend: str
    relative_volume: Decimal | None
    return_24h_pct: Decimal
    direction: int


def calculate_features(candles: list[Candle], cfg: ReferenceConfig, *, short: bool) -> Features:
    required = max(cfg.breakout_hours + 1, cfg.atr_hours + 1, 25, cfg.trend_slow_4h * 4 + 4)
    if len(candles) < required:
        raise ValueError(f"insufficient confirmed history: need {required} hourly candles")
    if any(not c.confirmed for c in candles):
        raise ValueError("unconfirmed candles are forbidden")
    bars = aggregate_completed(candles, hours=4)
    if len(bars) < cfg.trend_slow_4h:
        raise ValueError("insufficient completed 4H bars")
    fast = sum((c.close for c in bars[-cfg.trend_fast_4h :]), ZERO) / cfg.trend_fast_4h
    slow = sum((c.close for c in bars[-cfg.trend_slow_4h :]), ZERO) / cfg.trend_slow_4h
    trend = (
        "up"
        if fast > slow and bars[-1].close > slow
        else ("down" if fast < slow and bars[-1].close < slow else "mixed")
    )
    ranges = [
        max(c.high - c.low, abs(c.high - p.close), abs(c.low - p.close))
        for p, c in zip(candles[-cfg.atr_hours - 1 : -1], candles[-cfg.atr_hours :], strict=True)
    ]
    atr = sum(ranges, ZERO) / cfg.atr_hours
    last = candles[-1]
    previous = candles[-cfg.breakout_hours - 1 : -1]
    high, low = max(c.high for c in previous), min(c.low for c in previous)
    volume_mean = sum((c.volume for c in candles[-21:-1]), ZERO) / 20
    direction = 0
    if atr > 0 and last.volume > 0:
        if trend == "up" and last.close > high:
            direction = 1
        elif short and trend == "down" and last.close < low:
            direction = -1
    return Features(
        last.timestamp + HOUR,
        last.close,
        atr,
        atr / last.close * 100,
        high,
        low,
        trend,
        last.volume / volume_mean if volume_mean else None,
        (last.close / candles[-25].close - 1) * 100,
        direction,
    )


def calculate_ma_bollinger_snapshot(
    candles: list[Candle],
    *,
    trend_ma_period: int = 200,
    trend_tolerance_pct: Decimal = Decimal("0.03"),
    bollinger_period: int = 48,
    bollinger_std_dev: Decimal = Decimal("2.0"),
    min_bandwidth_pct: Decimal = Decimal("0.035"),
    atr_period: int = 24,
    atr_stop_multiplier: Decimal = Decimal("3.0"),
    reference_equity_usdt: Decimal = Decimal("10000"),
    risk_pct: Decimal = Decimal("0.01"),
    max_notional_pct: Decimal = Decimal("0.30"),
) -> dict[str, Any]:
    """Compute causal EMA200 + BB(48, 2.0) + 3xATR24 and risk-based sizing reference."""
    effective_trend_period = min(trend_ma_period, max(20, len(candles) - 1))
    effective_bb_period = min(bollinger_period, len(candles))
    effective_atr_period = min(atr_period, max(2, len(candles) - 1))

    closes = [c.close for c in candles]
    last_close = closes[-1]

    # EMA trend calculation seeded with initial SMA
    alpha = Decimal("2") / Decimal(effective_trend_period + 1)
    ema = sum(closes[:effective_trend_period], ZERO) / Decimal(effective_trend_period)
    prev_ema = ema
    for price in closes[effective_trend_period:]:
        prev_ema = ema
        ema = price * alpha + ema * (Decimal("1") - alpha)
    trend_rising = ema >= prev_ema
    trend_floor = ema * (Decimal("1") - trend_tolerance_pct)
    trend_ok = last_close >= trend_floor and trend_rising

    # Bollinger Bands
    bb_window = closes[-effective_bb_period:]
    bb_middle = sum(bb_window, ZERO) / Decimal(effective_bb_period)
    variance = sum(((p - bb_middle) ** 2 for p in bb_window), ZERO) / Decimal(effective_bb_period)
    std_dev = variance.sqrt()
    bb_upper = bb_middle + bollinger_std_dev * std_dev
    bb_lower = bb_middle - bollinger_std_dev * std_dev
    band_span = bb_upper - bb_lower
    bandwidth = band_span / bb_middle if bb_middle > 0 else ZERO
    percent_b = (last_close - bb_lower) / band_span if band_span > 0 else Decimal("0.5")
    distance_to_lower_pct = (
        (last_close - bb_lower) / bb_lower * Decimal("100") if bb_lower > 0 else ZERO
    )

    # ATR and stop-distance reference
    atr_ranges = [
        max(c.high - c.low, abs(c.high - p.close), abs(c.low - p.close))
        for p, c in zip(
            candles[-effective_atr_period - 1 : -1],
            candles[-effective_atr_period:],
            strict=True,
        )
    ]
    atr_val = sum(atr_ranges, ZERO) / Decimal(effective_atr_period)
    fixed_stop = last_close * (Decimal("1") - Decimal("0.045"))
    atr_stop = last_close - atr_val * atr_stop_multiplier
    stop_price = max(fixed_stop, atr_stop)
    stop_distance = (
        (last_close - stop_price) / last_close if last_close > stop_price > 0 else Decimal("0.045")
    )

    # Risk-per-trade notional reference (per 10,000 USDT equity)
    raw_notional = (reference_equity_usdt * risk_pct) / stop_distance
    suggested_notional = min(raw_notional, reference_equity_usdt * max_notional_pct)

    if trend_ok and bandwidth >= min_bandwidth_pct and last_close <= bb_lower:
        zone = "PULLBACK_BUY_ZONE"
        zone_label = "顺势下轨低吸区"
    elif last_close >= bb_upper:
        zone = "UPPER_BAND_EXTENSION"
        zone_label = "触及上轨超买区"
    elif trend_ok and last_close >= bb_middle:
        zone = "UPTREND_ABOVE_MID"
        zone_label = "上升趋势中轨上方"
    elif trend_ok:
        zone = "UPTREND_PULLBACK_WAIT"
        zone_label = "上升趋势回踩观察"
    else:
        zone = "BELOW_TREND_WAIT"
        zone_label = "趋势均线下方观望"

    return {
        "trend_ma_type": "ema",
        "trend_ma_period": effective_trend_period,
        "trend_ma": str(ema),
        "trend_floor": str(trend_floor),
        "trend_rising": trend_rising,
        "bollinger_period": effective_bb_period,
        "bollinger_std_dev": str(bollinger_std_dev),
        "bb_upper": str(bb_upper),
        "bb_middle": str(bb_middle),
        "bb_lower": str(bb_lower),
        "bandwidth_pct": float(bandwidth * Decimal("100")),
        "percent_b": float(percent_b),
        "distance_to_lower_pct": float(distance_to_lower_pct),
        "atr_period": effective_atr_period,
        "atr": str(atr_val),
        "protective_stop_price": str(stop_price),
        "stop_distance_pct": float(stop_distance * Decimal("100")),
        "risk_sizing_notional_per_10k_usdt": float(suggested_notional.quantize(Decimal("0.01"))),
        "signal_zone": zone,
        "signal_zone_label": zone_label,
    }


def levels(feature: Features, cfg: ReferenceConfig, tick: Decimal) -> tuple[Decimal, Decimal]:
    if not tick.is_finite() or tick <= 0 or feature.direction not in {-1, 1}:
        raise ValueError("valid tick and triggered direction required")
    stop = feature.close - feature.direction * cfg.stop_atr * feature.atr
    target = feature.close + feature.direction * cfg.target_atr * feature.atr
    rounding = ROUND_FLOOR if feature.direction == 1 else ROUND_CEILING
    stop = (stop / tick).to_integral_value(rounding=rounding) * tick
    target = (target / tick).to_integral_value(rounding=rounding) * tick
    if min(stop, target) <= 0 or feature.direction * (target - feature.close) <= 0:
        raise ValueError("invalid price levels")
    return stop, target


def replay(
    candles: list[Candle],
    cfg: ReferenceConfig,
    *,
    short: bool,
    tick: Decimal,
    decision_hour: int,
) -> dict[str, Any]:
    """Daily samples, next-open entry, conservative OHLC barriers; no leverage."""
    trades: list[dict[str, Any]] = []
    slip = cfg.slippage_bps_per_side / 10000
    fee = cfg.fee_bps_per_side / 10000
    i = max(cfg.trend_slow_4h * 4 + 4, cfg.breakout_hours + 1, cfg.atr_hours + 1, 25) - 1
    censored = 0
    while i < len(candles) - 1:
        if (candles[i].timestamp + HOUR).hour != decision_hour:
            i += 1
            continue
        f = calculate_features(candles[: i + 1], cfg, short=short)
        if not f.direction:
            i += 1
            continue
        stop, target = levels(f, cfg, tick)
        entry_bar = candles[i + 1]
        entry = entry_bar.open * (1 + f.direction * slip)
        if not (stop < entry < target if f.direction == 1 else target < entry < stop):
            i += 1
            continue
        deadline = entry_bar.timestamp + timedelta(hours=cfg.max_holding_hours)
        exit_price: Decimal | None = None
        reason = ""
        j = i + 1
        for j in range(i + 1, len(candles)):
            c = candles[j]
            # Opening gaps have known order; intrabar ambiguity uses stop first.
            if (f.direction == 1 and c.open <= stop) or (f.direction == -1 and c.open >= stop):
                exit_price, reason = c.open, "stop_gap"
            elif (f.direction == 1 and c.open >= target) or (
                f.direction == -1 and c.open <= target
            ):
                exit_price, reason = target, "target_gap"
            elif (f.direction == 1 and c.low <= stop) or (f.direction == -1 and c.high >= stop):
                exit_price, reason = stop, "stop"
            elif (f.direction == 1 and c.high >= target) or (f.direction == -1 and c.low <= target):
                exit_price, reason = target, "target"
            elif c.timestamp + HOUR >= deadline:
                exit_price, reason = c.close, "time_limit"
            if exit_price is not None:
                break
        if exit_price is None:
            censored += 1
            break
        exit_price *= 1 - f.direction * slip
        price_return = f.direction * (exit_price - entry) / entry
        fee_return = fee * (1 + exit_price / entry)
        trades.append(
            {
                "signal_time": f.decision_time.isoformat(),
                "entry_time": entry_bar.timestamp.isoformat(),
                "exit_bar_open": candles[j].timestamp.isoformat(),
                "exit_reason": reason,
                "direction": "long" if f.direction == 1 else "short",
                "entry": str(entry),
                "exit": str(exit_price),
                "stop": str(stop),
                "target": str(target),
                "return_after_assumed_fees_slippage_pct": float((price_return - fee_return) * 100),
            }
        )
        # Permit a new daily decision only after the previous exit bar has closed.
        i = max(i + 1, j)
    returns = [t["return_after_assumed_fees_slippage_pct"] for t in trades]
    return {
        "status": "UNVALIDATED_PRICE_PATH_REPLAY",
        "decision_hour_utc": decision_hour,
        "closed_trades": len(trades),
        "right_censored_trades": censored,
        "positive_outcome_fraction": sum(x > 0 for x in returns) / len(returns)
        if returns
        else None,
        "mean_trade_return_pct": mean(returns) if returns else None,
        "funding_included": False,
        "liquidation_included": False,
        "is_account_backtest": False,
        "is_out_of_sample": False,
        "trades": trades,
    }


def analyze(
    instrument: str,
    candles: list[Candle],
    cfg: ReferenceConfig,
    *,
    tick: Decimal,
    asof: datetime,
) -> dict[str, Any]:
    if asof.tzinfo is None:
        raise ValueError("asof requires timezone")
    if any(c.timestamp.tzinfo is None for c in candles):
        raise ValueError("candle timestamps require timezone")
    candles = normalize_candles(
        [c for c in candles if c.confirmed and c.timestamp + HOUR <= asof], bar="1h"
    )
    if not candles or asof - (candles[-1].timestamp + HOUR) > HOUR:
        raise ValueError("confirmed candle data is stale")
    if any(c.timestamp.minute or c.timestamp.second or c.timestamp.microsecond for c in candles):
        raise ValueError("candles must align to UTC hours")
    short = instrument.endswith("-SWAP")
    long_term_spot = not short and cfg.spot_policy == "thesis_hold"
    f = calculate_features(candles, cfg, short=short)
    plan: dict[str, Any] = {"status": "WAIT", "reason": "趋势与已收盘突破条件尚未同时满足"}
    if long_term_spot:
        plan = {
            "status": "OBSERVE",
            "reason": "长期现货：技术指标仅作观察；持有取决于投资逻辑，具体逻辑与失效条件待登记",
        }
    elif f.direction:
        stop, target = levels(f, cfg, tick)
        plan = {
            "status": "LONG" if f.direction == 1 else "SHORT",
            "reason": "4H 趋势与 1H 收盘突破同向；研究基线，未验证有效性",
            "reference_entry": str(f.close),
            "stop": str(stop),
            "target": str(target),
            "reward_risk_before_costs": str(abs(target - f.close) / abs(f.close - stop)),
            "entry_policy": "重新获取行情后，下一根 1H 开盘评估；跳过越过止损/目标的跳空",
            "expires_at": (f.decision_time + HOUR).isoformat(),
        }
    return {
        "instrument": instrument,
        "status": "READY",
        "asof": asof.astimezone(UTC).isoformat(),
        "data_through": f.decision_time.isoformat(),
        "history_start": candles[0].timestamp.isoformat(),
        "history_candles": len(candles),
        "close": str(f.close),
        "atr": str(f.atr),
        "atr_pct": float(f.atr_pct),
        "return_24h_pct": float(f.return_24h_pct),
        "prior_high": str(f.prior_high),
        "prior_low": str(f.prior_low),
        "trend_4h": f.trend,
        "relative_volume": float(f.relative_volume) if f.relative_volume is not None else None,
        "holding_policy": "thesis_hold" if long_term_spot else "tactical_research",
        "max_holding_hours": None if long_term_spot else cfg.max_holding_hours,
        "ma_bollinger": calculate_ma_bollinger_snapshot(candles),
        "plan": plan,
        "prediction": {"status": "NOT_VALIDATED", "up_probability": None, "price_interval": None},
        "replay": {
            "status": "NOT_APPLICABLE_LONG_TERM",
            "reason": "短线突破/ATR/时间退出回放不代表长期现货策略",
        }
        if long_term_spot
        else replay(candles, cfg, short=short, tick=tick, decision_hour=f.decision_time.hour),
    }
