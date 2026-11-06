"""Offline registration and review of user supplied spot investment theses."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.portfolio.spot_thesis import PRODUCTS, Evidence, Review, ThesisRevision, template_revision
from app.portfolio.thesis_storage import ThesisStore
from scripts._console import configure_utf8_output

LOCAL_ROOT = Path(__file__).resolve().parents[1] / "data" / "personal-theses"


def local_path(path: Path) -> Path:
    resolved = path.resolve()
    if resolved == LOCAL_ROOT.resolve() or not resolved.is_relative_to(LOCAL_ROOT.resolve()):
        raise ValueError(f"personal thesis outputs must be inside {LOCAL_ROOT}")
    return resolved


def _unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=_unique_keys)


def _escape(value: object) -> str:
    return re.sub(r"([\\`*_{}\[\]()#+!|<>])", r"\\\1", str(value)).replace("\n", " ")


def render(report: dict[str, Any]) -> str:
    lines = [
        "# 现货投资逻辑复核",
        "",
        f"报告时间：{report['generated_at']}",
        "",
        "现货无固定持有期限。此报告反映已登记材料，不验证材料真实性，不生成买卖指令。",
        "事实、用户判断与模型推断分别列示；小时指标和持有满 72 小时均不能单独判定长期逻辑失效。",
    ]
    for item in report["products"]:
        lines += [
            "",
            f"## {_escape(item['instrument'])}",
            "",
            f"状态：{_escape(item['status_label'])}（{item['status']}）",
            f"买入理由：{_escape(item.get('buy_reason') or '待填写，未默认认定成立')}",
            f"判断依据：{'; '.join(_escape(r) for r in item.get('reason_details', item['reasons']))}",
            "",
            "完整复核数据（含证据分类、来源、时点、条件和引用标识）：",
            "",
        ]
        # Indented code preserves arbitrary user text without allowing code-fence injection.
        lines += [
            "    " + line for line in json.dumps(item, ensure_ascii=False, indent=2).splitlines()
        ]
    return "\n".join(lines) + "\n"


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(
        description="离线现货投资逻辑登记与复核，不访问账户或生成交易指令"
    )
    cli.add_argument("--database", type=Path, default=LOCAL_ROOT / "theses.db")
    commands = cli.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="新建独立空库，五个产品保持待补充")
    template = commands.add_parser("template", help="生成待填写模板，不代填买入理由")
    template.add_argument("--instrument", choices=PRODUCTS)
    template.add_argument("--at", help="模板登记时间；默认当前 UTC")
    template.add_argument("--source", default="user_to_complete")
    template.add_argument("--output", type=Path)
    for name, help_text in (
        ("register", "新增投资逻辑版本，旧版本保留"),
        ("evidence", "新增不可改写的证据或关联更正"),
        ("review", "登记人工复核及条件检查，保存当时结果"),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--file", type=Path, required=True)
    report = commands.add_parser("report", help="按指定时点重算五个产品的状态")
    report.add_argument("--at")
    report.add_argument("--format", choices=("json", "markdown"), default="json")
    report.add_argument("--output", type=Path)
    history = commands.add_parser("history", help="读取所有版本、证据和原始复核结果")
    history.add_argument("--output", type=Path)
    return cli


def main(argv: list[str] | None = None) -> int:
    configure_utf8_output()
    cli = parser()
    args = cli.parse_args(argv)
    try:
        store = ThesisStore(local_path(args.database))
        result: Any
        if args.command == "init":
            store.initialize()
            result = {"created": str(store.path), "note": "五个产品均待补充，不预设买入理由"}
        elif args.command == "template":
            at = datetime.fromisoformat(args.at) if args.at else datetime.now(UTC)
            products = (args.instrument,) if args.instrument else PRODUCTS
            items = [
                template_revision(p, at=at, source=args.source, id=uuid4().hex).model_dump(
                    mode="json"
                )
                for p in products
            ]
            result = items[0] if args.instrument else items
        elif args.command == "register":
            result = {
                "added": store.append_revision(ThesisRevision.model_validate(_read(args.file)))
            }
        elif args.command == "evidence":
            result = {"added": store.append_evidence(Evidence.model_validate(_read(args.file)))}
        elif args.command == "review":
            result = {"added": store.append_review(Review.model_validate(_read(args.file)))}
        elif args.command == "history":
            result = store.history()
        else:
            now = datetime.fromisoformat(args.at) if args.at else datetime.now(UTC)
            result = store.report(now=now)
        text = (
            render(result)
            if getattr(args, "format", "json") == "markdown"
            else json.dumps(result, ensure_ascii=False, indent=2) + "\n"
        )
        if getattr(args, "output", None):
            output = local_path(args.output)
            output.parent.mkdir(parents=True, exist_ok=True)
            with output.open("x", encoding="utf-8") as stream:
                stream.write(text)
        print(text, end="")
        return 0
    except (ValueError, OSError, sqlite3.Error) as exc:
        cli.exit(2, f"投资逻辑操作失败：{exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
