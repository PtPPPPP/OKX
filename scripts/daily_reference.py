"""Run: python -m scripts.daily_reference --config configs/daily_reference.json."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.daily_reference import ReferenceConfig, analyze
from app.market.daily_public import DailyPublicClient
from app.market.historical_data import load_candles_csv, save_candles_csv


def write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )


def display_number(value: object) -> str:
    number = Decimal(str(value))
    if not number.is_finite():
        return "不可用"
    places = max(0, 5 - number.adjusted()) if number else 2
    return format(number.quantize(Decimal(1).scaleb(-places)), "f")


def display_time(value: object) -> str:
    stamp = datetime.fromtimestamp(int(str(value)) / 1000, UTC) + timedelta(hours=8)
    return stamp.strftime("%Y-%m-%d %H:%M:%S 北京时间")


def render_report(report: dict[str, Any]) -> str:
    cfg = report["config"]
    lines = [
        "# 每日交易参考报告",
        "",
        f"行情截止（UTC）：{report['asof']}；北京时间：{report['asof_beijing']}",
        "",
        "生成方式：保存数据的离线复现，未更新行情。"
        if report.get("offline_replay")
        else "生成方式：OKX 公共行情采集。",
        "",
        "状态：行情观察 + 未验证基线。每天运行一次生成快照；入场前需重新运行核对行情。",
        "现货按投资逻辑长期持有，无固定到期退出；永续基线持仓不超过 72 小时。"
        if cfg.get("spot_policy", "tactical_research") == "thesis_hold"
        else "历史短线研究模式：现货与永续均按配置的小时期限回放，不代表当前长期现货政策。",
        "日报不是盘中监控，不负责触发止损或发送订单。",
        "",
        "| 产品 | 数据 | 4H 趋势 | 收盘价 | 24H 涨跌% | ATR% | 基线条件 |",
        "|---|---|---|---:|---:|---:|---|",
    ]
    trend_names = {"up": "上行", "down": "下行", "mixed": "混合"}
    for item in report["instruments"]:
        if item["status"] != "READY":
            lines.append(f"| {item['instrument']} | {item['status']} | — | — | — | — | — |")
        else:
            lines.append(
                f"| {item['instrument']} | READY | {trend_names[item['trend_4h']]} | "
                f"{item['close']} | {item['return_24h_pct']:.2f} | {item['atr_pct']:.2f} | {item['plan']['status']} |"
            )
    lines += [
        "",
        "## 技术观察与合约短线基线",
        "",
        f"- 趋势：完整 4H K 线的 SMA{cfg['trend_fast_4h']}/SMA{cfg['trend_slow_4h']}，同时检查价格位置。",
        f"- 触发：最新完整 1H 收盘价突破此前 {cfg['breakout_hours']} 根高点/低点，且与趋势同向。",
        f"- 止损：{cfg['stop_atr']} × ATR；目标：{cfg['target_atr']} × ATR；ATR 为最近 {cfg['atr_hours']} 根真实波幅的算术均值。",
        f"- 合约时间退出：入场后最多 {cfg['max_holding_hours']} 小时。长期现货不采用这些小时级入场、止损、目标或到期退出规则。",
        "- OBSERVE 仅表示长期现货观察，不等于确认投资逻辑成立或推荐买入/继续持有。",
        "- 前期高低点是观察水平，不是已验证的支撑/阻力；ATR 也不是未来价格区间的概率保证。",
        "- 预测概率和预测区间留空：尚未建立经过时间切分、校准和样本外检验的预测模型。",
        "- INTC/QQQ 现货使用 XINTC/XQQQ，永续使用 INTC/QQQ；XAUT 现货与 XAU 永续独立分析。",
        "- 本报告未接入财报/宏观事件日历；股票及黄金相关事件需另行核实。",
        "",
    ]
    for item in report["instruments"]:
        lines += [f"## {item['instrument']}", ""]
        if item["status"] != "READY":
            lines += [item["reason"], ""]
            continue
        rv = item["relative_volume"]
        rv_text = f"{rv:.2f}" if rv is not None else "不可用"
        lines += [
            f"- 数据：{item['history_start']} 至 {item['data_through']}，{item['history_candles']} 根 1H。",
            f"- 前 {cfg['breakout_hours']}H 高/低：{item['prior_high']} / {item['prior_low']} USDT。",
            f"- ATR：{display_number(item['atr'])} USDT；相对前 20H 均量：{rv_text}。",
            f"- 条件：{item['plan']['status']}；{item['plan']['reason']}。",
        ]
        plan = item["plan"]
        if plan["status"] in {"LONG", "SHORT"}:
            lines += [
                f"- 信号收盘参考价：{plan['reference_entry']}；止损参考：{plan['stop']}；目标参考：{plan['target']}。",
                f"- 扣成本前盈亏比：{float(plan['reward_risk_before_costs']):.2f}；参考条件失效时间：{plan['expires_at']}。",
                "- 以上为信号时点条件，不代表当前可成交；跳空、点差和手续费会改变实际风险。",
            ]
        context = item.get("context", {})
        lines.append(
            f"- 当前附加行情：{context.get('quality', 'UNAVAILABLE')}（与 K 线截止时间分开记录）。"
        )
        ticker = context.get("ticker")
        if ticker:
            bid, ask = Decimal(ticker.get("bidPx") or "0"), Decimal(ticker.get("askPx") or "0")
            if bid.is_finite() and ask.is_finite() and 0 < bid <= ask:
                spread = (ask - bid) / ((ask + bid) / 2) * 10000
                lines.append(
                    f"- 当前买卖价差：{spread:.2f} bps；最新价：{ticker.get('last')}；快照：{display_time(ticker['ts'])}。"
                )
        funding = context.get("funding")
        if funding and funding.get("fundingRate"):
            rate = Decimal(funding["fundingRate"])
            if rate.is_finite():
                lines.append(
                    f"- 当前资金费率：{rate * 100:.5f}% / 本次结算周期；结算时间：{display_time(funding['fundingTime'])}；费率可能变化。"
                )
        oi = context.get("open_interest")
        if oi:
            lines.append(
                f"- 当前持仓量：{display_number(oi['oi'])} 张；名义美元值：{display_number(oi['oiUsd'])}；不从单个快照推断变化。"
            )
        lines += [f"- 附加行情缺项：{w}" for w in context.get("warnings", [])]
        r = item["replay"]
        if r["status"] == "NOT_APPLICABLE_LONG_TERM":
            lines += ["- 长期现货不输出短线路径回放成绩；投资逻辑及失效条件尚待登记。", ""]
            continue
        avg = r["mean_trade_return_pct"]
        avg_text = f"{avg:.3f}%" if avg is not None else "无样本"
        lines += [
            f"- 历史路径回放：每天 UTC {r['decision_hour_utc']:02d}:00 检查一次；已结束 {r['closed_trades']} 笔，未结束 {r['right_censored_trades']} 笔；每笔均值 {avg_text}。",
            "",
        ]
    lines += [
        "## 回放限制",
        "",
        f"固定假设：单边手续费 {cfg['fee_bps_per_side']} bps、单边滑点 {cfg['slippage_bps_per_side']} bps。这些是可调整的研究假设，不是你的账户费率。",
        "回放按信号后的下一根开盘入场，同根同时触及止损与目标时优先按止损处理；单个产品不重叠持仓。",
        "每笔收益按入场名义价值计算，不合并为组合收益。未模拟资金占用、最小下单量、成交量限制、资金费、杠杆、强平与市场冲击，不能视为可交易账户回测结果。",
        "这段历史用于检查基线行为，并非样本外验证；正收益样本比例不等于未来胜率，所有基线仍标记为未验证。",
        "运行时刻变化会改变历史每日采样时点，横向比较应使用固定时刻和相同配置。",
        "",
        "数据来源：[OKX 公共 API](https://www.okx.com/docs-v5/en/)；"
        "产品说明：[代币化股票](https://www.okx.com/stock)、[股票永续](https://www.okx.com/en-us/help/stock-perpetuals)。",
        "",
    ]
    return "\n".join(lines)


def run(
    cfg: ReferenceConfig,
    output: Path,
    *,
    source: Path | None = None,
) -> tuple[Path, int]:
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / "config.json", cfg.model_dump(mode="json"))
    client = None if source else DailyPublicClient()
    try:
        if source:
            original = json.loads((source / "report.json").read_text(encoding="utf-8"))
            asof = datetime.fromisoformat(original["asof"])
            metadata = json.loads((source / "instruments.json").read_text(encoding="utf-8"))
        else:
            assert client is not None
            server_time = client.server_time()
            if abs((datetime.now(UTC) - server_time).total_seconds()) > 120:
                raise ValueError("local/server clock difference exceeds 120 seconds")
            asof = server_time.replace(minute=0, second=0, microsecond=0)
            metadata = client.instruments()
        write_json(
            output / "instruments.json", {k: v for k, v in metadata.items() if k in cfg.instruments}
        )
        items: list[dict[str, Any]] = []
        for instrument in cfg.instruments:
            print(f"Collecting {instrument}", flush=True)
            meta = metadata.get(instrument)
            if not meta or meta.get("state") != "live":
                items.append(
                    {
                        "instrument": instrument,
                        "status": "UNAVAILABLE",
                        "reason": "本次 OKX 公共产品目录未返回此产品的 live 状态；不使用其他产品行情代替。",
                    }
                )
                continue
            try:
                if source:
                    original_csv = source / f"{instrument}.csv"
                    expected_hash = original.get("data_hashes", {}).get(original_csv.name)
                    if (
                        not expected_hash
                        or hashlib.sha256(original_csv.read_bytes()).hexdigest() != expected_hash
                    ):
                        raise ValueError("source candle hash mismatch or missing")
                    candles = load_candles_csv(original_csv, bar="1h")
                    context = json.loads(
                        (source / f"{instrument}-context.json").read_text(encoding="utf-8")
                    )
                else:
                    assert client is not None
                    candles = client.candles(instrument, cutoff=asof, days=cfg.history_days)
                    context = client.context(instrument, cutoff=asof)
                save_candles_csv(candles, output / f"{instrument}.csv")
                write_json(output / f"{instrument}-context.json", context)
                item = analyze(instrument, candles, cfg, tick=Decimal(meta["tickSz"]), asof=asof)
                item["context"] = context
                items.append(item)
            except (ValueError, KeyError, TypeError, OSError, ArithmeticError) as exc:
                # No external response bodies, credentials, proxy URLs or account data in diagnostics.
                items.append(
                    {
                        "instrument": instrument,
                        "status": "ERROR",
                        "reason": f"数据采集/校验失败（{type(exc).__name__}）；不生成规则参数。",
                    }
                )
        report = {
            "model_version": "daily_reference_v2_holding_policy",
            "asof": asof.isoformat(),
            "asof_beijing": (asof + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M +08:00"),
            "generated_at": datetime.now(UTC).isoformat(),
            "offline_replay": source is not None,
            "config": cfg.model_dump(mode="json"),
            "instruments": items,
            "data_hashes": {
                p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(output.glob("*.csv"))
            },
        }
        write_json(output / "report.json", report)
        (output / "report.md").write_text(render_report(report), encoding="utf-8")
        ready = sum(item["status"] == "READY" for item in items)
        errors = sum(item["status"] == "ERROR" for item in items)
        print(
            json.dumps(
                {"report": str((output / "report.md").resolve()), "ready": ready, "errors": errors},
                ensure_ascii=False,
            )
        )
        return output / "report.md", 2 if not ready else (1 if errors else 0)
    finally:
        if client is not None:
            client.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="OKX 只读每日交易参考模型")
    parser.add_argument("--config", type=Path, default=Path("configs/daily_reference.json"))
    parser.add_argument("--output-root", type=Path, default=Path("artifacts/daily-reference"))
    parser.add_argument(
        "--replay-run", type=Path, help="离线重放已保存运行目录，沿用该次配置与数据"
    )
    args = parser.parse_args()
    config_path = args.replay_run / "config.json" if args.replay_run else args.config
    config_values = json.loads(config_path.read_text(encoding="utf-8"))
    if args.replay_run and "spot_policy" not in config_values:
        # Preserve legacy snapshot semantics without applying them to the current portfolio.
        config_values["spot_policy"] = "tactical_research"
    cfg = ReferenceConfig.model_validate(config_values)
    output = args.output_root / (
        datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid4().hex[:8]
    )
    try:
        _, code = run(cfg, output, source=args.replay_run)
    except (ValueError, KeyError, TypeError, OSError) as exc:
        write_json(output / "failure.json", {"status": "FAILED", "error_type": type(exc).__name__})
        print(f"Report failed: {type(exc).__name__}; see {output / 'failure.json'}")
        raise SystemExit(2) from None
    raise SystemExit(code)


if __name__ == "__main__":
    main()
