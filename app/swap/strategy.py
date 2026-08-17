from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from uuid import uuid4

from app.swap.data import MultiTimeframeMarketBundle
from app.swap.domain import TradeAction
from app.swap.indicators import anchored_vwap, atr, macd, oi_metrics, relative_volume


@dataclass(frozen=True, slots=True)
class ScoreComponent:
    component_name: str
    raw_value: str
    normalized_value: Decimal
    weight: Decimal
    contribution: Decimal
    available: bool
    rejection_reason: str | None = None


@dataclass(frozen=True, slots=True)
class SwapSignal:
    signal_id: str
    action: TradeAction
    long_score: Decimal
    short_score: Decimal
    components: tuple[ScoreComponent, ...]
    rejection_reasons: tuple[str, ...]
    stop_price: Decimal | None
    take_profit_price: Decimal | None


class AnchoredVWAPMultifactorSwapStrategy:
    name = "anchored_vwap_multifactor_swap"

    def __init__(
        self, minimum_score: Decimal = Decimal("4"), advantage: Decimal = Decimal("1")
    ) -> None:
        self.minimum_score = minimum_score
        self.advantage = advantage

    def evaluate(self, bundle: MultiTimeframeMarketBundle) -> SwapSignal:
        if not bundle.tradable:
            return SwapSignal(
                uuid4().hex,
                TradeAction.DATA_INCOMPLETE,
                Decimal("0"),
                Decimal("0"),
                (),
                bundle.rejection_reasons,
                None,
                None,
            )
        m15, m5, m1h = (
            macd(list(bundle.candles_15m)),
            macd(list(bundle.candles_5m)),
            macd(list(bundle.candles_1h)),
        )
        session_vwap, current_atr, volume = (
            anchored_vwap(list(bundle.candles_5m)),
            atr(list(bundle.candles_15m)),
            relative_volume(list(bundle.candles_5m)),
        )
        oi = oi_metrics(
            list(bundle.oi_history), bundle.execution_candle.close - bundle.candles_5m[-2].close
        )
        if any(item is None for item in (m15, m5, m1h, session_vwap, current_atr, volume, oi)):
            return SwapSignal(
                uuid4().hex,
                TradeAction.DATA_INCOMPLETE,
                Decimal("0"),
                Decimal("0"),
                (),
                ("indicator_warmup_or_data_missing",),
                None,
                None,
            )
        assert (
            m15 is not None
            and m5 is not None
            and m1h is not None
            and session_vwap is not None
            and current_atr is not None
            and volume is not None
            and oi is not None
        )
        close = bundle.execution_candle.close
        long_conditions = (
            m1h.macd_line > m1h.signal_line,
            m15.bullish_cross or m15.histogram > 0,
            m5.histogram_delta is not None and m5.histogram_delta > 0,
            close > session_vwap,
            volume >= Decimal("1"),
            oi.change > 0,
        )
        short_conditions = (
            m1h.macd_line < m1h.signal_line,
            m15.bearish_cross or m15.histogram < 0,
            m5.histogram_delta is not None and m5.histogram_delta < 0,
            close < session_vwap,
            volume >= Decimal("1"),
            oi.change > 0,
        )
        components = tuple(
            ScoreComponent(
                name,
                str(value),
                Decimal("1") if value else Decimal("0"),
                Decimal("1"),
                Decimal("1") if value else Decimal("0"),
                True,
            )
            for name, value in zip(
                ("1h_macd", "15m_macd", "5m_trigger", "anchored_vwap", "volume", "oi"),
                long_conditions,
                strict=True,
            )
        )
        long_score, short_score = (
            sum((item.contribution for item in components), Decimal("0")),
            sum(
                (Decimal("1") if value else Decimal("0") for value in short_conditions),
                Decimal("0"),
            ),
        )
        action = TradeAction.NO_TRADE
        if long_score >= self.minimum_score and long_score - short_score >= self.advantage:
            action = TradeAction.OPEN_LONG
        elif short_score >= self.minimum_score and short_score - long_score >= self.advantage:
            action = TradeAction.OPEN_SHORT
        stop = (
            close - current_atr * Decimal("1.5")
            if action is TradeAction.OPEN_LONG
            else close + current_atr * Decimal("1.5")
            if action is TradeAction.OPEN_SHORT
            else None
        )
        target = (
            close + current_atr * Decimal("3")
            if action is TradeAction.OPEN_LONG
            else close - current_atr * Decimal("3")
            if action is TradeAction.OPEN_SHORT
            else None
        )
        return SwapSignal(
            uuid4().hex, action, long_score, short_score, components, (), stop, target
        )
