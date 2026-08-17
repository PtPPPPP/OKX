"""Run public-only Market Information Research V4 Phase A."""

from __future__ import annotations

import argparse
import csv
import json
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.market.network import NetworkConfiguration
from backtest.market_information_artifacts import write_artifacts
from backtest.market_information_data import (
    OKXMarketInformationClient,
    download_funding,
    download_open_interest,
    download_swap_candles,
    fetch_metadata,
)
from backtest.market_information_features import build_market_information_features
from backtest.market_information_research import run_information_study
from scripts.collect_prospective_oos import database_snapshot, protected_hashes

DEFAULT_ROOT = Path("data/market_information")
SPOT_CACHE = Path("data/research/vwap_signal_edge_v1/BTC-USDT_1h")


def main() -> None:
    parser = argparse.ArgumentParser(description="OKX public derivatives information Phase A")
    parser.add_argument("--data-root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--spot-cache", type=Path, default=SPOT_CACHE)
    parser.add_argument("--output-root", type=Path, default=Path("artifacts/market-information"))
    args = parser.parse_args()
    before = database_snapshot(Path("data/trading.db"))
    protected = protected_hashes()
    network = NetworkConfiguration.from_environment()
    if not network.probe_proxy_listener():
        raise RuntimeError("configured proxy listener is unavailable")
    client = OKXMarketInformationClient(network)
    try:
        metadata = fetch_metadata(client, args.data_root)
        downloads = [
            download_funding(client, args.data_root),
            download_open_interest(client, args.data_root),
            download_swap_candles(client, args.data_root),
        ]
    finally:
        client.close()
    _normalize(args.data_root)
    aligned = args.data_root / "aligned" / "market_information_features_1h.csv"
    features, feature_manifest = build_market_information_features(
        args.spot_cache, args.data_root, aligned
    )
    study = run_information_study(features)
    download_records = []
    for item in downloads:
        record = asdict(item)
        source = str(record["source"])
        record["raw_pages"] = len(list((args.data_root / "raw" / source / "pages").glob("*.json")))
        record["requests"] = record["raw_pages"]
        record["request_window"] = {
            "start_ms": 1690074000000 if source == "perp" else None,
            "end_ms": 1786550399999,
        }
        download_records.append(record)
    data_manifest = {
        "instrument": "BTC-USDT-SWAP",
        "spot_reference": "BTC-USDT",
        "bar": "1H",
        "research_cutoff": "2026-08-12T23:59:59.999000+00:00",
        "prospective_excluded": True,
        "downloads": download_records,
        "aligned_dataset_hash": feature_manifest["dataset_hash"],
    }
    capability = _capability_audit(metadata, network.redacted_proxy_url)
    output = (
        args.output_root
        / f"MARKET_INFORMATION_V4_PHASE_A_{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
    )
    summary = write_artifacts(
        output,
        capability_audit=capability,
        data_manifest=data_manifest,
        feature_manifest=feature_manifest,
        study=study,
        feature_rows=features,
        database_before=before,
        protected_before=protected,
    )
    print(
        json.dumps(
            {
                "output": str(output.resolve()),
                "final_state": summary["final_state"],
                "phase_b_hypotheses": len(study["phase_b_hypotheses"]),
            },
            ensure_ascii=False,
        )
    )


def _normalize(root: Path) -> None:
    target = root / "normalized"
    target.mkdir(parents=True, exist_ok=True)
    expected = tuple(target / name for name in ("funding.csv", "open_interest.csv", "perp.csv"))
    if all(path.exists() for path in expected):
        return
    specifications: tuple[tuple[str, list[str], Any], ...] = (
        (
            "funding",
            ["fundingTime", "realizedRate", "fundingRate", "method", "formulaType"],
            lambda row: row,
        ),
        (
            "open_interest",
            ["timestamp", "oi_contracts", "oi_btc", "oi_usd"],
            lambda row: dict(
                zip(["timestamp", "oi_contracts", "oi_btc", "oi_usd"], row, strict=True)
            ),
        ),
        (
            "perp",
            [
                "timestamp",
                "open",
                "high",
                "low",
                "close",
                "volume_contracts",
                "volume_btc",
                "volume_quote",
                "confirm",
            ],
            lambda row: dict(
                zip(
                    [
                        "timestamp",
                        "open",
                        "high",
                        "low",
                        "close",
                        "volume_contracts",
                        "volume_btc",
                        "volume_quote",
                        "confirm",
                    ],
                    row,
                    strict=True,
                )
            ),
        ),
    )
    for source, fields, convert in specifications:
        records = [
            json.loads(path.read_text(encoding="utf-8"))
            for path in sorted((root / "raw" / source / "records").glob("*.json"))
        ]
        with (target / f"{source}.csv").open("w", encoding="utf-8", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(convert(row) for row in records)


def _capability_audit(metadata: dict[str, Any], proxy: str | None) -> dict[str, Any]:
    instrument = metadata["instrument"]["response"]["data"][0]
    return {
        "authority": "OKX official API documentation and actual public endpoint probes",
        "official_documentation": "https://www.okx.com/docs-v5/en/",
        "network_mode": "proxy",
        "proxy_url_redacted": proxy,
        "authentication_used": False,
        "private_endpoint_calls": 0,
        "instrument": {
            key: instrument.get(key)
            for key in (
                "instId",
                "instType",
                "state",
                "ctType",
                "ctVal",
                "ctValCcy",
                "settleCcy",
                "tickSz",
                "lotSz",
                "listTime",
            )
        },
        "endpoints": [
            {
                "endpoint": "/api/v5/public/instruments",
                "purpose": "contract metadata",
                "public": True,
                "pagination": "instId exact query",
                "probe_code": metadata["instrument"]["response"]["code"],
            },
            {
                "endpoint": "/api/v5/public/funding-rate-history",
                "purpose": "realized funding history",
                "public": True,
                "pagination": "after=older fundingTime, maximum requested limit 400",
                "retention": "actual server retention reported in data_manifest",
            },
            {
                "endpoint": "/api/v5/rubik/stat/contracts/open-interest-history",
                "purpose": "historical OI",
                "public": True,
                "pagination": "end=older timestamp, limit 100",
                "period": "1H",
                "retention": "actual server retention reported in data_manifest",
            },
            {
                "endpoint": "/api/v5/market/history-candles",
                "purpose": "confirmed swap candles for basis",
                "public": True,
                "pagination": "after=older timestamp, limit 300",
                "confirmed_only": True,
            },
            {
                "endpoint": "/api/v5/public/open-interest",
                "purpose": "current OI capability probe",
                "public": True,
                "probe_code": metadata["open_interest_current"]["response"]["code"],
            },
            {
                "endpoint": "/api/v5/public/funding-rate",
                "purpose": "current funding capability probe",
                "public": True,
                "probe_code": metadata["funding_current"]["response"]["code"],
            },
            {
                "endpoint": "/api/v5/public/mark-price",
                "purpose": "mark price capability probe",
                "public": True,
                "probe_code": metadata["mark_current"]["response"]["code"],
            },
            {
                "endpoint": "/api/v5/market/index-tickers",
                "purpose": "index price capability probe",
                "public": True,
                "probe_code": metadata["index_current"]["response"]["code"],
            },
        ],
        "rate_limit_policy": "bounded sequential requests, 120 ms minimum application delay, maximum two retries with exponential delay",
        "basis_choice": "perpetual candle close divided by spot candle close minus one; exact same confirmed hourly timestamp",
    }


if __name__ == "__main__":
    main()
