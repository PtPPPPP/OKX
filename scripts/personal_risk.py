"""Offline personal risk report using explicitly supplied local data only."""

from __future__ import annotations

import argparse
import html
import json
import re
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from app.portfolio.ledger_storage import LedgerStore
from app.portfolio.personal_ledger import replay
from app.portfolio.personal_risk import RiskRequest, analyze_risk
from app.portfolio.risk_ledger_adapter import request_from_ledger
from scripts.personal_ledger import read_json

LOCAL_ROOT = Path(__file__).resolve().parents[1] / "data" / "personal-risk"


def local_path(path: Path) -> Path:
    resolved = path.resolve()
    root = LOCAL_ROOT.resolve()
    if resolved == root or not resolved.is_relative_to(root):
        raise ValueError(f"personal risk outputs must be inside {root}")
    return resolved


def _escape(value: object) -> str:
    if value is None:
        return "未知/未配置"
    escaped = html.escape(" ".join(str(value).split()), quote=False)
    return re.sub(r"([\\`*_{}\[\]()#+.!|>-])", r"\\\1", escaped)


def render(report: dict[str, Any]) -> str:
    lines = [
        "# 个人组合风险报告",
        "",
        "仅分析本地登记数据。核对标记由用户提供，不代表交易所认证；不生成交易指令。",
        "金额按报告币种计，比例为小数。未知数据和未配置预算不按零处理。",
        "",
        f"整体状态：{_escape(report.get('status_label', report.get('overall_status')))}",
        f"组合净权益：{_escape(report.get('net_equity'))} {_escape(report.get('currency'))}",
        f"估值完整：{_escape(report.get('valuation_complete'))}",
        f"现金余额：{_escape(report.get('cash_balance'))}；可用现金：{_escape(report.get('available_cash'))}",
        f"可用现金比例：{_escape(report.get('cash_ratio'))}；现金余额比例：{_escape(report.get('cash_balance_ratio'))}",
        f"总敞口：{_escape(report.get('gross_exposure'))}；净敞口：{_escape(report.get('net_exposure'))}",
        "",
        "## 数据缺项与判断依据",
        "",
    ]
    reasons = report.get("reasons", [])
    lines.extend(f"- {_escape(reason)}" for reason in reasons)
    if not reasons:
        lines.append("报告未列出额外缺项。")
    for name, key in (
        ("现货持仓", "spot_positions"),
        ("合约持仓", "contracts"),
        ("产品敞口", "by_product"),
        ("底层资产敞口", "by_underlying"),
        ("资产类别敞口", "by_asset_class"),
    ):
        lines.extend(["", f"## {name}", ""])
        rows = report.get(key, [])
        if not rows:
            lines.append("未登记项目；是否完整以报告完整性状态为准。")
        for row in rows:
            lines.append("- " + "；".join(f"{_escape(k)}：{_escape(v)}" for k, v in row.items()))
    lines.extend(["", "## 风险设置与检查", ""])
    for check in report.get("checks", []):
        lines.append(
            f"- {_escape(check.get('label', check.get('name')))}：{_escape(check.get('status'))}；"
            f"实际值 {_escape(check.get('actual'))}；限制 {_escape(check.get('limit'))}；"
            + "；".join(_escape(reason) for reason in check.get("reasons", []))
        )
    lines.extend(["", "## 用户情景", ""])
    lines.append(
        "以下为用户设定的价格冲击假设，不是预测概率或损失上限；未模拟强平、流动性及交易费用。"
    )
    scenarios = report.get("scenarios", [])
    if not scenarios:
        lines.append("未配置情景。")
    for scenario in scenarios:
        lines.append(
            f"- {_escape(scenario.get('name'))}：损益 {_escape(scenario.get('total_pnl'))}；"
            f"情景后权益 {_escape(scenario.get('equity_after'))}；完整 {_escape(scenario.get('complete'))}"
        )
        lines.extend(f"  - {_escape(reason)}" for reason in scenario.get("reasons", []))
    lines.extend(["", "## 完整数据与口径", ""])
    lines.extend(
        "    " + line for line in json.dumps(report, ensure_ascii=False, indent=2).splitlines()
    )
    return "\n".join(lines) + "\n"


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(description="离线个人组合风险与情景分析，不读取凭证、不下单")
    commands = cli.add_subparsers(dest="command", required=True)
    template = commands.add_parser("template", help="生成缺数据、缺预算的待填写模板")
    template.add_argument("--at")
    template.add_argument("--currency", default="USDT")
    template.add_argument("--output", type=Path)
    report = commands.add_parser("report", help="分析用户显式提供的持仓、行情及风险设置")
    report.add_argument("--input", type=Path, required=True)
    report.add_argument("--ledger", type=Path, help="可选，只读导入实际数量和现金，不读取历史市值")
    report.add_argument(
        "--confirm-ledger", action="store_true", help="用户已核对台账数据；非交易所认证"
    )
    report.add_argument("--available-cash", help="仅配合台账，手工提供可用现金；默认未知")
    report.add_argument("--at")
    report.add_argument("--max-quote-age-seconds", type=int, default=300)
    report.add_argument("--format", choices=("json", "markdown"), default="json")
    report.add_argument("--output", type=Path)
    return cli


def main(argv: list[str] | None = None) -> int:
    cli = parser()
    args = cli.parse_args(argv)
    try:
        now = datetime.fromisoformat(args.at) if args.at else datetime.now(UTC)
        if args.command == "template":
            result = RiskRequest.model_validate(
                {
                    "currency": args.currency,
                    "as_of": now,
                    "source": "user_to_complete",
                    "cash": None,
                    "cash_scope": "unknown",
                    "positions_complete": False,
                    "spot_positions": [],
                    "contracts": [],
                    "products": {},
                    "quotes": [],
                    "settings": {},
                    "scenarios": [],
                }
            ).model_dump(mode="json")
        else:
            if args.ledger is None and (args.confirm_ledger or args.available_cash is not None):
                raise ValueError("--confirm-ledger and --available-cash require --ledger")
            request = RiskRequest.model_validate(read_json(args.input))
            if args.ledger is not None:
                opening, entries = LedgerStore(args.ledger).load()
                request = request_from_ledger(
                    request,
                    replay(opening, entries),
                    verified=args.confirm_ledger,
                    available_cash=Decimal(args.available_cash)
                    if args.available_cash is not None
                    else None,
                )
            result = analyze_risk(
                request, now=now, max_quote_age_seconds=args.max_quote_age_seconds
            )
            result["valuation_time_mode"] = "explicit" if args.at else "current"
            result["ledger_confirmation"] = (
                ("user_confirmed_local_data" if args.confirm_ledger else "not_confirmed")
                if args.ledger is not None
                else "not_imported"
            )
        text = (
            render(result)
            if getattr(args, "format", "json") == "markdown"
            else json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        )
        if args.output is not None:
            output = local_path(args.output)
            output.parent.mkdir(parents=True, exist_ok=True)
            with output.open("x", encoding="utf-8") as stream:
                stream.write(text)
        print(text, end="")
        return 0
    except (ValueError, OSError, sqlite3.Error, InvalidOperation) as exc:
        cli.exit(2, f"个人风险分析失败：{exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
