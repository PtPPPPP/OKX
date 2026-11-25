"""Unified personal research cockpit CLI: Ledger + Thesis + Risk + MA-Bollinger signals."""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from app.domain.market import Candle
from app.market.historical_data import load_candles_csv
from app.portfolio.cockpit import build_cockpit_report, render_cockpit_markdown
from app.portfolio.ledger_storage import LedgerStore
from app.portfolio.personal_ledger import Quote, replay
from app.portfolio.personal_risk import RiskRequest
from app.portfolio.thesis_storage import ThesisStore
from scripts._console import configure_utf8_output
from scripts.personal_ledger import read_json

DEFAULT_WATCHLIST = ("BTC-USDT", "ARB-USDT", "XINTC-USDT", "XQQQ-USDT", "XAUT-USDT")


def _load_quotes_file(path: Path) -> list[Quote]:
    raw = read_json(path)
    if not isinstance(raw, list):
        raise ValueError("quotes JSON must be an array")
    return [Quote.model_validate(item) for item in raw]


def _fetch_public_market_data(
    instruments: list[str],
    *,
    history_days: int,
) -> tuple[datetime, list[Quote], dict[str, list[Candle]]]:
    from app.market.daily_public import DailyPublicClient

    client = DailyPublicClient()
    try:
        now = client.server_time()
        cutoff = now.replace(minute=0, second=0, microsecond=0)
        catalog = client.instruments()
        quotes: list[Quote] = []
        candles_by_inst: dict[str, list[Candle]] = {}
        for inst in instruments:
            meta = catalog.get(inst)
            if not meta or meta.get("state") != "live":
                continue
            ctx = client.context(inst, cutoff=now)
            ticker = ctx.get("ticker")
            if ticker and ticker.get("last") and ticker.get("ts"):
                ts = datetime.fromtimestamp(int(str(ticker["ts"])) / 1000, UTC)
                quotes.append(
                    Quote(
                        instrument=inst,
                        price=Decimal(str(ticker["last"])),
                        at=ts,
                        source="okx_public_ticker",
                    )
                )
            try:
                candles = client.candles(inst, cutoff=cutoff, days=history_days)
                if candles:
                    candles_by_inst[inst] = candles
                    if not any(q.instrument == inst for q in quotes):
                        quotes.append(
                            Quote(
                                instrument=inst,
                                price=candles[-1].close,
                                at=candles[-1].timestamp,
                                source="okx_public_1h_close",
                            )
                        )
            except Exception:
                continue
        return now, quotes, candles_by_inst
    finally:
        client.close()


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(
        description="个人投研与量化风控驾驶舱（一键整合台账、投资逻辑、组合风控与均线布林带信号）"
    )
    cli.add_argument("--ledger", type=Path, help="可选：本地个人持仓台账 SQLite 路径")
    cli.add_argument("--thesis-db", type=Path, help="可选：本地现货投资逻辑 SQLite 路径")
    cli.add_argument("--risk-input", type=Path, help="可选：个人组合风险配置 JSON 路径")
    cli.add_argument(
        "--confirm-ledger", action="store_true", help="标记用户已核对本地台账数量与现金"
    )
    cli.add_argument("--available-cash", help="配合台账传入可用现金（十进制文本）")
    cli.add_argument("--quotes", type=Path, help="离线行情报价 JSON 数组路径")
    cli.add_argument(
        "--candles-dir", type=Path, help="离线 1H K 线 CSV 目录（含 <INSTRUMENT>.csv）"
    )
    cli.add_argument(
        "--fetch-public",
        action="store_true",
        help="从 OKX 公共只读接口自动拉取最新报价与 1H K 线（无需 API Key）",
    )
    cli.add_argument(
        "--watchlist",
        default=",".join(DEFAULT_WATCHLIST),
        help="逗号分隔的观察品种列表",
    )
    cli.add_argument(
        "--history-days", type=int, default=15, help="在线拉取 1H K 线的天数（默认 15 天）"
    )
    cli.add_argument(
        "--risk-pct", default="0.01", help="以损定仓单笔风险预算比例（默认 0.01 即 1%%）"
    )
    cli.add_argument("--at", help="显式估值时点（ISO8601 带时区）；省略时使用当前 UTC 时间")
    cli.add_argument("--max-quote-age-seconds", type=int, default=300)
    cli.add_argument("--format", choices=("markdown", "json"), default="markdown")
    cli.add_argument("--output", type=Path, help="可选：保存报告到指定文件路径")
    return cli


