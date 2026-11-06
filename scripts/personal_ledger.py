"""Offline personal position ledger. No settings, credentials or network imports."""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.portfolio.ledger_storage import LedgerStore
from app.portfolio.personal_ledger import (
    Opening,
    Quote,
    Transaction,
    legacy_opening,
    replay,
    value_ledger,
)

LOCAL_ROOT = Path(__file__).resolve().parents[1] / "data" / "personal-positions"


def local_path(path: Path) -> Path:
    resolved = path.resolve()
    if not resolved.is_relative_to(LOCAL_ROOT.resolve()) or resolved == LOCAL_ROOT.resolve():
        raise ValueError(f"ledger outputs must be inside {LOCAL_ROOT}")
    return resolved


def _unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def read_json(path: Path) -> Any:
    return json.loads(
        path.read_text(encoding="utf-8-sig"),
        parse_float=Decimal,
        object_pairs_hook=_unique_keys,
    )


def _array(path: Path) -> list[Any]:
    value = read_json(path)
    if not isinstance(value, list):
        raise ValueError("expected a JSON array")
    return value


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(description="本地个人持仓台账（离线、不读取凭证、不下单）")
    cli.add_argument("--database", type=Path, default=LOCAL_ROOT / "ledger.db")
    commands = cli.add_subparsers(dest="command", required=True)
    initialize = commands.add_parser("init", help="从明确的期初数量、成本与现金创建新台账")
    initialize.add_argument("--opening", type=Path, required=True)
    legacy = commands.add_parser("import-legacy", help="保留旧文件为历史快照，数量/现金可未知")
    legacy.add_argument("--positions", type=Path, required=True)
    legacy.add_argument("--as-of", required=True, help="本次导入时点（带时区），不是旧市值时点")
    legacy.add_argument("--source", required=True)
    batch = commands.add_parser("import", help="原子导入流水数组；重复ID不会重复记账")
    batch.add_argument("--transactions", type=Path, required=True)
    record = commands.add_parser("record", help="手动登记一条流水，金额用十进制文本")
    record.add_argument("--id", required=True)
    record.add_argument(
        "--kind", choices=["buy", "sell", "fee", "transfer_in", "transfer_out"], required=True
    )
    record.add_argument("--at", required=True, help="实际发生时间，必须带时区")
    record.add_argument("--source", required=True)
    record.add_argument("--currency", default="USDT")
    record.add_argument("--instrument")
    for field in ("quantity", "price", "amount", "fee"):
        record.add_argument(f"--{field}", default="0")
    show = commands.add_parser("show", help="按带时间戳的行情计算估值，缺项时总估值为null")
    show.add_argument("--quotes", type=Path)
    show.add_argument("--at", help="显式估值时点；省略时使用当前UTC时间")
    show.add_argument("--max-age-seconds", type=int, default=300)
    show.add_argument("--output", type=Path, help="另存JSON（仅本地忽略目录，拒绝覆盖）")
    commands.add_parser("history", help="读取期初与已登记流水，供本地复核")
    return cli


def main(argv: list[str] | None = None) -> int:
    cli = parser()
    args = cli.parse_args(argv)
    try:
        store = LedgerStore(local_path(args.database))
        if args.command == "init":
            store.initialize(Opening.model_validate(read_json(args.opening)))
            result: Any = {"created": str(store.path)}
        elif args.command == "import-legacy":
            rows = _array(args.positions)
            if any(not isinstance(row, dict) for row in rows):
                raise ValueError("legacy positions must be objects")
            store.initialize(
                legacy_opening(rows, as_of=datetime.fromisoformat(args.as_of), source=args.source)
            )
            result = {"created": str(store.path), "note": "历史快照；未知数量与现金保持未知"}
        elif args.command in {"import", "record"}:
            if args.command == "import":
                entries = [Transaction.model_validate(row) for row in _array(args.transactions)]
            else:
                fields = (
                    "id",
                    "kind",
                    "at",
                    "source",
                    "currency",
                    "instrument",
                    "quantity",
                    "price",
                    "amount",
                    "fee",
                )
                entries = [Transaction.model_validate({key: getattr(args, key) for key in fields})]
            added, duplicates = store.import_transactions(entries)
            result = {"added": added, "duplicates": duplicates}
        elif args.command == "history":
            opening, entries = store.load()
            result = {
                "opening": opening.model_dump(mode="json"),
                "transactions": [item.model_dump(mode="json") for item in entries],
            }
        else:
            opening, entries = store.load()
            quotes = (
                [Quote.model_validate(row) for row in _array(args.quotes)] if args.quotes else []
            )
            now = datetime.fromisoformat(args.at) if args.at else datetime.now(UTC)
            result = value_ledger(
                replay(opening, entries), quotes, now=now, max_age_seconds=args.max_age_seconds
            )
            result["valuation_time_mode"] = "explicit" if args.at else "current"
            if args.output:
                output = local_path(args.output)
                output.parent.mkdir(parents=True, exist_ok=True)
                with output.open("x", encoding="utf-8") as stream:
                    json.dump(result, stream, ensure_ascii=False, indent=2)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError, sqlite3.Error) as exc:
        cli.exit(2, f"台账操作失败：{exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
