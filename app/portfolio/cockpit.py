"""Unified personal research and portfolio cockpit (Ledger + Thesis + Risk + MA-Bollinger)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from app.daily_reference import calculate_ma_bollinger_snapshot
from app.domain.market import Candle
from app.portfolio.personal_ledger import LedgerState, Quote, value_ledger
from app.portfolio.personal_risk import RiskRequest, analyze_risk
from app.portfolio.risk_ledger_adapter import request_from_ledger

DEFAULT_EQUITY_REFERENCE = Decimal("10000")


def build_cockpit_report(
    *,
    now: datetime,
    ledger_state: LedgerState | None = None,
    quotes: list[Quote] | None = None,
    thesis_report: dict[str, Any] | None = None,
    risk_request: RiskRequest | None = None,
    confirm_ledger: bool = False,
    available_cash: Decimal | None = None,
    candles_by_instrument: dict[str, list[Candle]] | None = None,
    watchlist: list[str] | None = None,
    max_quote_age_seconds: int = 300,
    risk_pct: Decimal = Decimal("0.01"),
    quote_source: str = "offline_supplied",
) -> dict[str, Any]:
    if now.tzinfo is None:
        raise ValueError("cockpit valuation time requires timezone")
    now_utc = now.astimezone(UTC)
    quote_list = quotes or []
    quote_map = {q.instrument: q for q in quote_list}

    # 1. Ledger valuation
    ledger_summary: dict[str, Any] | None = None
    effective_equity = DEFAULT_EQUITY_REFERENCE
    equity_source = "reference_10000_usdt"
    if ledger_state is not None:
        ledger_summary = value_ledger(
            ledger_state,
            quote_list,
            now=now_utc,
            max_age_seconds=max_quote_age_seconds,
        )
        total_val = ledger_summary.get("account_value")
        if total_val is not None:
            parsed_eq = Decimal(str(total_val))
            if parsed_eq.is_finite() and parsed_eq > 0:
                effective_equity = parsed_eq
                equity_source = "valued_ledger_equity"

    # 2. Risk report (integrated with ledger if both provided)
    risk_summary: dict[str, Any] | None = None
    if risk_request is not None:
        req = risk_request
        if ledger_state is not None:
            req = request_from_ledger(
                req,
                ledger_state,
                verified=confirm_ledger,
                available_cash=available_cash,
            )
        risk_summary = analyze_risk(
            req,
            now=now_utc,
            max_quote_age_seconds=max_quote_age_seconds,
        )
        if risk_summary.get("net_equity") is not None and equity_source == "reference_10000_usdt":
            parsed_net = Decimal(str(risk_summary["net_equity"]))
            if parsed_net.is_finite() and parsed_net > 0:
                effective_equity = parsed_net
                equity_source = "risk_report_net_equity"

    # 3. Thesis index by instrument
    thesis_by_instrument: dict[str, dict[str, Any]] = {}
    if thesis_report is not None:
        for item in thesis_report.get("products", []):
            inst = str(item.get("instrument", ""))
            if inst:
                thesis_by_instrument[inst] = item

    # 4. Align positions with thesis and live/supplied quotes
    aligned_positions: list[dict[str, Any]] = []
    if ledger_summary is not None:
        for pos in ledger_summary.get("positions", []):
            inst = str(pos["instrument"])
            th = thesis_by_instrument.get(inst)
            mkt_price = pos.get("quote_price")
            avg_cost = pos.get("average_cost")
            aligned_positions.append(
                {
                    "instrument": inst,
                    "quantity": pos.get("quantity"),
                    "average_cost": avg_cost,
                    "cost_basis": pos.get("cost_basis"),
                    "market_price": mkt_price,
                    "market_value": pos.get("market_value"),
                    "unrealized_pnl": pos.get("unrealized_pnl"),
                    "unrealized_return_pct": (
                        float(
                            (Decimal(str(mkt_price)) / Decimal(str(avg_cost)) - 1) * Decimal("100")
                        )
                        if mkt_price is not None
                        and avg_cost is not None
                        and Decimal(str(avg_cost)) > 0
                        else None
                    ),
                    "quote_status": pos.get("quote_status"),
                    "thesis_status": th["status"] if th else "MISSING_INPUT",
                    "thesis_status_label": th["status_label"] if th else "待补充",
                    "buy_reason": th.get("buy_reason") if th else None,
                }
            )

    # 5. MA + Bollinger + ATR signals across watchlist and held instruments
    candle_map = candles_by_instrument or {}
    ordered_instruments: list[str] = []
    for candidate in [
        *(p["instrument"] for p in aligned_positions),
        *(watchlist or []),
        *candle_map.keys(),
    ]:
        if candidate and candidate not in ordered_instruments:
            ordered_instruments.append(candidate)

    quant_signals: list[dict[str, Any]] = []
    for inst in ordered_instruments:
        candles = [c for c in candle_map.get(inst, []) if c.confirmed and c.timestamp <= now_utc]
        if len(candles) < 25:
            quant_signals.append(
                {
                    "instrument": inst,
                    "status": "INSUFFICIENT_CANDLES",
                    "latest_quote": str(quote_map[inst].price) if inst in quote_map else None,
                }
            )
            continue
        snapshot = calculate_ma_bollinger_snapshot(
            candles,
            reference_equity_usdt=effective_equity,
            risk_pct=risk_pct,
        )
        quant_signals.append(
            {
                "instrument": inst,
                "status": "READY",
                "close": str(candles[-1].close),
                "latest_quote": str(quote_map[inst].price)
                if inst in quote_map
                else str(candles[-1].close),
                "data_through": candles[-1].timestamp.isoformat(),
                **snapshot,
            }
        )

    return {
        "generated_at": now_utc.isoformat(),
        "quote_source": quote_source,
        "sizing_equity_usdt": str(effective_equity),
        "sizing_equity_source": equity_source,
        "risk_pct_per_trade": str(risk_pct),
        "ledger": ledger_summary,
        "positions": aligned_positions,
        "theses": list(thesis_by_instrument.values()),
        "risk": risk_summary,
        "quant_signals": quant_signals,
    }


def _fmt_num(val: object, decimals: int = 2) -> str:
    if val is None:
        return "—"
    try:
        d = Decimal(str(val))
        if not d.is_finite():
            return "—"
        if abs(d) >= Decimal("100"):
            return f"{d:,.2f}"
        if abs(d) >= Decimal("1"):
            return f"{d:.4f}".rstrip("0").rstrip(".")
        return f"{d:.6f}".rstrip("0").rstrip(".")
    except Exception:
        return str(val)


def _clean_cell(val: object) -> str:
    if val is None or val == "":
        return "—"
    return " ".join(str(val).replace("|", "/").split())


def render_cockpit_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# 个人投研与量化风控驾驶舱（Personal Cockpit）",
        "",
        f"- **生成时间（UTC）**：`{report['generated_at']}`",
        f"- **行情来源**：`{report['quote_source']}`",
        f"- **以损定仓基准净值**：`{_fmt_num(report['sizing_equity_usdt'])} USDT`（来源：`{report['sizing_equity_source']}`，单笔风险预算 `{float(Decimal(report['risk_pct_per_trade']) * 100):.2f}%`）",
        "",
    ]

    # Section 1: Portfolio & Ledger Overview
    lines += ["## 一、账户估值与组合风险总览", ""]
    ledger = report.get("ledger")
    risk = report.get("risk")
    if ledger is None and risk is None:
        lines.append("> 未提供本地台账或组合风险输入，当前仅展示量化信号面板。")
    else:
        lines += [
            "| 指标 | 数值 | 状态 / 说明 |",
            "|---|---:|---|",
        ]
        if ledger is not None:
            lines.append(
                f"| 台账现金余额 | `{_fmt_num(ledger.get('cash'))} USDT` | 已实现盈亏 `{_fmt_num(ledger.get('realized_pnl'))} USDT`，累计手续费 `{_fmt_num(ledger.get('fees_paid'))} USDT` |"
            )
            lines.append(
                f"| 台账总权益 | `{_fmt_num(ledger.get('account_value'))} USDT` | 估值完整：`{ledger.get('complete')}` |"
            )
        if risk is not None:
            lines.append(
                f"| 组合净权益 | `{_fmt_num(risk.get('net_equity'))} {risk.get('currency', 'USDT')}` | 风险整体状态：**{_clean_cell(risk.get('status_label', risk.get('overall_status')))}** |"
            )
            lines.append(
                f"| 总敞口 / 净敞口 | `{_fmt_num(risk.get('gross_exposure'))}` / `{_fmt_num(risk.get('net_exposure'))}` | 可用现金比例：`{_fmt_num(risk.get('cash_ratio'))}` |"
            )
            for chk in risk.get("checks", []):
                lines.append(
                    f"| 风控项：{_clean_cell(chk.get('label', chk.get('name')))} | 实际 `{_fmt_num(chk.get('actual'))}` / 限额 `{_fmt_num(chk.get('limit'))}` | 状态：`{chk.get('status')}` |"
                )

    # Section 2: Spot Positions & Investment Theses
    lines += ["", "## 二、现货持仓台账与投资逻辑对齐", ""]
    positions = report.get("positions", [])
    if not positions:
        lines.append("> 当前台账无现货持仓记录。")
    else:
        lines += [
            "| 品种 | 持仓数量 | 均价成本 | 最新报价 | 持仓市值 (USDT) | 浮动盈亏 (USDT) | 收益率% | 长期逻辑状态 | 买入理由摘要 |",
            "|---|---:|---:|---:|---:|---:|---:|---|---|",
        ]
        for p in positions:
            ret_str = (
                f"{p['unrealized_return_pct']:+.2f}%"
                if p.get("unrealized_return_pct") is not None
                else "—"
            )
            lines.append(
                f"| `{p['instrument']}` | {_fmt_num(p.get('quantity'))} | "
                f"{_fmt_num(p.get('average_cost'))} | {_fmt_num(p.get('market_price'))} | "
                f"{_fmt_num(p.get('market_value'))} | {_fmt_num(p.get('unrealized_pnl'))} | "
                f"{ret_str} | **{_clean_cell(p.get('thesis_status_label'))}** (`{p.get('thesis_status')}`) | "
                f"{_clean_cell(p.get('buy_reason') or '待登记')} |"
            )

    # Section 3: MA + Bollinger + ATR Quant Signals
    lines += [
        "",
        "## 三、均线 + 布林带 + ATR 量化模型与以损定仓面板",
        "",
        "| 品种 | 最新收盘价 | EMA200 趋势线 | 布林下轨 / 中轨 / 上轨 (BB48) | %B 分位 | 带宽% | 距下轨% | 3×ATR24 保护止损 (止损距离%) | 建议以损定仓额度 | 量化状态 |",
        "|---|---:|---:|---|---:|---:|---:|---:|---:|---|",
    ]
    for sig in report.get("quant_signals", []):
        if sig.get("status") != "READY":
            lines.append(
                f"| `{sig['instrument']}` | {_fmt_num(sig.get('latest_quote'))} | — | — | — | — | — | — | — | K线不足 (`{sig.get('status')}`) |"
            )
            continue
        bands = (
            f"{_fmt_num(sig['bb_lower'])} / "
            f"{_fmt_num(sig['bb_middle'])} / "
            f"{_fmt_num(sig['bb_upper'])}"
        )
        stop_col = f"{_fmt_num(sig['protective_stop_price'])} ({sig['stop_distance_pct']:.2f}%)"
        lines.append(
            f"| `{sig['instrument']}` | {_fmt_num(sig['close'])} | "
            f"{_fmt_num(sig['trend_ma'])} | {bands} | "
            f"{sig['percent_b']:.2f} | {sig['bandwidth_pct']:.2f}% | "
            f"{sig['distance_to_lower_pct']:+.2f}% | {stop_col} | "
            f"**{sig['risk_sizing_notional_per_10k_usdt']:,.0f} USDT** | **{sig['signal_zone_label']}** |"
        )

    lines += [
        "",
        "> 说明：本简报仅供个人投研与风险复核，不自动触发任何真实或模拟订单。长期现货持仓以投资逻辑复核为准；量化面板基于 `ma_bollinger_pullback`（EMA200 + BB48 + 3×ATR24）与 `risk_per_trade` 模型计算。",
        "",
    ]
    return "\n".join(lines)