def main(argv: list[str] | None = None) -> int:
    configure_utf8_output()
    cli = parser()
    args = cli.parse_args(argv)
    try:
        if args.fetch_public and (args.quotes is not None or args.candles_dir is not None):
            raise ValueError(
                "--fetch-public cannot be combined with offline --quotes or --candles-dir"
            )

        watchlist = [item.strip() for item in str(args.watchlist).split(",") if item.strip()]
        ledger_state = None
        if args.ledger is not None:
            if not args.ledger.is_file():
                raise ValueError(f"ledger database does not exist: {args.ledger}")
            opening, entries = LedgerStore(args.ledger).load()
            ledger_state = replay(opening, entries)
            for inst in ledger_state.positions:
                if inst not in watchlist:
                    watchlist.append(inst)

        risk_request = None
        if args.risk_input is not None:
            risk_request = RiskRequest.model_validate(read_json(args.risk_input))

        now = datetime.fromisoformat(args.at) if args.at else datetime.now(UTC)
        quotes: list[Quote] = []
        candles_by_inst: dict[str, list[Candle]] = {}
        quote_source = "offline_supplied"

        if args.fetch_public:
            fetched_now, quotes, candles_by_inst = _fetch_public_market_data(
                watchlist,
                history_days=args.history_days,
            )
            if not args.at:
                now = fetched_now
            quote_source = "okx_public_api"
        else:
            if args.quotes is not None:
                quotes = _load_quotes_file(args.quotes)
            elif risk_request is not None and risk_request.quotes:
                quotes = [
                    Quote(
                        instrument=q.instrument,
                        price=q.price,
                        at=q.at,
                        source=q.source,
                    )
                    for q in risk_request.quotes
                ]
            if args.candles_dir is not None:
                if not args.candles_dir.is_dir():
                    raise ValueError(f"candles directory does not exist: {args.candles_dir}")
                for csv_path in sorted(args.candles_dir.glob("*.csv")):
                    inst = csv_path.stem.upper()
                    candles_by_inst[inst] = load_candles_csv(csv_path, bar="1h")
                    if not any(q.instrument == inst for q in quotes) and candles_by_inst[inst]:
                        last_bar = candles_by_inst[inst][-1]
                        quotes.append(
                            Quote(
                                instrument=inst,
                                price=last_bar.close,
                                at=last_bar.timestamp,
                                source="offline_candle_close",
                            )
                        )

        thesis_report: dict[str, Any] | None = None
        if args.thesis_db is not None:
            if not args.thesis_db.is_file():
                raise ValueError(f"thesis database does not exist: {args.thesis_db}")
            thesis_report = ThesisStore(args.thesis_db).report(now=now)

        report = build_cockpit_report(
            now=now,
            ledger_state=ledger_state,
            quotes=quotes,
            thesis_report=thesis_report,
            risk_request=risk_request,
            confirm_ledger=args.confirm_ledger,
            available_cash=Decimal(args.available_cash)
            if args.available_cash is not None
            else None,
            candles_by_instrument=candles_by_inst,
            watchlist=watchlist,
            max_quote_age_seconds=args.max_quote_age_seconds,
            risk_pct=Decimal(str(args.risk_pct)),
            quote_source=quote_source,
        )

        text = (
            render_cockpit_markdown(report)
            if args.format == "markdown"
            else json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        )
        if args.output is not None:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(text, encoding="utf-8")
        print(text, end="")
        return 0
    except (ValueError, OSError, sqlite3.Error, InvalidOperation) as exc:
        cli.exit(2, f"个人投研驾驶舱运行失败：{exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
