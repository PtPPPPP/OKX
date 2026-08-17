"""Download resumable public OKX candles for read-only VWAP research."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.config.run_config import load_run_config
from app.market.network import NetworkConfiguration
from backtest.vwap_signal_edge_data import HistoricalCandleCache, OKXHistoricalCandleDownloader


def main() -> None:
    parser = argparse.ArgumentParser(description="Resumable public VWAP research history")
    parser.add_argument("--config", type=Path, default=Path("configs/btc_vwap_shadow.yaml"))
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--months", type=int, default=36)
    parser.add_argument("--end", type=datetime.fromisoformat)
    parser.add_argument("--no-resume", action="store_true")
    arguments = parser.parse_args()
    if arguments.months < 12:
        raise ValueError("VWAP signal edge research requires at least 12 months")
    config = load_run_config(arguments.config, environ={})
    if config.strategy.name != "vwap_shadow":
        raise ValueError("research history must use the formal vwap_shadow configuration")
    end = arguments.end.astimezone(UTC) if arguments.end else datetime.now(UTC)
    end = end.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
    start = end - timedelta(days=arguments.months * 31)
    network = NetworkConfiguration.from_environment()
    downloader = OKXHistoricalCandleDownloader(
        HistoricalCandleCache(arguments.cache), network=network
    )
    try:
        result = downloader.download(
            instrument=config.market.instrument_id,
            bar=config.market.bar,
            start=start,
            end=end,
            resume=not arguments.no_resume,
        )
    finally:
        downloader.close()
    print(json.dumps(asdict(result), ensure_ascii=False, default=list))


if __name__ == "__main__":
    main()
