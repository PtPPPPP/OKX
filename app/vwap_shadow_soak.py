from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from app.config.run_config import load_run_config
from app.market.synthetic_candles import SyntheticCandleRequest
from app.services.vwap_shadow_soak import (
    ShadowSoakError,
    build_synthetic_soak_source,
    load_csv_soak_source,
    run_vwap_shadow_soak,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.vwap_shadow_soak",
        description="Run local, read-only, non-executable VWAP Shadow soak processing.",
    )
    parser.add_argument("--config", type=Path, required=True)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--input-csv", type=Path)
    source.add_argument("--synthetic-bars", type=int)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--bar-interval")
    parser.add_argument("--checkpoint-every", type=int, default=500)
    parser.add_argument("--stop-after-bars", type=int)
    parser.add_argument("--resume-run-id")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data/results/vwap-shadow-soak"),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    config = load_run_config(arguments.config, environ={})
    bar_interval = (arguments.bar_interval or config.market.bar).lower()
    if arguments.input_csv is not None:
        source = load_csv_soak_source(
            arguments.input_csv,
            bar_interval=bar_interval,
        )
    else:
        source = build_synthetic_soak_source(
            SyntheticCandleRequest(
                count=arguments.synthetic_bars,
                seed=arguments.seed,
                bar_interval=bar_interval,
            )
        )
    output_dir: Path = arguments.output_dir
    try:
        summary = run_vwap_shadow_soak(
            database_path=output_dir / "vwap-shadow-soak.db",
            output_dir=output_dir,
            config=config,
            source=source,
            bar_interval=bar_interval,
            checkpoint_every=arguments.checkpoint_every,
            stop_after_bars=arguments.stop_after_bars,
            resume_run_id=arguments.resume_run_id,
        )
    except ShadowSoakError as exc:
        raise SystemExit(str(exc)) from exc
    for key, value in summary.items():
        rendered = str(value).lower() if isinstance(value, bool) else value
        print(f"{key}={rendered}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
