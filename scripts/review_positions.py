"""Read-only, multi-horizon review of explicitly supplied spot positions."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import numpy as np

from app.daily_reference import ReferenceConfig, calculate_features
from app.domain.market import Candle
from app.market.daily_public import DailyPublicClient
from app.market.historical_data import save_candles_csv
from scripts.daily_reference import display_number, write_json


def _thesis_summary(symbol: str, thesis: dict[str, Any] | None) -> dict[str, Any]:
    """Accept only computed thesis-report entries, never fields from positions/tickers."""
    if thesis is None:
        return {
            "instrument": symbol,
            "status": "MISSING_INPUT",
            "status_label": "待补充",
            "reasons": ["thesis_not_recorded"],
            "reason_details": ["尚未登记投资逻辑，请填写用户自己的买入理由、条件和复核周期。"],
            "buy_reason": None,
            "revision_id": None,
            "review_id": None,
            "holding_policy": "thesis_hold",
            "max_holding_hours": None,
            "action": None,
        }
    if (
        thesis.get("instrument") != symbol
        or thesis.get("status")
        not in {"MISSING_INPUT", "PENDING_REVIEW", "SUPPORTED", "CONFLICT", "INVALIDATED"}
        or not isinstance(thesis.get("status_label"), str)
        or not isinstance(thesis.get("reasons"), list)
        or any(not isinstance(reason, str) for reason in thesis["reasons"])
        or not isinstance(thesis.get("buy_reason"), (str, type(None)))
        or thesis.get("holding_policy") != "thesis_hold"
        or thesis.get("max_holding_hours") is not None
        or thesis.get("action") is not None
    ):
        raise ValueError("invalid computed thesis report for position")
    return dict(thesis)


def load_thesis_summaries(database: Path, *, now: datetime) -> dict[str, dict[str, Any]]:
    """Explicit read-only opt-in; a missing database is never created."""
    if not database.is_file():
        raise ValueError("thesis database does not exist")
    from app.portfolio.thesis_storage import ThesisStore

    report = ThesisStore(database).report(now=now)
    summaries: dict[str, dict[str, Any]] = {}
    for product in report["products"]:
        symbol = str(product["instrument"])
        if symbol in summaries:
            raise ValueError("duplicate product in computed thesis report")
        summaries[symbol] = _thesis_summary(symbol, product)
    return summaries


def _markdown(value: object) -> str:
    """Render manually supplied prose as one plain-text Markdown line."""
    text = html.escape(" ".join(str(value).split()), quote=False)
    return re.sub(r"([\\`*_{}\[\]()#+.!|>-])", r"\\\1", text)


def summarize(
    daily: list[Candle],
    hourly: list[Candle],
    position: dict[str, Any],
    context: dict[str, Any],
    *,
    asof: datetime | None = None,
    max_quote_age_seconds: int = 300,
    thesis: dict[str, Any] | None = None,
) -> dict[str, Any]:
    symbol = str(position["instrument"])
    cfg = ReferenceConfig(instruments=[symbol])
    if symbol.endswith("-SWAP"):
        raise ValueError("position review accepts spot only")
    thesis_report = _thesis_summary(symbol, thesis)
    cost = Decimal(str(position["cost"]))
    value = (
        Decimal(str(position["market_value_usdt"]))
        if position.get("market_value_usdt") is not None
        else None
    )
    quantity = Decimal(str(position["quantity"])) if position.get("quantity") is not None else None
    if not cost.is_finite() or cost <= 0:
        raise ValueError("average cost must be positive")
    if any(x is not None and (not x.is_finite() or x < 0) for x in (value, quantity)):
        raise ValueError("reported market value and actual quantity must be finite and nonnegative")
    asof = asof or datetime.now(UTC)
    if asof.tzinfo is None or max_quote_age_seconds <= 0:
        raise ValueError("aware valuation time and positive quote age limit required")
    asof = asof.astimezone(UTC)
    f = calculate_features(hourly, cfg, short=False)
    ticker = context.get("ticker")
    if not ticker:
        raise ValueError("fresh ticker required for position valuation")
    current = Decimal(str(ticker["last"]))
    if not current.is_finite() or current <= 0 or len(daily) < 30:
        raise ValueError("invalid price or insufficient daily history")
    if ticker.get("instId", symbol) != symbol:
        raise ValueError("ticker instrument does not match position")
    ticker_time = None
    age_seconds = None
    quote_status = "MISSING_TIMESTAMP"
    if ticker.get("ts") is not None:
        try:
            ticker_time = datetime.fromtimestamp(int(str(ticker["ts"])) / 1000, UTC)
        except (ValueError, OverflowError, OSError):
            quote_status = "INVALID_TIMESTAMP"
        else:
            age_seconds = (asof - ticker_time).total_seconds()
            quote_status = (
                "FUTURE"
                if age_seconds < 0
                else "STALE"
                if age_seconds > max_quote_age_seconds
                else "FRESH"
            )
    valuation_status = (
        "UNKNOWN_QUANTITY"
        if quantity is None
        else "READY"
        if quote_status == "FRESH"
        else "UNAVAILABLE_QUOTE"
    )
    basis = quantity * cost if quantity is not None else None
    market_value = (
        quantity * current if valuation_status == "READY" and quantity is not None else None
    )
    closes = [float(c.close) for c in daily]
    averages = {str(n): sum(closes[-n:]) / n if len(closes) >= n else None for n in (20, 50, 200)}
    # Non-overlapping 3-day endpoint returns; descriptive history, not a forecast.
    outcomes = [(closes[i] / closes[i - 3] - 1) * 100 for i in range(3, len(closes), 3)]
    q = np.quantile(outcomes, [0.05, 0.5, 0.95]).tolist()
    high, low = max(c.high for c in daily[-20:]), min(c.low for c in daily[-20:])
    ranges = [
        max(c.high - c.low, abs(c.high - p.close), abs(c.low - p.close))
        for p, c in zip(daily[-15:-1], daily[-14:], strict=True)
    ]
    atr_daily = sum(ranges, Decimal(0)) / 14
    return {
        "instrument": symbol,
        "holding_policy": "thesis_hold",
        "max_holding_hours": None,
        "thesis_validation": thesis_report["status"],
        "thesis": thesis_report,
        "action": None,
        "cost": str(cost),
        "reported_market_value_usdt": str(value) if value is not None else None,
        "historical_snapshot": {
            "cost": str(cost),
            "market_value_usdt": str(value),
            "updated_at": position.get("updated_at"),
            "source": position.get("source", "user_supplied_legacy_file"),
        }
        if value is not None
        else None,
        "quantity": str(quantity) if quantity is not None else None,
        "quantity_status": "UNKNOWN" if quantity is None else "KNOWN",
        "updated_at": position.get("updated_at"),
        "source": position.get("source", "user_supplied_file"),
        "latest_price": str(current),
        "ticker_time": ticker_time.isoformat() if ticker_time is not None else None,
        "valuation_at": asof.isoformat(),
        "quote_age_seconds": age_seconds,
        "max_quote_age_seconds": max_quote_age_seconds,
        "quote_status": quote_status,
        "valuation_status": valuation_status,
        "return_from_cost_pct": float((current / cost - 1) * 100),
        "rise_to_break_even_pct": float((cost / current - 1) * 100),
        "cost_basis_usdt": str(basis) if basis is not None else None,
        "market_value_usdt": str(market_value) if market_value is not None else None,
        "unrealized_pnl_usdt": str(market_value - basis)
        if market_value is not None and basis is not None
        else None,
        "valuation_note": "仅按用户明确提供的实际数量与有效报价计算；旧市值仅作历史快照。成本是否含手续费由输入决定；不提供账户总估值。",
        "daily_bars": len(daily),
        "daily_start": daily[0].timestamp.isoformat(),
        "daily_end": (daily[-1].timestamp + timedelta(days=1)).isoformat(),
        "daily_sma": averages,
        "daily_sma20_slope_5d_pct": ((sum(closes[-20:]) / 20) / (sum(closes[-25:-5]) / 20) - 1)
        * 100,
        "daily_atr14": str(atr_daily),
        "daily_atr14_pct": float(atr_daily / current * 100),
        "daily_return_pct": {
            str(n): (closes[-1] / closes[-n - 1] - 1) * 100 if len(closes) > n else None
            for n in (7, 30, 90, 365)
        },
        "daily_20_high": str(high),
        "daily_20_low": str(low),
        "below_history_high_pct": float((current / max(c.high for c in daily) - 1) * 100),
        "historical_3d": {
            "sample_count": len(outcomes),
            "q05_pct": q[0],
            "median_pct": q[1],
            "q95_pct": q[2],
            "interpretation": "non-overlapping unconditional historical returns; not predictive probabilities",
        },
        "hourly_cutoff": f.decision_time.isoformat(),
        "trend_4h": f.trend,
        "last_hour_close": str(f.close),
        "atr_1h": str(f.atr),
        "atr_1h_pct": float(f.atr_pct),
        "prior_24h_high": str(f.prior_high),
        "prior_24h_low": str(f.prior_low),
        "relative_volume": float(f.relative_volume) if f.relative_volume is not None else None,
        "hourly_signal": "LONG" if f.direction == 1 else "WAIT",
        "hourly_signal_scope": "technical_observation_only; not a long-term holding decision",
        "context": context,
    }


def render(report: dict[str, Any]) -> str:
    lines = [
        "# 现货持仓长周期分析",
        "",
        f"采集开始：{report['started_at']}；要求日线窗口：{report['requested_daily_days']} 天。",
        "各产品使用自身 OKX 历史；不足窗口的按实际数据分析。日线 UTC 零点切分，仅使用已收盘数据。",
        "现货没有固定持有期限，以登记的投资逻辑与复核材料为依据。小时形态仅作观察，不触发长期持仓买卖。",
        "达到 72 小时或单次小时级技术破位不会改变长期逻辑状态；材料支持也不等于买入或继续持有建议。",
        "",
        "本报告不含现金与完整账户流水，不提供账户总估值。过期或无时间戳报价仅用于注明时点的技术观察。",
        "",
        "| 产品 | 参考价 | 行情状态 | 成本 | 相对成本% | 回本需上涨% | 日线数量 | 4H 趋势 |",
        "|---|---:|---|---:|---:|---:|---:|---|",
    ]
    for item in report["positions"]:
        lines.append(
            f"| {item['instrument']} | {item['latest_price']} | {item['quote_status']} | {item['cost']} | {item['return_from_cost_pct']:.2f} | {item['rise_to_break_even_pct']:.2f} | {item['daily_bars']} | {item['trend_4h']} |"
        )
    for item in report["positions"]:
        ma = item["daily_sma"]
        hist = item["historical_3d"]
        thesis = item["thesis"]
        lines += [
            "",
            f"## {_markdown(item['instrument'])}",
            "",
            f"- 长期逻辑状态：{_markdown(thesis['status_label'])}（{_markdown(thesis['status'])}）。",
            f"- 逻辑评估时点：{_markdown(thesis.get('evaluated_at') or '未评估')}。",
            f"- 已登记买入理由：{_markdown(thesis['buy_reason']) if thesis['buy_reason'] else '待补充'}。",
            "- 状态依据："
            + (
                "；".join(
                    _markdown(reason) for reason in thesis.get("reason_details", thesis["reasons"])
                )
                or "无新增状态依据"
            )
            + "。",
            "- 买卖动作：无；未登记理由或材料不足时保持待补充/待复核，不由技术指标代填结论。",
            f"- 报价时点：{item['ticker_time'] or '未知'}；行情状态：{item['quote_status']}（最大有效期 {item['max_quote_age_seconds']} 秒）。日线范围：{item['daily_start']} 至 {item['daily_end']}。",
            "- 日线 SMA20/50/200："
            + " / ".join(
                display_number(ma[str(n)]) if ma[str(n)] is not None else "历史不足"
                for n in (20, 50, 200)
            )
            + "。",
            f"- 日线 SMA20 最近五日变化：{item['daily_sma20_slope_5d_pct']:.2f}%；日线 ATR14：{display_number(item['daily_atr14'])}。",
            f"- 最近 20 个完整日高/低：{item['daily_20_high']} / {item['daily_20_low']}。",
            f"- 前 24 个完整小时高/低：{item['prior_24h_high']} / {item['prior_24h_low']}；小时 ATR：{display_number(item['atr_1h'])}。",
            f"- 小时形态（仅观察）：{item['hourly_signal']}；4H 趋势：{item['trend_4h']}；相对量：{item['relative_volume']}。",
            f"- 历史不重叠三日收益样本 {hist['sample_count']} 个；5%/中位/95% 分位：{hist['q05_pct']:.2f}% / {hist['median_pct']:.2f}% / {hist['q95_pct']:.2f}%。",
            "- 上述历史分位数是过去收益描述，不能作为未来三天的预测概率或置信区间。",
            f"- 实际数量：{item['quantity'] if item['quantity'] is not None else '未知'}；估值状态：{item['valuation_status']}。",
            f"- 当前持仓市值：{item['market_value_usdt'] if item['market_value_usdt'] is not None else '不可用'} USDT；未实现盈亏：{item['unrealized_pnl_usdt'] if item['unrealized_pnl_usdt'] is not None else '不可用'} USDT。",
            f"- 用户历史市值快照：{item['reported_market_value_usdt'] if item['reported_market_value_usdt'] is not None else '未提供'} USDT；记录时点：{item['updated_at'] or '未知'}。",
            f"- {item['valuation_note']}",
        ]
    lines += [
        "",
        "未自动下单。此文件为事实参数；具体持仓建议另附分析，并考虑事件、资金用途和可承受亏损。",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Public-only long horizon spot position review")
    parser.add_argument("--positions", type=Path, required=True)
    parser.add_argument("--days", type=int, default=730)
    parser.add_argument("--max-quote-age-seconds", type=int, default=300)
    parser.add_argument(
        "--thesis-database",
        type=Path,
        help="可选的本地长期逻辑台账；只读加载，省略时不读取任何逻辑台账",
    )
    parser.add_argument("--output-root", type=Path, default=Path("artifacts/position-review"))
    args = parser.parse_args()
    if not 30 <= args.days <= 3650:
        parser.error("days must be between 30 and 3650")
    if args.max_quote_age_seconds <= 0:
        parser.error("max-quote-age-seconds must be positive")
    positions = json.loads(args.positions.read_text(encoding="utf-8-sig"), parse_float=Decimal)
    cfg = ReferenceConfig(instruments=[p["instrument"] for p in positions])
    if any(i.endswith("-SWAP") for i in cfg.instruments):
        parser.error("spot positions only")
    thesis_by_instrument: dict[str, dict[str, Any]] = {}
    if args.thesis_database is not None:
        try:
            thesis_by_instrument = load_thesis_summaries(
                args.thesis_database, now=datetime.now(UTC)
            )
        except (ValueError, OSError, sqlite3.Error) as exc:
            parser.error(f"cannot read thesis database: {exc}")
    output = args.output_root / (
        datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid4().hex[:8]
    )
    output.mkdir(parents=True, exist_ok=False)
    client = DailyPublicClient()
    try:
        now = client.server_time()
        if abs((datetime.now(UTC) - now).total_seconds()) > 120:
            raise ValueError("server clock difference exceeds 120 seconds")
        hourly_cutoff = now.replace(minute=0, second=0, microsecond=0)
        daily_cutoff = hourly_cutoff.replace(hour=0)
        metadata = client.instruments()
        items = []
        for position in positions:
            symbol = str(position["instrument"])
            if metadata.get(symbol, {}).get("state") != "live":
                raise ValueError(f"instrument unavailable: {symbol}")
            print(
                f"Reviewing {symbol}: up to {args.days} daily candles + 15 days hourly", flush=True
            )
            daily = client.candles(symbol, cutoff=daily_cutoff, days=args.days, bar="1d")
            hourly = client.candles(symbol, cutoff=hourly_cutoff, days=15)
            context = client.context(symbol, cutoff=hourly_cutoff)
            save_candles_csv(daily, output / f"{symbol}-1d.csv")
            save_candles_csv(hourly, output / f"{symbol}-1h.csv")
            items.append(
                summarize(
                    daily,
                    hourly,
                    position,
                    context,
                    max_quote_age_seconds=args.max_quote_age_seconds,
                    thesis=thesis_by_instrument.get(symbol),
                )
            )
        # Re-evaluate after potentially slow public-history collection. Indicators
        # are deliberately not inputs to the thesis evaluator.
        if args.thesis_database is not None:
            final_theses = load_thesis_summaries(args.thesis_database, now=datetime.now(UTC))
            for item in items:
                item["thesis"] = _thesis_summary(
                    item["instrument"], final_theses.get(item["instrument"])
                )
                item["thesis_validation"] = item["thesis"]["status"]
        report = {
            "started_at": now.isoformat(),
            "requested_daily_days": args.days,
            "positions": items,
            "data_hashes": {
                p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in output.glob("*.csv")
            },
        }
        write_json(output / "analysis.json", report)
        (output / "analysis.md").write_text(render(report), encoding="utf-8")
        print(str(output.resolve()))
    finally:
        client.close()


if __name__ == "__main__":
    main()
